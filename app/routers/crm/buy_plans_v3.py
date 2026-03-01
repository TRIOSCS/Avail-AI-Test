"""
buy_plans_v3.py — Buy Plan V3 API Endpoints

Structured buy plan system with split lines, dual approval tracks,
AI-powered vendor selection, and per-line PO tracking.

Endpoints:
  POST /api/quotes/{quote_id}/buy-plan-v3/build  — AI-build draft plan
  GET  /api/buy-plans-v3                          — List / queue view
  GET  /api/buy-plans-v3/{plan_id}                — Full detail + lines
  POST /api/buy-plans-v3/{plan_id}/submit         — Submit with SO#
  POST /api/buy-plans-v3/{plan_id}/approve        — Manager approve/reject
  POST /api/buy-plans-v3/{plan_id}/resubmit       — Resubmit after rejection
  POST /api/buy-plans-v3/{plan_id}/verify-so      — Ops verify SO
  POST /api/buy-plans-v3/{plan_id}/lines/{line_id}/confirm-po  — Buyer PO
  POST /api/buy-plans-v3/{plan_id}/lines/{line_id}/verify-po   — Ops verify PO
  POST /api/buy-plans-v3/{plan_id}/lines/{line_id}/issue       — Flag issue
  GET  /api/buy-plans-v3/{plan_id}/offers/{req_id} — Offer comparison
  GET  /api/buy-plans-v3/verification-group        — List ops members
  POST /api/buy-plans-v3/verification-group        — Add/remove member

Called by: frontend (Phases 6-8)
Depends on: services/buy_plan_v3_service.py, schemas/buy_plan.py
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_buyer, require_user
from ...models import Offer, Requirement, User
from ...models.buy_plan import (
    BuyPlanLine,
    BuyPlanV3,
    VerificationGroupMember,
)
from ...schemas.buy_plan import (
    BuyPlanLineIssue,
    BuyPlanV3Approval,
    BuyPlanV3Submit,
    POConfirmation,
    POVerificationRequest,
    SOVerificationRequest,
    VerificationGroupUpdate,
)
from ...services.buy_plan_v3_service import (
    approve_buy_plan,
    build_buy_plan,
    check_completion,
    confirm_po,
    detect_favoritism,
    flag_line_issue,
    generate_case_report,
    resubmit_buy_plan,
    submit_buy_plan,
    verify_po,
    verify_so,
)
from ...services.buyplan_v3_notifications import (
    notify_v3_approved,
    notify_v3_completed,
    notify_v3_issue_flagged,
    notify_v3_po_confirmed,
    notify_v3_rejected,
    notify_v3_so_rejected,
    notify_v3_so_verified,
    notify_v3_submitted,
    run_v3_notify_bg,
)

router = APIRouter()


# ── Serialization helpers ────────────────────────────────────────────


def _line_to_dict(line: BuyPlanLine) -> dict:
    """Serialize a BuyPlanLine for API response."""
    offer = line.offer
    req = line.requirement
    buyer = line.buyer
    return {
        "id": line.id,
        "buy_plan_id": line.buy_plan_id,
        "requirement_id": line.requirement_id,
        "offer_id": line.offer_id,
        "quantity": line.quantity,
        "unit_cost": float(line.unit_cost) if line.unit_cost else None,
        "unit_sell": float(line.unit_sell) if line.unit_sell else None,
        "margin_pct": float(line.margin_pct) if line.margin_pct else None,
        "ai_score": line.ai_score,
        "buyer_id": line.buyer_id,
        "buyer_name": buyer.name if buyer else None,
        "assignment_reason": line.assignment_reason,
        "status": line.status,
        "po_number": line.po_number,
        "estimated_ship_date": str(line.estimated_ship_date) if line.estimated_ship_date else None,
        "po_confirmed_at": str(line.po_confirmed_at) if line.po_confirmed_at else None,
        "po_verified_by_id": line.po_verified_by_id,
        "po_verified_at": str(line.po_verified_at) if line.po_verified_at else None,
        "po_rejection_note": line.po_rejection_note,
        "issue_type": line.issue_type,
        "issue_note": line.issue_note,
        "sales_note": line.sales_note,
        "manager_note": line.manager_note,
        "mpn": offer.mpn if offer else (req.primary_mpn if req else None),
        "vendor_name": offer.vendor_name if offer else None,
        "manufacturer": offer.manufacturer if offer else None,
        "requirement_qty": req.target_qty if req else None,
        "lead_time": offer.lead_time if offer else None,
        "condition": offer.condition if offer else None,
    }


def _plan_to_dict(plan: BuyPlanV3) -> dict:
    """Serialize a BuyPlanV3 for API response."""
    lines = [_line_to_dict(ln) for ln in (plan.lines or [])]
    vendor_names = {ln.get("vendor_name") for ln in lines if ln.get("vendor_name")}

    quote = plan.quote
    req = plan.requisition
    customer_name = None
    if quote and quote.customer_site:
        site = quote.customer_site
        co = site.company if hasattr(site, "company") else None
        customer_name = co.name if co else site.site_name

    return {
        "id": plan.id,
        "quote_id": plan.quote_id,
        "requisition_id": plan.requisition_id,
        "sales_order_number": plan.sales_order_number,
        "customer_po_number": plan.customer_po_number,
        "quote_number": quote.quote_number if quote else None,
        "customer_name": customer_name,
        "requisition_name": req.name if req else None,
        "status": plan.status,
        "so_status": plan.so_status,
        "total_cost": float(plan.total_cost) if plan.total_cost else None,
        "total_revenue": float(plan.total_revenue) if plan.total_revenue else None,
        "total_margin_pct": float(plan.total_margin_pct) if plan.total_margin_pct else None,
        "ai_summary": plan.ai_summary,
        "ai_flags": plan.ai_flags or [],
        "auto_approved": plan.auto_approved or False,
        "approved_by_id": plan.approved_by_id,
        "approved_by_name": plan.approved_by.name if plan.approved_by else None,
        "approved_at": str(plan.approved_at) if plan.approved_at else None,
        "approval_notes": plan.approval_notes,
        "so_verified_by_id": plan.so_verified_by_id,
        "so_verified_at": str(plan.so_verified_at) if plan.so_verified_at else None,
        "so_rejection_note": plan.so_rejection_note,
        "submitted_by_id": plan.submitted_by_id,
        "submitted_by_name": plan.submitted_by.name if plan.submitted_by else None,
        "submitted_at": str(plan.submitted_at) if plan.submitted_at else None,
        "salesperson_notes": plan.salesperson_notes,
        "completed_at": str(plan.completed_at) if plan.completed_at else None,
        "case_report": plan.case_report,
        "is_stock_sale": plan.is_stock_sale or False,
        "lines": lines,
        "line_count": len(lines),
        "vendor_count": len(vendor_names),
        "created_at": str(plan.created_at) if plan.created_at else None,
    }


def _plan_to_list_item(plan: BuyPlanV3) -> dict:
    """Lightweight serialization for queue/list views."""
    quote = plan.quote
    customer_name = None
    if quote and quote.customer_site:
        site = quote.customer_site
        co = site.company if hasattr(site, "company") else None
        customer_name = co.name if co else site.site_name

    flags = plan.ai_flags or []
    return {
        "id": plan.id,
        "quote_id": plan.quote_id,
        "requisition_id": plan.requisition_id,
        "status": plan.status,
        "so_status": plan.so_status,
        "sales_order_number": plan.sales_order_number,
        "customer_name": customer_name,
        "quote_number": quote.quote_number if quote else None,
        "total_cost": float(plan.total_cost) if plan.total_cost else None,
        "total_revenue": float(plan.total_revenue) if plan.total_revenue else None,
        "total_margin_pct": float(plan.total_margin_pct) if plan.total_margin_pct else None,
        "line_count": len(plan.lines) if plan.lines else 0,
        "vendor_count": 0,
        "ai_flag_count": len(flags),
        "submitted_by_name": plan.submitted_by.name if plan.submitted_by else None,
        "approved_by_name": plan.approved_by.name if plan.approved_by else None,
        "submitted_at": str(plan.submitted_at) if plan.submitted_at else None,
        "approved_at": str(plan.approved_at) if plan.approved_at else None,
        "created_at": str(plan.created_at) if plan.created_at else None,
        "auto_approved": plan.auto_approved or False,
        "is_stock_sale": plan.is_stock_sale or False,
    }


# ── Verification Group (must be before /{plan_id} routes) ────────────


@router.get("/api/buy-plans-v3/verification-group")
async def list_verification_group(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all members of the ops verification group."""
    members = db.query(VerificationGroupMember).options(joinedload(VerificationGroupMember.user)).all()
    return {
        "items": [
            {
                "id": m.id,
                "user_id": m.user_id,
                "user_name": m.user.name if m.user else None,
                "user_email": m.user.email if m.user else None,
                "is_active": m.is_active,
                "added_at": str(m.added_at) if m.added_at else None,
            }
            for m in members
        ]
    }


@router.post("/api/buy-plans-v3/verification-group")
async def update_verification_group(
    body: VerificationGroupUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add or remove a user from the ops verification group (admin only)."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin access required")

    target = db.get(User, body.user_id)
    if not target:
        raise HTTPException(404, "User not found")

    if body.action == "add":
        existing = db.query(VerificationGroupMember).filter_by(user_id=body.user_id).first()
        if existing:
            existing.is_active = True
        else:
            db.add(VerificationGroupMember(user_id=body.user_id, is_active=True))
        db.commit()
        return {"ok": True, "action": "added", "user_id": body.user_id}

    elif body.action == "remove":
        member = db.query(VerificationGroupMember).filter_by(user_id=body.user_id).first()
        if member:
            member.is_active = False
            db.commit()
        return {"ok": True, "action": "removed", "user_id": body.user_id}


# ── Intelligence ─────────────────────────────────────────────────────


@router.get("/api/buy-plans-v3/favoritism/{user_id}")
async def get_favoritism_report(
    user_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Detect buyer favoritism patterns for a salesperson (manager/admin only)."""
    if user.role not in ("manager", "admin"):
        raise HTTPException(403, "Manager or admin role required")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    findings = detect_favoritism(user_id, db)
    return {"user_id": user_id, "user_name": target.name, "findings": findings}


@router.post("/api/buy-plans-v3/{plan_id}/case-report")
async def regenerate_case_report(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Regenerate the case report for a completed buy plan."""
    plan = (
        db.query(BuyPlanV3)
        .options(
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanV3.quote),
        )
        .filter(BuyPlanV3.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if plan.status != "completed":
        raise HTTPException(400, "Case report only available for completed plans")
    plan.case_report = generate_case_report(plan, db)
    db.commit()
    return {"ok": True, "plan_id": plan.id, "case_report": plan.case_report}


# ── Build ────────────────────────────────────────────────────────────


@router.post("/api/quotes/{quote_id}/buy-plan-v3/build")
async def build_plan_v3(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-build a draft buy plan from a won quote.

    Scores all offers, auto-splits where needed, assigns buyers.
    Returns the unsaved draft for salesperson review before submit.
    """
    try:
        plan = build_buy_plan(quote_id, db)
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.add(plan)
    db.commit()
    db.refresh(plan)
    logger.info("Buy plan V3 #{} built for quote #{}", plan.id, quote_id)
    return _plan_to_dict(plan)


# ── Get / List ───────────────────────────────────────────────────────


@router.get("/api/buy-plans-v3")
async def list_buy_plans_v3(
    status: str | None = Query(None),
    so_status: str | None = Query(None),
    buyer_id: int | None = Query(None),
    quote_id: int | None = Query(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List buy plans with optional filters for queue views."""
    q = db.query(BuyPlanV3).options(
        joinedload(BuyPlanV3.quote),
        joinedload(BuyPlanV3.submitted_by),
        joinedload(BuyPlanV3.approved_by),
        joinedload(BuyPlanV3.lines),
    )
    if status:
        q = q.filter(BuyPlanV3.status == status)
    if so_status:
        q = q.filter(BuyPlanV3.so_status == so_status)
    if buyer_id:
        q = q.join(BuyPlanLine).filter(BuyPlanLine.buyer_id == buyer_id)
    if quote_id:
        q = q.filter(BuyPlanV3.quote_id == quote_id)

    # Sales users see only their own plans
    if user.role == "sales":
        q = q.filter(BuyPlanV3.submitted_by_id == user.id)

    plans = q.order_by(BuyPlanV3.created_at.desc()).all()
    return {"items": [_plan_to_list_item(p) for p in plans], "count": len(plans)}


@router.get("/api/buy-plans-v3/{plan_id}")
async def get_buy_plan_v3(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get full buy plan detail with all lines."""
    plan = (
        db.query(BuyPlanV3)
        .options(
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlanV3.quote),
            joinedload(BuyPlanV3.requisition),
            joinedload(BuyPlanV3.submitted_by),
            joinedload(BuyPlanV3.approved_by),
        )
        .filter(BuyPlanV3.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    return _plan_to_dict(plan)


# ── Submit ───────────────────────────────────────────────────────────


@router.post("/api/buy-plans-v3/{plan_id}/submit")
async def submit_plan_v3(
    plan_id: int,
    body: BuyPlanV3Submit,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a draft buy plan with SO# and optional line edits."""
    edits = None
    if body.line_edits:
        edits = [e.model_dump() for e in body.line_edits]

    try:
        plan = submit_buy_plan(
            plan_id,
            body.sales_order_number,
            user,
            db,
            customer_po_number=body.customer_po_number,
            line_edits=edits,
            salesperson_notes=body.salesperson_notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.commit()
    logger.info("Buy plan V3 #{} submitted by {}", plan_id, user.email)
    if plan.auto_approved:
        run_v3_notify_bg(notify_v3_approved, plan.id)
    else:
        run_v3_notify_bg(notify_v3_submitted, plan.id)
    return {"ok": True, "plan_id": plan.id, "status": plan.status, "auto_approved": plan.auto_approved}


# ── Approval ─────────────────────────────────────────────────────────


@router.post("/api/buy-plans-v3/{plan_id}/approve")
async def approve_plan_v3(
    plan_id: int,
    body: BuyPlanV3Approval,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager approves or rejects a pending buy plan."""
    if user.role not in ("manager", "admin"):
        raise HTTPException(403, "Manager or admin role required")

    overrides = None
    if body.line_overrides:
        overrides = [o.model_dump() for o in body.line_overrides]

    try:
        plan = approve_buy_plan(
            plan_id,
            body.action,
            user,
            db,
            line_overrides=overrides,
            notes=body.notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.commit()
    if body.action == "approve":
        run_v3_notify_bg(notify_v3_approved, plan.id)
    else:
        run_v3_notify_bg(notify_v3_rejected, plan.id)
    return {"ok": True, "plan_id": plan.id, "status": plan.status}


# ── Resubmit ─────────────────────────────────────────────────────────


@router.post("/api/buy-plans-v3/{plan_id}/resubmit")
async def resubmit_plan_v3(
    plan_id: int,
    body: BuyPlanV3Submit,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Resubmit a rejected (draft) buy plan with corrected SO#."""
    try:
        plan = resubmit_buy_plan(
            plan_id,
            body.sales_order_number,
            user,
            db,
            customer_po_number=body.customer_po_number,
            salesperson_notes=body.salesperson_notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.commit()
    if plan.auto_approved:
        run_v3_notify_bg(notify_v3_approved, plan.id)
    else:
        run_v3_notify_bg(notify_v3_submitted, plan.id)
    return {"ok": True, "plan_id": plan.id, "status": plan.status, "auto_approved": plan.auto_approved}


# ── SO Verification ──────────────────────────────────────────────────


@router.post("/api/buy-plans-v3/{plan_id}/verify-so")
async def verify_so_v3(
    plan_id: int,
    body: SOVerificationRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies the Sales Order setup in Acctivate."""
    try:
        plan = verify_so(
            plan_id,
            body.action,
            user,
            db,
            rejection_note=body.rejection_note,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))

    db.commit()
    if body.action == "approve":
        run_v3_notify_bg(notify_v3_so_verified, plan.id)
    else:
        run_v3_notify_bg(notify_v3_so_rejected, plan.id, action=body.action)
    return {"ok": True, "plan_id": plan.id, "so_status": plan.so_status, "status": plan.status}


# ── PO Confirmation ──────────────────────────────────────────────────


@router.post("/api/buy-plans-v3/{plan_id}/lines/{line_id}/confirm-po")
async def confirm_po_v3(
    plan_id: int,
    line_id: int,
    body: POConfirmation,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO was cut in Acctivate for a line."""
    try:
        line = confirm_po(
            plan_id,
            line_id,
            body.po_number,
            body.estimated_ship_date,
            user,
            db,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.commit()
    run_v3_notify_bg(notify_v3_po_confirmed, plan_id, line_id=line.id)
    return {"ok": True, "line_id": line.id, "status": line.status, "po_number": line.po_number}


# ── PO Verification ──────────────────────────────────────────────────


@router.post("/api/buy-plans-v3/{plan_id}/lines/{line_id}/verify-po")
async def verify_po_v3(
    plan_id: int,
    line_id: int,
    body: POVerificationRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies a PO was properly entered."""
    try:
        line = verify_po(
            plan_id,
            line_id,
            body.action,
            user,
            db,
            rejection_note=body.rejection_note,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))

    db.commit()
    # Check if all lines verified → auto-complete
    updated_plan = check_completion(plan_id, db)
    if updated_plan and updated_plan.status == "completed":
        db.commit()
        run_v3_notify_bg(notify_v3_completed, plan_id)
    return {"ok": True, "line_id": line.id, "status": line.status}


# ── Flag Issue ───────────────────────────────────────────────────────


@router.post("/api/buy-plans-v3/{plan_id}/lines/{line_id}/issue")
async def flag_issue_v3(
    plan_id: int,
    line_id: int,
    body: BuyPlanLineIssue,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Buyer flags an issue on a line (sold out, price changed, etc.)."""
    try:
        line = flag_line_issue(
            plan_id,
            line_id,
            body.issue_type,
            user,
            db,
            note=body.note,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.commit()
    run_v3_notify_bg(notify_v3_issue_flagged, plan_id, line_id=line.id, issue_type=body.issue_type)
    return {"ok": True, "line_id": line.id, "status": line.status, "issue_type": line.issue_type}


# ── Offer Comparison ─────────────────────────────────────────────────


@router.get("/api/buy-plans-v3/{plan_id}/offers/{requirement_id}")
async def offer_comparison_v3(
    plan_id: int,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get all available offers for a requirement — used in manager/salesperson views."""
    plan = db.get(BuyPlanV3, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")

    # Current selections
    selected_ids = {ln.offer_id for ln in (plan.lines or []) if ln.requirement_id == requirement_id and ln.offer_id}

    offers = db.query(Offer).filter(Offer.requirement_id == requirement_id, Offer.status == "active").all()
    items = []
    for o in offers:
        items.append(
            {
                "offer_id": o.id,
                "vendor_name": o.vendor_name,
                "unit_price": float(o.unit_price) if o.unit_price else None,
                "qty_available": o.qty_available,
                "lead_time": o.lead_time,
                "condition": o.condition,
                "date_code": o.date_code,
                "packaging": o.packaging,
                "is_selected": o.id in selected_ids,
                "created_at": str(o.created_at) if o.created_at else None,
            }
        )

    return {
        "requirement_id": requirement_id,
        "mpn": requirement.primary_mpn,
        "target_qty": requirement.target_qty,
        "selected_offer_ids": list(selected_ids),
        "offers": items,
    }
