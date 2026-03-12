"""buy_plans.py — V1 Buy Plan CRUD (legacy, deprecated in favor of V3).

V1 buy plans use JSON line_items and do not enforce structured state machines.
Disabled by default via buy_plan_v1_enabled feature flag.

Called by: routers/crm/__init__.py
Depends on: models, schemas, config
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ...config import settings
from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_user
from ...models import (
    BuyPlan,
    CustomerSite,
    Offer,
    Quote,
    Requisition,
    User,
    VendorCard,
)
from ...schemas.crm import (
    BuyPlanApprove,
    BuyPlanCancel,
    BuyPlanPOBulk,
    BuyPlanPOEntry,
    BuyPlanReject,
    BuyPlanResubmit,
    BuyPlanSubmit,
)

router = APIRouter()


def _require_v1_enabled():
    """Dependency that blocks V1 buy plan mutations when disabled."""
    if not settings.buy_plan_v1_enabled:
        raise HTTPException(410, "V1 buy plans are disabled. Use /api/v3/buy-plans endpoints.")


# ── Buy Plans ────────────────────────────────────────────────────────────


def _buyplan_to_dict(bp: BuyPlan, db=None) -> dict:
    # Gather deal context from quote/requisition
    customer_name = ""
    quote_number = ""
    quote_subtotal = None
    if bp.quote:
        quote_number = bp.quote.quote_number or ""
        quote_subtotal = float(bp.quote.subtotal) if bp.quote.subtotal else None
        if bp.quote.customer_site and bp.quote.customer_site.company:
            customer_name = f"{bp.quote.customer_site.company.name} — {bp.quote.customer_site.site_name}"

    # Compute margin totals from line items
    total_cost = 0.0
    total_revenue = 0.0
    for item in bp.line_items or []:
        plan_qty = item.get("plan_qty") or item.get("qty") or 0
        cost = plan_qty * (item.get("cost_price") or 0)
        sell = plan_qty * (item.get("sell_price") or 0)
        total_cost += cost
        total_revenue += sell
    total_profit = total_revenue - total_cost
    overall_margin_pct = round((total_profit / total_revenue) * 100, 2) if total_revenue else 0

    # Enrich line items with vendor scores
    enriched_items = list(bp.line_items or [])
    if db and enriched_items:
        vendor_names = {(item.get("vendor_name") or "").strip().lower() for item in enriched_items}
        vendor_names.discard("")
        if vendor_names:
            cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(vendor_names)).all()
            score_map = {
                c.normalized_name: {
                    "vendor_score": c.vendor_score,
                    "is_new_vendor": c.is_new_vendor if c.is_new_vendor is not None else True,
                }
                for c in cards
            }
            for item in enriched_items:
                vname = (item.get("vendor_name") or "").strip().lower()
                info = score_map.get(vname, {})
                item["vendor_score"] = info.get("vendor_score")
                item["is_new_vendor"] = info.get("is_new_vendor", True)

    return {
        "id": bp.id,
        "requisition_id": bp.requisition_id,
        "requisition_name": bp.requisition.name if bp.requisition else None,
        "quote_id": bp.quote_id,
        "quote_number": quote_number,
        "quote_subtotal": quote_subtotal,
        "customer_name": customer_name,
        "status": bp.status,
        "line_items": enriched_items,
        "is_stock_sale": bp.is_stock_sale or False,
        "total_cost": round(total_cost, 2),
        "total_revenue": round(total_revenue, 2),
        "total_profit": round(total_profit, 2),
        "overall_margin_pct": overall_margin_pct,
        "sales_order_number": bp.sales_order_number,
        "salesperson_notes": bp.salesperson_notes,
        "manager_notes": bp.manager_notes,
        "rejection_reason": bp.rejection_reason,
        "submitted_by": bp.submitted_by.name if bp.submitted_by else None,
        "submitted_by_id": bp.submitted_by_id,
        "approved_by": bp.approved_by.name if bp.approved_by else None,
        "approved_by_id": bp.approved_by_id,
        "rejected_by": bp.approved_by.name if (bp.approved_by and bp.status == "rejected") else None,
        "rejected_by_id": bp.approved_by_id if bp.status == "rejected" else None,
        "submitted_at": bp.submitted_at.isoformat() if bp.submitted_at else None,
        "approved_at": bp.approved_at.isoformat() if bp.approved_at else None,
        "rejected_at": bp.rejected_at.isoformat() if bp.rejected_at else None,
        "completed_at": bp.completed_at.isoformat() if bp.completed_at else None,
        "completed_by": bp.completed_by.name if bp.completed_by else None,
        "cancelled_at": bp.cancelled_at.isoformat() if bp.cancelled_at else None,
        "cancelled_by": bp.cancelled_by.name if bp.cancelled_by else None,
        "cancellation_reason": bp.cancellation_reason,
    }


def _build_buy_plan_line_items(quote: Quote, body: BuyPlanSubmit, db: Session):
    """Build line_items for a buy plan from quote + submit body. Shared by draft and submit."""
    offer_ids = body.offer_ids
    offers = db.query(Offer).options(joinedload(Offer.entered_by)).filter(Offer.id.in_(offer_ids)).all()
    offers_map = {o.id: o for o in offers}
    quote_items_by_offer = {item["offer_id"]: item for item in (quote.line_items or []) if item.get("offer_id")}
    plan_qtys = body.plan_qtys or {}
    line_items = []
    for oid in offer_ids:
        o = offers_map.get(oid)
        if not o:
            continue
        qi = quote_items_by_offer.get(oid, {})
        qty_available = o.qty_available or 0
        plan_qty = plan_qtys.get(str(o.id), plan_qtys.get(o.id, qi.get("qty") or qty_available))
        line_items.append(
            {
                "offer_id": o.id,
                "mpn": qi.get("mpn") or o.mpn,
                "vendor_name": qi.get("vendor_name") or o.vendor_name,
                "manufacturer": qi.get("manufacturer") or o.manufacturer,
                "qty": qi.get("qty") or qty_available,
                "plan_qty": int(plan_qty) if plan_qty else qty_available,
                "cost_price": qi.get("cost_price") or (float(o.unit_price) if o.unit_price else 0),
                "sell_price": qi.get("sell_price"),
                "lead_time": qi.get("lead_time") or o.lead_time,
                "condition": qi.get("condition") or o.condition,
                "date_code": qi.get("date_code") or o.date_code,
                "packaging": qi.get("packaging") or o.packaging,
                "entered_by_id": o.entered_by_id,
                "entered_by_name": o.entered_by.name if o.entered_by else None,
                "po_number": None,
                "po_entered_at": None,
                "po_sent_at": None,
                "po_recipient": None,
                "po_verified": False,
            }
        )
    return line_items, offers


@router.post("/api/quotes/{quote_id}/buy-plan/draft")
async def create_buy_plan_draft(
    quote_id: int,
    body: BuyPlanSubmit,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a buy plan in Draft. Sales can later use 'Ready to send' (PUT submit) to move to Pending."""
    if not settings.buy_plan_v1_enabled:
        raise HTTPException(410, "Buy Plan V1 is deprecated. Use V3 buy plans.")
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    if not body.offer_ids:
        raise HTTPException(400, "At least one offer must be selected")
    line_items, _ = _build_buy_plan_line_items(quote, body, db)
    if not line_items:
        raise HTTPException(400, "No valid offers for selected IDs")
    stock_names = settings.stock_sale_vendor_names
    is_stock = all((item.get("vendor_name") or "").strip().lower() in stock_names for item in line_items)
    plan = BuyPlan(
        requisition_id=quote.requisition_id,
        quote_id=quote_id,
        status="draft",
        salesperson_notes=(body.salesperson_notes or "").strip() or None,
        line_items=line_items,
        submitted_by_id=user.id,
        is_stock_sale=is_stock,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    from ...services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(db, user.id, plan, "buyplan_created", "draft created")
    return {"ok": True, "buy_plan_id": plan.id, "status": "draft"}


@router.put("/api/buy-plans/{plan_id}/submit", dependencies=[Depends(_require_v1_enabled)])
async def submit_draft_buy_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Move Draft -> Pending (Ready to send). Allowed: plan creator (sales) or admin/manager."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if plan.status != "draft":
        raise HTTPException(400, f"Can only submit draft plans, current: {plan.status}")
    is_mgr = _is_admin(user) or user.role == "manager"
    is_creator = plan.submitted_by_id == user.id
    if not is_mgr and not is_creator:
        raise HTTPException(403, "Only the plan creator or admin/manager can submit for approval")
    import secrets
    from datetime import timedelta

    plan.status = "pending_approval"
    plan.approval_token = secrets.token_urlsafe(32)
    plan.token_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    plan.submitted_at = datetime.now(timezone.utc)
    quote = db.get(Quote, plan.quote_id)
    req = db.get(Requisition, plan.requisition_id) if plan.requisition_id else None
    offer_ids = [item.get("offer_id") for item in (plan.line_items or []) if item.get("offer_id")]
    if quote:
        quote.result = "won"
        quote.result_at = datetime.now(timezone.utc)
        quote.status = "won"
        quote.won_revenue = quote.subtotal
    if req:
        req.status = "won"
    if offer_ids:
        for o in db.query(Offer).filter(Offer.id.in_(offer_ids)).all():
            o.status = "won"
    if quote and offer_ids:
        offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
        _record_purchase_history(db, req, quote, offers)
    from ...services.buyplan_service import log_buyplan_activity, notify_buyplan_submitted, run_buyplan_bg

    log_buyplan_activity(db, user.id, plan, "buyplan_submitted", "submitted for approval")
    db.commit()
    run_buyplan_bg(notify_buyplan_submitted, plan.id)
    return {"ok": True, "status": "pending_approval", "buy_plan_id": plan.id}


@router.post("/api/quotes/{quote_id}/buy-plan")
async def submit_buy_plan(
    quote_id: int,
    body: BuyPlanSubmit,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a buy plan when marking a quote as Won (creates plan in Pending in one step)."""
    if not settings.buy_plan_v1_enabled:
        raise HTTPException(410, "Buy Plan V1 is deprecated. Use V3 buy plans.")
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    offer_ids = body.offer_ids
    if not offer_ids:
        raise HTTPException(400, "At least one offer must be selected")
    salesperson_notes = (body.salesperson_notes or "").strip()
    line_items, offers = _build_buy_plan_line_items(quote, body, db)
    if not line_items:
        raise HTTPException(400, "No valid offers for selected IDs")

    import secrets

    stock_names = settings.stock_sale_vendor_names
    is_stock = bool(line_items) and all(
        (item.get("vendor_name") or "").strip().lower() in stock_names for item in line_items
    )
    from datetime import timedelta

    plan = BuyPlan(
        requisition_id=quote.requisition_id,
        quote_id=quote_id,
        status="pending_approval",
        salesperson_notes=salesperson_notes or None,
        line_items=line_items,
        submitted_by_id=user.id,
        submitted_at=datetime.now(timezone.utc),
        approval_token=secrets.token_urlsafe(32),
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_stock_sale=is_stock,
    )
    db.add(plan)

    quote.result = "won"
    quote.result_at = datetime.now(timezone.utc)
    quote.status = "won"
    quote.won_revenue = quote.subtotal
    req = db.get(Requisition, quote.requisition_id)
    if req:
        req.status = "won"
    for o in offers:
        o.status = "won"
    _record_purchase_history(db, req, quote, offers)
    from ...services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(db, user.id, plan, "buyplan_submitted", f"submitted for quote #{quote_id}")
    db.commit()
    from ...services.buyplan_service import notify_buyplan_submitted, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_submitted, plan.id)
    return {
        "ok": True,
        "buy_plan_id": plan.id,
        "status": "pending_approval",
        "req_status": req.status if req else None,
        "status_changed": True,
    }


@router.get("/api/buy-plans")
async def list_buy_plans(
    status: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List buy plans. Admins see all, sales see own, buyers see their offers."""
    query = (
        db.query(BuyPlan)
        .options(
            joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
            joinedload(BuyPlan.completed_by),
            joinedload(BuyPlan.cancelled_by),
        )
        .order_by(BuyPlan.created_at.desc())
    )
    if status:
        # Workflow: Draft | Pending | Approved | Completed. "Approved" includes po_entered, po_confirmed.
        if status == "approved":
            query = query.filter(BuyPlan.status.in_(["approved", "po_entered", "po_confirmed"]))
        else:
            query = query.filter(BuyPlan.status == status)
    if not _is_admin(user):
        if user.role in ("sales", "trader"):
            query = query.filter(BuyPlan.submitted_by_id == user.id)
        # Buyers see all (they need to check which plans have their offers)
    plans = query.limit(200).all()
    return [_buyplan_to_dict(p, db) for p in plans]


@router.get("/api/buy-plans/token/{token}")
async def get_buyplan_by_token(
    token: str,
    db: Session = Depends(get_db),
):
    """Public: get buy plan by approval token (no auth required)."""
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid or expired token")
    if plan.token_expires_at and plan.token_expires_at < datetime.now(timezone.utc):
        raise HTTPException(410, "Token has expired")
    return _buyplan_to_dict(plan, db)


@router.put("/api/buy-plans/token/{token}/approve", dependencies=[Depends(_require_v1_enabled)])
async def approve_buyplan_by_token(
    token: str,
    body: BuyPlanApprove,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public: approve buy plan via token link in email."""
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid or expired token")
    if plan.token_expires_at and plan.token_expires_at < datetime.now(timezone.utc):
        raise HTTPException(410, "Token has expired")
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot approve plan in status: {plan.status}")

    so_number = body.sales_order_number.strip()
    if not so_number:
        raise HTTPException(400, "Acctivate Sales Order # is required")

    plan.sales_order_number = so_number
    if body.manager_notes is not None:
        plan.manager_notes = body.manager_notes

    plan.status = "approved"
    plan.approved_at = datetime.now(timezone.utc)
    plan.approval_token = None  # Invalidate after use
    # approved_by_id stays None (token-based, no logged-in user)

    from ...services.buyplan_service import log_buyplan_activity

    # Stock sale fast-track: approve → complete (no PO required)
    if plan.is_stock_sale:
        plan.status = "complete"
        plan.completed_at = datetime.now(timezone.utc)
        # completed_by_id stays None (token-based)
        log_buyplan_activity(
            db,
            plan.submitted_by_id,
            plan,
            "buyplan_approved",
            "stock sale approved + auto-completed via email token",
        )
    else:
        log_buyplan_activity(db, plan.submitted_by_id, plan, "buyplan_approved", "approved via email token")
    db.commit()

    from ...services.buyplan_service import notify_buyplan_approved, run_buyplan_bg

    if plan.is_stock_sale:
        from ...services.buyplan_service import notify_stock_sale_approved

        run_buyplan_bg(notify_stock_sale_approved, plan.id)
    else:
        run_buyplan_bg(notify_buyplan_approved, plan.id)
    return {"ok": True, "status": plan.status}


@router.put("/api/buy-plans/token/{token}/reject", dependencies=[Depends(_require_v1_enabled)])
async def reject_buyplan_by_token(
    token: str,
    body: BuyPlanReject,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public: reject buy plan via token link in email."""
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid or expired token")
    if plan.token_expires_at and plan.token_expires_at < datetime.now(timezone.utc):
        raise HTTPException(410, "Token has expired")
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot reject plan in status: {plan.status}")

    plan.rejection_reason = body.reason
    plan.status = "rejected"
    plan.rejected_at = datetime.now(timezone.utc)
    plan.approval_token = None  # Invalidate after use

    from ...services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(db, plan.submitted_by_id, plan, "buyplan_rejected", "rejected via email token")
    db.commit()

    from ...services.buyplan_service import notify_buyplan_rejected, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_rejected, plan.id)
    return {"ok": True, "status": "rejected"}


@router.get("/api/buy-plans/{plan_id}")
async def get_buy_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if not _is_admin(user) and user.role not in ("manager", "buyer"):
        if plan.submitted_by_id != user.id:
            raise HTTPException(403, "You can only view your own buy plans")
    return _buyplan_to_dict(plan, db)


@router.put("/api/buy-plans/{plan_id}/approve", dependencies=[Depends(_require_v1_enabled)])
async def approve_buy_plan(
    plan_id: int,
    body: BuyPlanApprove,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/admin approves the buy plan."""
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "Manager or admin approval required")
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot approve plan in status: {plan.status}")

    so_number = body.sales_order_number.strip()
    if not so_number:
        raise HTTPException(400, "Acctivate Sales Order # is required")
    plan.sales_order_number = so_number

    if body.line_items is not None:
        plan.line_items = body.line_items
    if body.manager_notes is not None:
        plan.manager_notes = body.manager_notes

    plan.status = "approved"
    plan.approved_by_id = user.id
    plan.approved_at = datetime.now(timezone.utc)

    from ...services.buyplan_service import log_buyplan_activity

    # Stock sale fast-track: approve → complete (no PO required)
    if plan.is_stock_sale:
        plan.status = "complete"
        plan.completed_at = datetime.now(timezone.utc)
        plan.completed_by_id = user.id
        log_buyplan_activity(
            db,
            user.id,
            plan,
            "buyplan_approved",
            f"stock sale approved + auto-completed with SO# {so_number}",
        )
    else:
        log_buyplan_activity(db, user.id, plan, "buyplan_approved", f"approved with SO# {so_number}")
    db.commit()

    from ...services.buyplan_service import notify_buyplan_approved, run_buyplan_bg

    if plan.is_stock_sale:
        from ...services.buyplan_service import notify_stock_sale_approved

        run_buyplan_bg(notify_stock_sale_approved, plan.id)
    else:
        run_buyplan_bg(notify_buyplan_approved, plan.id)

    return {"ok": True, "status": plan.status}


@router.put("/api/buy-plans/{plan_id}/reject", dependencies=[Depends(_require_v1_enabled)])
async def reject_buy_plan(
    plan_id: int,
    body: BuyPlanReject,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/admin rejects the buy plan."""
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "Manager or admin rejection required")
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot reject plan in status: {plan.status}")

    plan.rejection_reason = body.reason
    plan.status = "rejected"
    plan.approved_by_id = user.id  # reuse field for who acted
    plan.rejected_at = datetime.now(timezone.utc)

    from ...services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(db, user.id, plan, "buyplan_rejected", plan.rejection_reason or "no reason")
    db.commit()

    from ...services.buyplan_service import notify_buyplan_rejected, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_rejected, plan.id)

    return {"ok": True, "status": "rejected"}


@router.put("/api/buy-plans/{plan_id}/po", dependencies=[Depends(_require_v1_enabled)])
async def enter_po_number(
    plan_id: int,
    body: BuyPlanPOEntry,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer enters PO number for a line item."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if plan.status not in ("approved", "po_entered"):
        raise HTTPException(400, f"Cannot enter PO for plan in status: {plan.status}")

    line_index = body.line_index
    po_number = body.po_number.strip()
    if not po_number:
        raise HTTPException(400, "po_number required")
    if line_index < 0 or line_index >= len(plan.line_items or []):
        raise HTTPException(400, "Invalid line_index")

    plan.line_items[line_index]["po_number"] = po_number
    plan.line_items[line_index]["po_entered_at"] = datetime.now(timezone.utc).isoformat()
    plan.status = "po_entered"

    from sqlalchemy.orm.attributes import flag_modified

    from ...services.buyplan_service import log_buyplan_activity

    flag_modified(plan, "line_items")
    log_buyplan_activity(db, user.id, plan, "buyplan_po_entered", f"line {line_index} PO: {po_number}")
    db.commit()

    # Trigger PO verification in background
    from ...services.buyplan_service import run_buyplan_bg, verify_po_sent

    run_buyplan_bg(verify_po_sent, plan.id)

    return {"ok": True, "status": "po_entered"}


@router.get("/api/buy-plans/{plan_id}/verify-po")
async def check_po_verification(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check PO verification status — re-scan if needed."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")

    from ...services.buyplan_service import verify_po_sent

    results = await verify_po_sent(plan, db)
    return {
        "plan_id": plan.id,
        "status": plan.status,
        "verifications": results,
        "line_items": plan.line_items,
    }


@router.put("/api/buy-plans/{plan_id}/complete", dependencies=[Depends(_require_v1_enabled)])
async def complete_buy_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark buy plan complete. Admin/manager: from Approved or PO-entered. Buyer: only after PO entered (po_entered/po_confirmed)."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    allowed_statuses = ["approved", "po_entered", "po_confirmed"]
    if plan.status not in allowed_statuses:
        raise HTTPException(400, f"Can only complete from {'/'.join(allowed_statuses)}, current: {plan.status}")
    is_mgr = _is_admin(user) or user.role == "manager"
    is_buyer = user.role in ("buyer", "trader")
    if is_mgr:
        pass  # can complete from any allowed status
    elif is_buyer:
        if plan.status not in ("po_entered", "po_confirmed"):
            raise HTTPException(403, "Buyer can mark complete only after PO numbers are entered")
    else:
        raise HTTPException(403, "Only admin, manager, or buyer can mark plan complete")

    plan.status = "complete"
    plan.completed_at = datetime.now(timezone.utc)
    plan.completed_by_id = user.id

    from ...services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(db, user.id, plan, "buyplan_completed", "marked complete")
    db.commit()

    from ...services.buyplan_service import notify_buyplan_completed, run_buyplan_bg

    run_buyplan_bg(
        notify_buyplan_completed,
        plan.id,
        completer_name=user.name or user.email,
    )

    return {"ok": True, "status": "complete"}


@router.put("/api/buy-plans/{plan_id}/cancel", dependencies=[Depends(_require_v1_enabled)])
async def cancel_buy_plan(
    plan_id: int,
    body: BuyPlanCancel,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan. Submitter can cancel from pending_approval.
    Admin/manager can cancel from pending_approval or approved (before POs)."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")

    is_mgr = _is_admin(user) or user.role == "manager"
    is_submitter = plan.submitted_by_id == user.id

    if plan.status == "pending_approval":
        if not is_submitter and not is_mgr:
            raise HTTPException(403, "Only submitter or admin/manager can cancel pending plans")
    elif plan.status == "approved":
        if not is_mgr:
            raise HTTPException(403, "Only admin/manager can cancel approved plans")
        has_pos = any(item.get("po_number") for item in (plan.line_items or []))
        if has_pos:
            raise HTTPException(400, "Cannot cancel — PO numbers already entered. Remove POs first.")
    else:
        raise HTTPException(400, f"Cannot cancel plan in status: {plan.status}")

    reason = body.reason.strip()

    plan.status = "cancelled"
    plan.cancelled_at = datetime.now(timezone.utc)
    plan.cancelled_by_id = user.id
    plan.cancellation_reason = reason or None

    # Revert quote/req/offer statuses
    quote = db.get(Quote, plan.quote_id)
    req = db.get(Requisition, plan.requisition_id) if plan.requisition_id else None
    if quote:
        quote.status = "sent"
        quote.result = None
        quote.result_at = None
        quote.won_revenue = None
    if req:
        req.status = "active"
    offer_ids = [item.get("offer_id") for item in (plan.line_items or []) if item.get("offer_id")]
    if offer_ids:
        offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
        for o in offers:
            if o.status == "won":
                o.status = "active"

    from ...services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(db, user.id, plan, "buyplan_cancelled", reason or "cancelled")
    db.commit()

    from ...services.buyplan_service import notify_buyplan_cancelled, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_cancelled, plan.id)

    return {"ok": True, "status": "cancelled"}


@router.put("/api/buy-plans/{plan_id}/resubmit", dependencies=[Depends(_require_v1_enabled)])
async def resubmit_buy_plan(
    plan_id: int,
    body: BuyPlanResubmit,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Resubmit a rejected or cancelled buy plan as a new plan."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if not _is_admin(user) and user.role != "manager":
        if plan.submitted_by_id != user.id:
            raise HTTPException(403, "Only the original submitter, admin, or manager can resubmit")
    if plan.status not in ("rejected", "cancelled"):
        raise HTTPException(400, f"Can only resubmit from rejected/cancelled, current: {plan.status}")

    salesperson_notes = body.salesperson_notes.strip()

    import secrets

    new_line_items = [
        {
            **item,
            "po_number": None,
            "po_entered_at": None,
            "po_sent_at": None,
            "po_recipient": None,
            "po_verified": False,
        }
        for item in (plan.line_items or [])
    ]

    # Detect stock sale: all vendors match stock_sale_vendor_names
    stock_names = settings.stock_sale_vendor_names
    is_stock = bool(new_line_items) and all(
        (item.get("vendor_name") or "").strip().lower() in stock_names for item in new_line_items
    )

    from datetime import timedelta

    new_plan = BuyPlan(
        requisition_id=plan.requisition_id,
        quote_id=plan.quote_id,
        status="pending_approval",
        salesperson_notes=salesperson_notes or plan.salesperson_notes,
        line_items=new_line_items,
        submitted_by_id=user.id,
        approval_token=secrets.token_urlsafe(32),
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_stock_sale=is_stock,
    )
    db.add(new_plan)

    # Re-mark quote/req/offers as won
    quote = db.get(Quote, plan.quote_id)
    req = db.get(Requisition, plan.requisition_id) if plan.requisition_id else None
    if quote:
        quote.status = "won"
        quote.result = "won"
        quote.result_at = datetime.now(timezone.utc)
        quote.won_revenue = quote.subtotal
    if req:
        req.status = "won"
    offer_ids = [item.get("offer_id") for item in (plan.line_items or []) if item.get("offer_id")]
    if offer_ids:
        offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
        for o in offers:
            o.status = "won"

    from ...services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(
        db,
        user.id,
        new_plan,
        "buyplan_resubmitted",
        f"resubmitted from plan #{plan.id}",
    )
    db.commit()

    from ...services.buyplan_service import notify_buyplan_submitted, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_submitted, new_plan.id)

    return {"ok": True, "new_plan_id": new_plan.id, "status": "pending_approval"}


@router.put("/api/buy-plans/{plan_id}/po-bulk", dependencies=[Depends(_require_v1_enabled)])
async def bulk_po_entry(
    plan_id: int,
    body: BuyPlanPOBulk,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk add/edit/clear PO numbers for line items."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Buy plan not found")
    if plan.status not in ("approved", "po_entered"):
        raise HTTPException(400, f"Cannot modify POs for plan in status: {plan.status}")

    entries = body.entries
    if not entries:
        raise HTTPException(400, "No PO entries provided")

    from sqlalchemy.orm.attributes import flag_modified

    from ...services.buyplan_service import log_buyplan_activity

    now = datetime.now(timezone.utc).isoformat()
    changes = 0

    for entry in entries:
        idx = entry.line_index
        po = entry.po_number.strip() or None

        if idx is None or idx < 0 or idx >= len(plan.line_items or []):
            continue

        item = plan.line_items[idx]
        old_po = item.get("po_number")

        if po:
            if old_po and old_po != po:
                # Edit: reset verification
                item["po_verified"] = False
                item["po_sent_at"] = None
                item["po_recipient"] = None
                log_buyplan_activity(
                    db,
                    user.id,
                    plan,
                    "buyplan_po_updated",
                    f"line {idx} PO changed: {old_po} -> {po}",
                )
                changes += 1
            elif not old_po:
                log_buyplan_activity(
                    db,
                    user.id,
                    plan,
                    "buyplan_po_entered",
                    f"line {idx} PO: {po}",
                )
                changes += 1
            item["po_number"] = po
            item["po_entered_at"] = now
        else:
            # Clear PO
            if old_po:
                log_buyplan_activity(
                    db,
                    user.id,
                    plan,
                    "buyplan_po_updated",
                    f"line {idx} PO cleared (was {old_po})",
                )
                changes += 1
            item["po_number"] = None
            item["po_entered_at"] = None
            item["po_sent_at"] = None
            item["po_recipient"] = None
            item["po_verified"] = False

    # Determine new status
    has_any_po = any(item.get("po_number") for item in plan.line_items)
    if has_any_po:
        plan.status = "po_entered"
    else:
        plan.status = "approved"

    flag_modified(plan, "line_items")
    db.commit()

    # Trigger verification in background
    if has_any_po:
        from ...services.buyplan_service import run_buyplan_bg, verify_po_sent

        run_buyplan_bg(verify_po_sent, plan.id)

    return {"ok": True, "status": plan.status, "changes": changes}


@router.get("/api/buy-plans/for-quote/{quote_id}")
async def get_buyplan_for_quote(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get the buy plan associated with a quote (newest if multiple exist)."""
    plan = db.query(BuyPlan).filter(BuyPlan.quote_id == quote_id).order_by(BuyPlan.created_at.desc()).first()
    if not plan:
        return None
    return _buyplan_to_dict(plan, db)


def _record_purchase_history(db: Session, req: Requisition | None, quote: Quote, offers: list[Offer]) -> None:
    """Feed customer_part_history from won offers and quote line items.

    Called once when a buy plan is first submitted (not on resubmit).
    Errors are logged but never block the buy plan flow.
    """
    if not req or not req.customer_site_id:
        return
    try:
        from ...models import CustomerSite
        from ...services.purchase_history_service import upsert_purchase

        site = db.get(CustomerSite, req.customer_site_id)
        if not site or not site.company_id:
            return
        company_id = site.company_id

        # From won offers
        for o in offers:
            if o.material_card_id:
                upsert_purchase(
                    db,
                    company_id=company_id,
                    material_card_id=o.material_card_id,
                    source="avail_offer",
                    unit_price=o.unit_price,
                    quantity=o.qty_available,
                    source_ref=f"offer:{o.id}",
                )

        # From quote line items (may include parts not in offer selection)
        for li in quote.line_items or []:
            card_id = li.get("material_card_id")
            if not card_id:
                continue
            upsert_purchase(
                db,
                company_id=company_id,
                material_card_id=card_id,
                source="avail_quote_won",
                unit_price=li.get("sell_price"),
                quantity=li.get("qty"),
                source_ref=f"quote:{quote.id}",
            )
    except Exception as e:
        logger.warning("Purchase history recording failed: %s", e)
