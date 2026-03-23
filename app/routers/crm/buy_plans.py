"""buy_plans.py — Buy Plan V1 Compatibility Layer.

Thin adapter that reads from the V3/V4 BuyPlan model (table buy_plans_v3)
and returns V1-shaped responses with mapped status names. Mutation endpoints
for workflow transitions (build, submit, approve, resubmit, reset-to-draft)
return 410 Gone, directing consumers to the V3 API.

Operational endpoints (verify-so, confirm-po, verify-po, flag-issue),
token-based approval, verification group, offer comparison, favoritism,
and case report endpoints remain fully functional.

V1→V3 Status Mapping:
  V1 pending_approval → V3 pending
  V1 approved         → V3 active
  V1 po_entered       → V3 active (line has po_number)
  V1 po_confirmed     → V3 active (line status pending_verify or verified)
  V1 complete         → V3 completed
  V1 rejected         → V3 draft (with rejection note)
  V1 draft            → V3 draft
  V1 cancelled        → V3 cancelled

Called by: frontend (legacy), HTMX views
Depends on: models/buy_plan.py, services/buyplan_service.py, schemas/buy_plan.py
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload, selectinload

from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_buyer, require_user
from ...models import Offer, Requirement, User
from ...models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    VerificationGroupMember,
)
from ...rate_limit import limiter
from ...schemas.buy_plan import (
    BuyPlanLineIssue,
    BuyPlanTokenApproval,
    BuyPlanTokenReject,
    POConfirmation,
    POVerificationRequest,
    SOVerificationRequest,
    VerificationGroupUpdate,
)
from ...services.buyplan_notifications import (
    notify_completed,
    notify_po_confirmed,
    notify_so_rejected,
    notify_so_verified,
    run_notify_bg,
)
from ...services.buyplan_service import (
    check_completion,
    detect_favoritism,
    generate_case_report,
)
from ...services.buyplan_service import (
    confirm_po as svc_confirm_po,
)
from ...services.buyplan_service import (
    flag_line_issue as svc_flag_line_issue,
)
from ...services.buyplan_service import (
    verify_po as svc_verify_po,
)
from ...services.buyplan_service import (
    verify_so as svc_verify_so,
)

router = APIRouter()


# ── V3→V1 Status Mapping ──────────────────────────────────────────────

_V3_TO_V1_STATUS = {
    "draft": "draft",
    "pending": "pending_approval",
    "active": "approved",
    "halted": "halted",
    "completed": "complete",
    "cancelled": "cancelled",
}


def _map_v3_status_to_v1(plan: BuyPlan) -> str:
    """Map a V3 plan status to V1 status, considering line-level context.

    Special V1 statuses:
      - po_entered: plan is active AND at least one line has po_number but not yet verified
      - po_confirmed: plan is active AND all lines with POs are pending_verify or verified
      - rejected: plan is draft AND has a cancellation_reason (rejection note)
    """
    v3_status = plan.status

    if v3_status == "draft" and plan.cancellation_reason:
        return "rejected"

    if v3_status == "active":
        lines = plan.lines or []
        lines_with_po = [ln for ln in lines if ln.po_number]
        if lines_with_po:
            all_confirmed = all(
                ln.status in (BuyPlanLineStatus.PENDING_VERIFY.value, BuyPlanLineStatus.VERIFIED.value)
                for ln in lines_with_po
            )
            if all_confirmed:
                return "po_confirmed"
            return "po_entered"

    return _V3_TO_V1_STATUS.get(v3_status, v3_status)


# ── V3→V1 Serialization Helpers ───────────────────────────────────────


def _line_to_v1_dict(line: BuyPlanLine) -> dict:
    """Serialize a BuyPlanLine for V1-shaped API response."""
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


def _v3_to_v1_dict(plan: BuyPlan) -> dict:
    """Convert a V3 BuyPlan to a V1-shaped response dict.

    Maps V3 statuses to V1 status names and generates line_items from BuyPlanLine rows.
    """
    lines = [_line_to_v1_dict(ln) for ln in (plan.lines or [])]
    vendor_names = {ln.get("vendor_name") for ln in lines if ln.get("vendor_name")}

    quote = plan.quote
    req = plan.requisition
    customer_name = None
    if quote and quote.customer_site:
        site = quote.customer_site
        co = site.company if hasattr(site, "company") else None
        customer_name = co.name if co else site.site_name

    v1_status = _map_v3_status_to_v1(plan)

    return {
        "id": plan.id,
        "quote_id": plan.quote_id,
        "requisition_id": plan.requisition_id,
        "sales_order_number": plan.sales_order_number,
        "customer_po_number": plan.customer_po_number,
        "quote_number": quote.quote_number if quote else None,
        "customer_name": customer_name,
        "requisition_name": req.name if req else None,
        "status": v1_status,
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
        "line_items": lines,  # V1 compat alias
        "line_count": len(lines),
        "vendor_count": len(vendor_names),
        "created_at": str(plan.created_at) if plan.created_at else None,
    }


def _v3_to_v1_list_item(plan: BuyPlan) -> dict:
    """Lightweight V1-shaped serialization for queue/list views."""
    quote = plan.quote
    customer_name = None
    if quote and quote.customer_site:
        site = quote.customer_site
        co = site.company if hasattr(site, "company") else None
        customer_name = co.name if co else site.site_name

    v1_status = _map_v3_status_to_v1(plan)
    flags = plan.ai_flags or []
    lines = plan.lines or []
    vendor_names = {ln.offer.vendor_name.lower() for ln in lines if ln.offer and ln.offer.vendor_name}

    return {
        "id": plan.id,
        "quote_id": plan.quote_id,
        "requisition_id": plan.requisition_id,
        "status": v1_status,
        "so_status": plan.so_status,
        "sales_order_number": plan.sales_order_number,
        "customer_name": customer_name,
        "quote_number": quote.quote_number if quote else None,
        "total_cost": float(plan.total_cost) if plan.total_cost else None,
        "total_revenue": float(plan.total_revenue) if plan.total_revenue else None,
        "total_margin_pct": float(plan.total_margin_pct) if plan.total_margin_pct else None,
        "line_count": len(lines),
        "vendor_count": len(vendor_names),
        "ai_flag_count": len(flags),
        "submitted_by_name": plan.submitted_by.name if plan.submitted_by else None,
        "approved_by_name": plan.approved_by.name if plan.approved_by else None,
        "submitted_at": str(plan.submitted_at) if plan.submitted_at else None,
        "approved_at": str(plan.approved_at) if plan.approved_at else None,
        "created_at": str(plan.created_at) if plan.created_at else None,
        "auto_approved": plan.auto_approved or False,
        "is_stock_sale": plan.is_stock_sale or False,
    }


# ── Deprecated Mutation Helper ─────────────────────────────────────────


def _gone_response(endpoint: str, v3_path: str) -> JSONResponse:
    """Return a 410 Gone response directing consumers to the V3 API."""
    logger.info("V1 buy-plan mutation '{}' → 410 Gone, use V3: {}", endpoint, v3_path)
    return JSONResponse(
        status_code=410,
        content={
            "error": f"V1 endpoint '{endpoint}' is deprecated. Use V3 API: {v3_path}",
            "status_code": 410,
            "v3_endpoint": v3_path,
        },
    )


# ── Verification Group (must be before /{plan_id} routes) ────────────


@router.get("/api/buy-plans/verification-group")
async def list_verification_group(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all members of the ops verification group."""
    members = db.query(VerificationGroupMember).options(joinedload(VerificationGroupMember.user)).limit(1000).all()
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


@router.post("/api/buy-plans/verification-group")
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


# ── Token-based Approval (public, no auth) ──────────────────────────


def _to_approval_dict(plan: BuyPlan) -> dict:
    """Return only the minimum fields an approver needs to make a decision.

    Deliberately omits sensitive commercial data (ai_summary, ai_flags,
    total_margin_pct, case_report, salesperson_notes, full line items) so that a leaked
    token URL does not expose financial details.
    """
    lines = plan.lines or []
    vendor_names = sorted({ln.offer.vendor_name for ln in lines if ln.offer and ln.offer.vendor_name})

    requested_by_name = plan.submitted_by.name if plan.submitted_by else None
    v1_status = _map_v3_status_to_v1(plan)

    return {
        "id": plan.id,
        "status": v1_status,
        "total_cost": float(plan.total_cost) if plan.total_cost else None,
        "total_revenue": float(plan.total_revenue) if plan.total_revenue else None,
        "line_count": len(lines),
        "vendor_names": vendor_names,
        "created_at": str(plan.created_at) if plan.created_at else None,
        "requested_by_name": requested_by_name,
    }


def _token_expired(expires_at) -> bool:
    """Timezone-safe token expiration check (SQLite returns naive datetimes)."""
    now = datetime.now(timezone.utc)
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at is not None and expires_at < now


@router.get("/api/buy-plans/token/{token}")
@limiter.limit("10/minute")
async def get_plan_by_token(token: str, request: Request, db: Session = Depends(get_db)):
    """Public endpoint — no auth required.

    Get plan details by approval token. Returns V1-shaped response.
    """
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid token")
    if plan.token_expires_at and _token_expired(plan.token_expires_at):
        raise HTTPException(410, "Token expired")
    return _to_approval_dict(plan)


@router.put("/api/buy-plans/token/{token}/approve")
@limiter.limit("5/minute")
async def approve_by_token(token: str, request: Request, body: BuyPlanTokenApproval, db: Session = Depends(get_db)):
    """Public token-based approval.

    Sets SO number and activates plan. Returns V1-shaped response.
    """
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid token")
    if plan.token_expires_at and _token_expired(plan.token_expires_at):
        raise HTTPException(410, "Token expired")
    if plan.status != BuyPlanStatus.PENDING.value:
        raise HTTPException(400, f"Cannot approve plan in '{plan.status}' status")
    plan.status = BuyPlanStatus.ACTIVE.value
    plan.sales_order_number = body.sales_order_number
    plan.approval_notes = body.notes
    plan.approved_at = datetime.now(timezone.utc)
    plan.approval_token = None  # Invalidate token after use
    # Stock sale fast-track: if is_stock_sale, auto-complete
    if plan.is_stock_sale:
        plan.status = BuyPlanStatus.COMPLETED.value
        plan.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(plan)
    return _v3_to_v1_dict(plan)


@router.put("/api/buy-plans/token/{token}/reject")
@limiter.limit("5/minute")
async def reject_by_token(token: str, request: Request, body: BuyPlanTokenReject, db: Session = Depends(get_db)):
    """Public token-based rejection.

    Resets plan to draft. Returns V1-shaped response.
    """
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid token")
    if plan.token_expires_at and _token_expired(plan.token_expires_at):
        raise HTTPException(410, "Token expired")
    if plan.status != BuyPlanStatus.PENDING.value:
        raise HTTPException(400, f"Cannot reject plan in '{plan.status}' status")
    plan.status = BuyPlanStatus.DRAFT.value
    plan.cancellation_reason = body.reason
    plan.approval_token = None  # Invalidate token
    db.commit()
    db.refresh(plan)
    return _v3_to_v1_dict(plan)


# ── Intelligence ─────────────────────────────────────────────────────


@router.get("/api/buy-plans/favoritism/{user_id}")
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


@router.post("/api/buy-plans/{plan_id}/case-report")
async def regenerate_case_report(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Regenerate the case report for a completed buy plan."""
    plan = (
        db.query(BuyPlan)
        .options(
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.quote),
        )
        .filter(BuyPlan.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if plan.status != BuyPlanStatus.COMPLETED.value:
        raise HTTPException(400, "Case report only available for completed plans")
    plan.case_report = generate_case_report(plan, db)
    db.commit()
    return {"ok": True, "plan_id": plan.id, "case_report": plan.case_report}


# ── Deprecated Mutation: Build ────────────────────────────────────────


@router.post("/api/quotes/{quote_id}/buy-plan/build")
async def build_plan(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED — Use V3 API POST /api/v3/quotes/{quote_id}/buy-plan/build."""
    return _gone_response("build", f"/api/v3/quotes/{quote_id}/buy-plan/build")


# ── Get / List (V1 read adapter) ─────────────────────────────────────


@router.get("/api/buy-plans")
async def list_buy_plans(
    status: str | None = Query(None),
    so_status: str | None = Query(None),
    buyer_id: int | None = Query(None),
    quote_id: int | None = Query(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List buy plans with V1-shaped status names.

    Accepts both V1 and V3 status filter values.
    """
    q = db.query(BuyPlan).options(
        joinedload(BuyPlan.quote),
        joinedload(BuyPlan.submitted_by),
        joinedload(BuyPlan.approved_by),
        selectinload(BuyPlan.lines)
        .load_only(
            BuyPlanLine.id,
            BuyPlanLine.status,
            BuyPlanLine.po_number,
            BuyPlanLine.offer_id,
            BuyPlanLine.buy_plan_id,
        )
        .joinedload(BuyPlanLine.offer)
        .load_only(Offer.id, Offer.vendor_name),
    )

    # Accept both V1 and V3 status filter values
    _v1_to_v3_status = {
        "pending_approval": "pending",
        "approved": "active",
        "complete": "completed",
        "rejected": "draft",
    }
    _valid_v3_statuses = {"draft", "pending", "active", "halted", "completed", "cancelled"}
    _valid_so_statuses = {"pending", "approved", "rejected"}

    if status:
        # Translate V1 status filter to V3 for querying
        v3_status = _v1_to_v3_status.get(status, status)
        if v3_status not in _valid_v3_statuses:
            raise HTTPException(
                400,
                f"Invalid status: {status}. Must be one of: {', '.join(sorted(_valid_v3_statuses | set(_v1_to_v3_status.keys())))}",
            )
        q = q.filter(BuyPlan.status == v3_status)
    if so_status and so_status not in _valid_so_statuses:
        raise HTTPException(
            400, f"Invalid so_status: {so_status}. Must be one of: {', '.join(sorted(_valid_so_statuses))}"
        )

    if so_status:
        q = q.filter(BuyPlan.so_status == so_status)
    if buyer_id:
        q = q.join(BuyPlanLine).filter(BuyPlanLine.buyer_id == buyer_id)
    if quote_id:
        q = q.filter(BuyPlan.quote_id == quote_id)

    # Sales users see only their own plans
    if user.role == "sales":
        q = q.filter(BuyPlan.submitted_by_id == user.id)

    plans = q.order_by(BuyPlan.created_at.desc()).limit(500).all()
    return {"items": [_v3_to_v1_list_item(p) for p in plans], "count": len(plans)}


@router.get("/api/buy-plans/{plan_id}")
async def get_buy_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get full buy plan detail with V1-shaped status names."""
    plan = (
        db.query(BuyPlan)
        .options(
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
        )
        .filter(BuyPlan.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    return _v3_to_v1_dict(plan)


# ── Deprecated Mutations: Submit / Approve / Resubmit / Reset ────────


@router.post("/api/buy-plans/{plan_id}/submit")
async def submit_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED — Use V3 API POST /api/v3/buy-plans/{plan_id}/submit."""
    return _gone_response("submit", f"/api/v3/buy-plans/{plan_id}/submit")


@router.post("/api/buy-plans/{plan_id}/approve")
async def approve_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED — Use V3 API POST /api/v3/buy-plans/{plan_id}/approve."""
    return _gone_response("approve", f"/api/v3/buy-plans/{plan_id}/approve")


@router.post("/api/buy-plans/{plan_id}/resubmit")
async def resubmit_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED — Use V3 API POST /api/v3/buy-plans/{plan_id}/resubmit."""
    return _gone_response("resubmit", f"/api/v3/buy-plans/{plan_id}/resubmit")


@router.post("/api/buy-plans/{plan_id}/reset-to-draft")
async def reset_plan_to_draft(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED — Use V3 API POST /api/v3/buy-plans/{plan_id}/reset-to-draft."""
    return _gone_response("reset-to-draft", f"/api/v3/buy-plans/{plan_id}/reset-to-draft")


# ── SO Verification (kept — operational) ─────────────────────────────


@router.post("/api/buy-plans/{plan_id}/verify-so")
async def verify_so(
    plan_id: int,
    body: SOVerificationRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies the Sales Order setup in Acctivate."""
    try:
        plan = svc_verify_so(
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
        await run_notify_bg(notify_so_verified, plan.id)
    else:
        await run_notify_bg(notify_so_rejected, plan.id, action=body.action)
    return {"ok": True, "plan_id": plan.id, "so_status": plan.so_status, "status": plan.status}


# ── PO Confirmation (kept — operational) ─────────────────────────────


@router.post("/api/buy-plans/{plan_id}/lines/{line_id}/confirm-po")
async def confirm_po(
    plan_id: int,
    line_id: int,
    body: POConfirmation,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO was cut in Acctivate for a line."""
    try:
        line = svc_confirm_po(
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
    await run_notify_bg(notify_po_confirmed, plan_id, line_id=line.id)
    return {"ok": True, "line_id": line.id, "status": line.status, "po_number": line.po_number}


# ── PO Verification (kept — operational) ─────────────────────────────


@router.post("/api/buy-plans/{plan_id}/lines/{line_id}/verify-po")
async def verify_po(
    plan_id: int,
    line_id: int,
    body: POVerificationRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies a PO was properly entered."""
    try:
        line = svc_verify_po(
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
    if updated_plan and updated_plan.status == BuyPlanStatus.COMPLETED.value:
        db.commit()
        await run_notify_bg(notify_completed, plan_id)
    return {"ok": True, "line_id": line.id, "status": line.status}


# ── Flag Issue (kept — operational) ──────────────────────────────────


@router.post("/api/buy-plans/{plan_id}/lines/{line_id}/issue")
async def flag_issue(
    plan_id: int,
    line_id: int,
    body: BuyPlanLineIssue,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Buyer flags an issue on a line (sold out, price changed, etc.)."""
    try:
        line = svc_flag_line_issue(
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
    return {"ok": True, "line_id": line.id, "status": line.status, "issue_type": line.issue_type}


# ── PO Verification Scanning (kept — operational) ───────────────────


@router.get("/api/buy-plans/{plan_id}/verify-po")
async def verify_po_scan(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Scan buyer's Outlook sent folder for PO emails matching each line.

    Uses Graph API to search each buyer's Sent Items for the PO number. Returns per-PO
    verification results with recipient and sent timestamp.
    """
    plan = (
        db.query(BuyPlan)
        .options(
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
        )
        .filter(BuyPlan.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(404, "Plan not found")
    from ...services.buyplan_service import verify_po_sent_v3

    results = await verify_po_sent_v3(plan, db)
    db.commit()
    return {"plan_id": plan.id, "verifications": results}


# ── Offer Comparison (kept — operational) ────────────────────────────


@router.get("/api/buy-plans/{plan_id}/offers/{requirement_id}")
async def offer_comparison(
    plan_id: int,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get all available offers for a requirement — used in manager/salesperson
    views."""
    plan = db.get(BuyPlan, plan_id)
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
