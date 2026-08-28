"""services/buyplan_handoff.py — deterministic facts block for the AI handoff brief.

Purpose: assemble everything a backup needs to pick up a deal — plan header,
per-line standing (vendor, qty, cost→sell, status, PO, ETA, issues), QP section
review stamps, prepayment states, and the ApprovalEvent timeline — as plain text
for the BUY_PLAN entity of activity_digest_service. Read-only; no AI here.

Called by: app/services/activity_digest_service.py (BUY_PLAN branch)
Depends on: app/models/buy_plan.py, app/models/quality_plan.py, app/models/approvals.py
"""

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import ApprovalGateType, ApprovalSubjectType
from ..models.approvals import ApprovalEvent, ApprovalRequest
from ..models.buy_plan import BuyPlan
from ..models.quality_plan import Prepayment, QualityPlan

LINE_CAP = 15
EVENT_CAP = 10


def _money(v) -> str:
    return f"${v:,.0f}" if v is not None else "—"


def _unit_money(v) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _d(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "not yet"


def build_handoff_facts(db: Session, plan: BuyPlan) -> str:
    """Deterministic facts block, one section per aspect of the deal."""
    req = plan.requisition
    parts = [
        f"Buy plan #{plan.id} | status: {plan.status} | SO verify: {plan.so_status} | order type: {plan.order_type}",
        f"Customer: {req.customer_name or '?'} | requisition: REQ-{req.id} '{req.name}'",
        f"ERP refs: SO# {plan.sales_order_number or 'not set'} | customer PO# {plan.customer_po_number or 'not set'}",
        f"Totals: cost {_money(plan.total_cost)} | revenue {_money(plan.total_revenue)} | "
        f"margin {plan.total_margin_pct if plan.total_margin_pct is not None else '—'}%",
        f"Quote: {plan.quote.status if plan.quote else 'none linked'}",
    ]

    lines = list(plan.lines)
    if not lines:
        parts.append("Lines: none yet")
    else:
        counts = Counter(line.status for line in lines)
        parts.append(f"Lines ({len(lines)}): " + ", ".join(f"{s}: {n}" for s, n in counts.items()))
        for line in lines[:LINE_CAP]:
            mpn = line.requirement.primary_mpn if line.requirement else "?"
            vendor = line.offer.vendor_name if line.offer else "?"
            eta = _d(line.estimated_ship_date) if line.estimated_ship_date else "—"
            seg = (
                f"  - {mpn} ×{line.quantity or 0} | vendor: {vendor} | "
                f"{_unit_money(line.unit_cost)} → {_unit_money(line.unit_sell)} | {line.status} | "
                f"PO {line.po_number or '—'} | ETA {eta}"
            )
            if line.issue_type:
                note = f": {line.issue_note}" if line.issue_note else ""
                seg += f" | ISSUE {line.issue_type}{note}"
            parts.append(seg)
        if len(lines) > LINE_CAP:
            parts.append(f"  (+{len(lines) - LINE_CAP} more lines)")

    qp = db.scalars(
        select(QualityPlan).where(QualityPlan.buy_plan_id == plan.id).order_by(QualityPlan.id.asc())
    ).first()
    if qp is None:
        parts.append("Quality plan: not created")
    else:
        sales = f"reviewed {_d(qp.sales_section_reviewed_at)}" if qp.sales_section_reviewed_at else "not reviewed"
        purch = (
            f"reviewed {_d(qp.purchasing_section_reviewed_at)}" if qp.purchasing_section_reviewed_at else "not reviewed"
        )
        parts.append(f"Quality plan #{qp.id}: Sales section {sales}; Purchasing section {purch}")

    prepays = list(db.scalars(select(Prepayment).where(Prepayment.buy_plan_id == plan.id)))
    if not prepays:
        parts.append("Prepayments: none")
    else:
        pc = Counter(p.status for p in prepays)
        parts.append(f"Prepayments ({len(prepays)}): " + ", ".join(f"{s}: {n}" for s, n in pc.items()))
        for p in prepays:
            parts.append(f"  - {_money(p.total_incl_fees)} to {p.vendor_name or '?'} ({p.status})")

    req_ids = list(
        db.scalars(
            select(ApprovalRequest.id).where(
                ApprovalRequest.gate_type == ApprovalGateType.BUY_PLAN,
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan.id,
            )
        )
    )
    events = []
    if req_ids:
        events = list(
            db.scalars(
                select(ApprovalEvent)
                .where(ApprovalEvent.request_id.in_(req_ids))
                .order_by(ApprovalEvent.created_at.desc())
                .limit(EVENT_CAP)
            )
        )
    if not events:
        parts.append("Approval history: none")
    else:
        parts.append("Approval history (newest first):")
        for e in events:
            actor = (e.actor.name or e.actor.email) if e.actor else "system"
            note = ""
            if isinstance(e.payload, dict):
                note = e.payload.get("comment") or e.payload.get("note") or ""
            parts.append(f"  - {_d(e.created_at)} | {e.event_type} | {actor}" + (f" | {note}" if note else ""))

    return "\n".join(parts)
