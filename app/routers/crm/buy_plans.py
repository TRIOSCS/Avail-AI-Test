"""buy_plans.py — V1→V3 redirect shim for Buy Plan endpoints.

Read-only endpoints adapt BuyPlanV3 data to V1 JSON shape.
All mutation endpoints return 410, pointing callers to V3.

Called by: routers/crm/__init__.py
Depends on: models.buy_plan (BuyPlanV3, BuyPlanLine), dependencies
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_user
from ...models import Quote, Requisition, User
from ...models.buy_plan import BuyPlanLine, BuyPlanStatus, BuyPlanV3

router = APIRouter()

# ── V3→V1 status mapping ─────────────────────────────────────────────

_V3_TO_V1_STATUS = {
    "draft": "draft",
    "pending": "pending_approval",
    "active": "approved",
    "halted": "cancelled",
    "completed": "complete",
    "cancelled": "cancelled",
}

_V1_TO_V3_STATUS = {v: k for k, v in _V3_TO_V1_STATUS.items()}
# Extra V1 statuses that don't have direct V3 counterparts:
_V1_TO_V3_STATUS["pending_approval"] = "pending"
_V1_TO_V3_STATUS["approved"] = "active"
_V1_TO_V3_STATUS["complete"] = "completed"
_V1_TO_V3_STATUS["po_entered"] = "active"
_V1_TO_V3_STATUS["po_confirmed"] = "active"


def _v3_to_v1_status(plan: BuyPlanV3) -> str:
    """Map V3 status to V1 status, detecting PO substates."""
    base = _V3_TO_V1_STATUS.get(plan.status, plan.status)
    if plan.status == "active" and plan.lines:
        has_po = any(l.po_number for l in plan.lines)
        all_verified = has_po and all(
            l.status == "verified" for l in plan.lines if l.po_number
        )
        if all_verified:
            return "po_confirmed"
        elif has_po:
            return "po_entered"
    return base


def _line_to_v1_dict(line: BuyPlanLine) -> dict:
    """Convert V3 BuyPlanLine to V1 line_items dict shape."""
    offer = line.offer
    req = line.requirement
    return {
        "offer_id": line.offer_id,
        "mpn": offer.mpn if offer else (req.primary_mpn if req else None),
        "vendor_name": offer.vendor_name if offer else None,
        "qty": line.quantity,
        "plan_qty": line.quantity,
        "cost_price": float(line.unit_cost) if line.unit_cost else None,
        "sell_price": float(line.unit_sell) if line.unit_sell else None,
        "lead_time": None,
        "condition": None,
        "entered_by_id": line.buyer_id,
        "po_number": line.po_number,
        "po_entered_at": str(line.po_confirmed_at) if line.po_confirmed_at else None,
        "po_sent_at": str(line.po_confirmed_at) if line.po_confirmed_at else None,
        "po_recipient": None,
        "po_verified": line.status == "verified",
    }


def _v3_to_v1_dict(plan: BuyPlanV3, db: Session) -> dict:
    """Convert V3 plan + lines to V1-shaped dict."""
    # Load related objects
    requisition = db.get(Requisition, plan.requisition_id) if plan.requisition_id else None
    quote = db.get(Quote, plan.quote_id) if plan.quote_id else None

    line_items = [_line_to_v1_dict(l) for l in (plan.lines or [])]
    total_cost = float(plan.total_cost) if plan.total_cost else 0
    total_revenue = float(plan.total_revenue) if plan.total_revenue else 0
    total_profit = total_revenue - total_cost

    return {
        "id": plan.id,
        "requisition_id": plan.requisition_id,
        "requisition_name": requisition.name if requisition else None,
        "quote_id": plan.quote_id,
        "quote_number": quote.quote_number if quote else None,
        "quote_subtotal": float(quote.subtotal) if quote and quote.subtotal else None,
        "customer_name": requisition.customer_name if requisition else None,
        "status": _v3_to_v1_status(plan),
        "line_items": line_items,
        "is_stock_sale": plan.is_stock_sale or False,
        "total_cost": total_cost,
        "total_revenue": total_revenue,
        "total_profit": total_profit,
        "overall_margin_pct": float(plan.total_margin_pct) if plan.total_margin_pct else None,
        "sales_order_number": plan.sales_order_number,
        "salesperson_notes": plan.salesperson_notes,
        "manager_notes": plan.approval_notes,
        "rejection_reason": plan.cancellation_reason,
        "submitted_by": plan.submitted_by.name if plan.submitted_by else None,
        "submitted_by_id": plan.submitted_by_id,
        "approved_by": plan.approved_by.name if plan.approved_by else None,
        "approved_by_id": plan.approved_by_id,
        "rejected_by": None,
        "rejected_by_id": None,
        "submitted_at": str(plan.submitted_at) if plan.submitted_at else None,
        "approved_at": str(plan.approved_at) if plan.approved_at else None,
        "rejected_at": None,
        "completed_at": str(plan.completed_at) if plan.completed_at else None,
        "completed_by": None,
        "cancelled_at": str(plan.cancelled_at) if plan.cancelled_at else None,
        "cancelled_by": plan.cancelled_by.name if plan.cancelled_by else None,
        "cancellation_reason": plan.cancellation_reason,
    }


# ── Read Endpoints (adapt V3 data to V1 shape) ──────────────────────


@router.get("/api/buy-plans")
async def list_buy_plans(
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """List buy plans (reads from V3, returns V1 shape)."""
    q = db.query(BuyPlanV3).options(
        joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.offer),
        joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.requirement),
        joinedload(BuyPlanV3.submitted_by),
        joinedload(BuyPlanV3.approved_by),
        joinedload(BuyPlanV3.cancelled_by),
    )

    # Access control: admin/manager/buyer see all, sales/trader see own
    if user.role not in ("admin", "manager", "buyer"):
        q = q.filter(BuyPlanV3.submitted_by_id == user.id)

    # Status filter: convert V1 status name to V3
    if status:
        v3_status = _V1_TO_V3_STATUS.get(status, status)
        q = q.filter(BuyPlanV3.status == v3_status)

    plans = q.order_by(BuyPlanV3.created_at.desc()).all()
    return [_v3_to_v1_dict(p, db) for p in plans]


@router.get("/api/buy-plans/for-quote/{quote_id}")
async def get_plan_for_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get buy plan for a quote (reads from V3)."""
    plan = (
        db.query(BuyPlanV3)
        .options(
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanV3.submitted_by),
            joinedload(BuyPlanV3.approved_by),
            joinedload(BuyPlanV3.cancelled_by),
        )
        .filter(BuyPlanV3.quote_id == quote_id)
        .order_by(BuyPlanV3.created_at.desc())
        .first()
    )
    if not plan:
        return None
    return _v3_to_v1_dict(plan, db)


@router.get("/api/buy-plans/token/{token}")
async def get_plan_by_token(
    token: str,
    db: Session = Depends(get_db),
):
    """Public: get plan by approval token (reads from V3)."""
    plan = (
        db.query(BuyPlanV3)
        .options(
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanV3.submitted_by),
            joinedload(BuyPlanV3.approved_by),
            joinedload(BuyPlanV3.cancelled_by),
        )
        .filter(BuyPlanV3.approval_token == token)
        .first()
    )
    if not plan:
        raise HTTPException(404, "Invalid or expired token")
    return _v3_to_v1_dict(plan, db)


@router.get("/api/buy-plans/{plan_id}")
async def get_buy_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get buy plan detail (reads from V3, returns V1 shape)."""
    plan = (
        db.query(BuyPlanV3)
        .options(
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanV3.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanV3.submitted_by),
            joinedload(BuyPlanV3.approved_by),
            joinedload(BuyPlanV3.cancelled_by),
        )
        .filter(BuyPlanV3.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(404, "Buy plan not found")

    # Access control: admin/manager/buyer see all, others only own
    if user.role not in ("admin", "manager", "buyer"):
        if plan.submitted_by_id != user.id:
            raise HTTPException(403, "Not authorized to view this plan")

    return _v3_to_v1_dict(plan, db)


# ── Mutation Endpoints (return 410, point to V3) ─────────────────────

_V1_DEPRECATED_MSG = "V1 buy plan mutations are disabled. Use /api/buy-plans-v3/ endpoints."


@router.post("/api/quotes/{quote_id}/buy-plan/draft")
async def create_buy_plan_draft(quote_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.post("/api/quotes/{quote_id}/buy-plan")
async def submit_buy_plan(quote_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/submit")
async def submit_draft_buy_plan(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/approve")
async def approve_buy_plan(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/reject")
async def reject_buy_plan(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/po")
async def enter_po_number(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/po-bulk")
async def bulk_po_entry(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/complete")
async def complete_buy_plan(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/cancel")
async def cancel_buy_plan(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/{plan_id}/resubmit")
async def resubmit_buy_plan(plan_id: int):
    raise HTTPException(410, _V1_DEPRECATED_MSG)


@router.put("/api/buy-plans/token/{token}/approve")
async def approve_buyplan_by_token(token: str):
    raise HTTPException(410, "Use PUT /api/buy-plans-v3/token/{token}/approve")


@router.put("/api/buy-plans/token/{token}/reject")
async def reject_buyplan_by_token(token: str):
    raise HTTPException(410, "Use PUT /api/buy-plans-v3/token/{token}/reject")


@router.get("/api/buy-plans/{plan_id}/verify-po")
async def check_po_verification(plan_id: int):
    raise HTTPException(410, "Use GET /api/buy-plans-v3/{plan_id}/verify-po")
