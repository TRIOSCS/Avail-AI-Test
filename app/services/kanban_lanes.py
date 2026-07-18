"""kanban_lanes.py — PO kanban lane computation for the Approvals Workspace (Phase 3).

Purpose: Pure, display-only lane placement for buy-plan lines on the SO pane's PO
         kanban (spec §6). ``kanban_lane`` maps one line's (status, prepayment state,
         payment method, received) to a KanbanLane with the design's exact precedence:
         cancelled is hidden; re-sourcing, received, then the PAID risk lane (prepay
         PAID on any advance method — COD never enters) outrank verified; the
         remainder fall through Approved → Pending approval → Awaiting PO (an ISSUE
         line stays in Awaiting PO carrying its issue badge). ``build_kanban``
         assembles the whole board: per-lane card DTOs with prepayment badge data
         (amount + payee + paid_at for risk aging), age anchors, edited-by-manager
         markers, note/file counts and the "line N of M · partial-ship" flag.

         Lanes are NEVER persisted and cards move only by the real actions
         (confirm PO / verify / mark received) — this module only reads.

Called by: routers/htmx/approvals_hub.py render_plan_pane (kanban context for
           _pane_kanban.html).
Depends on: app.constants (KanbanLane, BuyPlanLineStatus, PaymentMethod,
            PrepaymentStatus), app.models.buy_plan (BuyPlan, BuyPlanLine,
            BuyPlanAttachment), app.models.quality_plan (Prepayment — read only),
            app.services.prepayment_service.prepayment_state_for_lines (read only),
            app.services.field_audit.manager_edited_line_ids,
            app.services.workspace_notes.note_counts,
            app.services.qp_workspace.qp_sales_row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..constants import BuyPlanLineStatus, KanbanLane, PaymentMethod, PrepaymentStatus
from ..models.buy_plan import BuyPlan, BuyPlanAttachment, BuyPlanLine

# Board order (spec §6): Awaiting PO → Pending approval → Paid · awaiting delivery →
# Approved → Received, plus the Re-sourcing claim pool. This is the COLUMN order —
# lane *precedence* lives in kanban_lane(), and the two are deliberately different.
LANE_ORDER: tuple[KanbanLane, ...] = (
    KanbanLane.AWAITING_PO,
    KanbanLane.PENDING_APPROVAL,
    KanbanLane.PAID_AWAITING_DELIVERY,
    KanbanLane.APPROVED,
    KanbanLane.RECEIVED,
    KanbanLane.RESOURCING,
)

# UI labels (spec §5/§6 vocabulary — Approve/Approved/Pending approval, never the
# backend pending_verify/verified names).
LANE_LABELS: dict[KanbanLane, str] = {
    KanbanLane.AWAITING_PO: "Awaiting PO",
    KanbanLane.PENDING_APPROVAL: "Pending approval",
    KanbanLane.PAID_AWAITING_DELIVERY: "Paid · awaiting delivery",
    KanbanLane.APPROVED: "Approved",
    KanbanLane.RECEIVED: "Received",
    KanbanLane.RESOURCING: "Re-sourcing",
}


def kanban_lane(
    *,
    line_status: str,
    prepay_status: str | None,
    payment_method: str | None,
    received: bool,
) -> KanbanLane | None:
    """Place one buy-plan line on the kanban (display only — never persisted).

    Exact precedence (spec §6 / design D6):
      1. cancelled            → None (hidden — a dead line has no column)
      2. resourcing           → RESOURCING (the claim pool is its own lane)
      3. received             → RECEIVED (goods arrived — outranks everything live,
                                incl. a paid prepayment: paid-and-RECEIVED is not a risk)
      4. prepay PAID, not COD → PAID_AWAITING_DELIVERY (the RISK lane: money out before
                                goods, on any advance rail — wire/PayPal/CC/ACH — and it
                                outranks verified; COD never enters — nothing was paid
                                in advance, so a COD-paid row is defensive noise)
      5. verified             → APPROVED
      6. pending_verify       → PENDING_APPROVAL
      7. everything else      → AWAITING_PO (awaiting_po AND issue — an ISSUE line stays
                                in the Awaiting-PO column wearing its issue badge)

    Args:
        line_status: BuyPlanLineStatus value string.
        prepay_status: the line's live prepayment state from
            prepayment_state_for_lines ('requested'/'approved'/'paid') or None.
        payment_method: PaymentMethod value string on the line, or None.
        received: BuyPlanLine.is_received (received_at stamped).
    """
    if line_status == BuyPlanLineStatus.CANCELLED.value:
        return None
    if line_status == BuyPlanLineStatus.RESOURCING.value:
        return KanbanLane.RESOURCING
    if received:
        return KanbanLane.RECEIVED
    if prepay_status == PrepaymentStatus.PAID.value and payment_method != PaymentMethod.COD.value:
        return KanbanLane.PAID_AWAITING_DELIVERY
    if line_status == BuyPlanLineStatus.VERIFIED.value:
        return KanbanLane.APPROVED
    if line_status == BuyPlanLineStatus.PENDING_VERIFY.value:
        return KanbanLane.PENDING_APPROVAL
    return KanbanLane.AWAITING_PO


@dataclass
class KanbanCard:
    """One kanban card, fully resolved for the template (no ORM in Jinja).

    Card face (spec §6): part number, vendor, qty × unit cost, PO# (copy chip),
    est ship, payment-method chip, prepayment badge (state + amount + payee),
    notes/file count, age, edited-by-manager marker; risk cards age on paid_at.
    """

    line_id: int
    plan_id: int
    lane: KanbanLane
    part: str
    vendor: str | None
    quantity: int | None
    unit_cost: float | None
    po_number: str | None
    estimated_ship_date: datetime | None
    payment_method: str | None
    status: str
    issue_type: str | None
    # Prepayment badge (requested/approved/paid) — never its own column.
    prepay_state: str | None
    prepay_amount: float | None
    prepay_payee: str | None
    paid_at: datetime | None  # risk-lane aging anchor (green → amber 3d → red 7d)
    age_at: datetime | None  # per-lane age-chip anchor
    edited_by_manager: bool
    note_count: int
    file_count: int
    line_index: int
    line_total: int
    partial_ship: bool | None  # QP-sales "authorized to ship partial" (plan-level)
    can_receive: bool  # eligible for the manual "Mark received" action
    received_at: datetime | None = None


@dataclass
class KanbanLaneView:
    """One rendered column: lane key, UI label, its cards, and the risk flag."""

    lane: KanbanLane
    label: str
    cards: list[KanbanCard] = field(default_factory=list)

    @property
    def is_risk(self) -> bool:
        return self.lane is KanbanLane.PAID_AWAITING_DELIVERY


def _age_anchor(line: BuyPlanLine, lane: KanbanLane, paid_at: datetime | None) -> datetime | None:
    """The lane-appropriate age-chip timestamp: how long the card has sat HERE.

    Risk lane ages on paid_at (spec §6); received on received_at; approved on
    po_verified_at; pending approval on po_confirmed_at; re-sourcing on updated_at
    (when the line re-entered the pool); awaiting PO on created_at. Each falls back
    to created_at so the chip never goes blank on legacy rows.
    """
    # dict values are Any: classic-Column ORM attributes type as Column[datetime]
    # to mypy (no plugin) while holding datetime | None at runtime.
    anchors: dict[KanbanLane, Any] = {
        KanbanLane.PAID_AWAITING_DELIVERY: paid_at,
        KanbanLane.RECEIVED: line.received_at,
        KanbanLane.APPROVED: line.po_verified_at,
        KanbanLane.PENDING_APPROVAL: line.po_confirmed_at,
        KanbanLane.RESOURCING: line.updated_at,
        KanbanLane.AWAITING_PO: line.created_at,
    }
    return cast("datetime | None", anchors.get(lane) or line.created_at)


def _live_prepayments(db: Session, line_ids: list[int]) -> dict[int, object]:
    """The single most-progressed live (non-void) Prepayment row per line.

    Same precedence as prepayment_state_for_lines (paid > approved > requested,
    void excluded) but returning the ROW — the kanban badge needs amount, payee
    snapshot and paid_at, not just the state string.
    """
    from ..models.quality_plan import Prepayment

    if not line_ids:
        return {}
    precedence = {
        PrepaymentStatus.PAID.value: 3,
        PrepaymentStatus.APPROVED.value: 2,
        PrepaymentStatus.REQUESTED.value: 1,
    }
    rows = (
        db.query(Prepayment)
        .filter(
            Prepayment.buy_plan_line_id.in_(line_ids),
            Prepayment.status.in_(list(precedence.keys())),
        )
        .all()
    )
    best: dict[int, object] = {}
    for pp in rows:
        line_id = pp.buy_plan_line_id
        if line_id is None:
            continue
        current = best.get(line_id)
        if current is None or precedence[pp.status] > precedence.get(str(getattr(current, "status", "")), 0):
            best[line_id] = pp
    return best


def _attachment_counts(db: Session, line_ids: list[int]) -> dict[int, int]:
    """Batched per-line attachment counts (BuyPlanAttachment.buy_plan_line_id)."""
    if not line_ids:
        return {}
    rows = (
        db.query(BuyPlanAttachment.buy_plan_line_id, func.count(BuyPlanAttachment.id))
        .filter(BuyPlanAttachment.buy_plan_line_id.in_(line_ids))
        .group_by(BuyPlanAttachment.buy_plan_line_id)
        .all()
    )
    return {int(line_id): int(count) for line_id, count in rows}


def _as_float(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def build_kanban(db: Session, plan: BuyPlan) -> list[KanbanLaneView]:
    """Assemble the plan's PO kanban: the five spec §6 columns in board order, plus
    the Re-sourcing lane ONLY when it has cards (mirrors the Pipeline's conditional
    Halted lane — an empty claim pool is a dead column).

    Cancelled lines are hidden entirely. All card data is batch-resolved (one query
    each for prepayments, notes, attachments, manager edits) — no per-line N+1.
    """
    from .field_audit import manager_edited_line_ids
    from .prepayment_service import prepayment_state_for_lines
    from .qp_workspace import qp_sales_row
    from .workspace_notes import note_counts

    lines: list[BuyPlanLine] = sorted(plan.lines or [], key=lambda ln: ln.id)
    line_ids = [ln.id for ln in lines]

    states = prepayment_state_for_lines(db, line_ids)
    prepayments = _live_prepayments(db, line_ids)
    edited_ids = manager_edited_line_ids(db, plan)
    notes = note_counts(db, buy_plan_line_ids=line_ids)
    files = _attachment_counts(db, line_ids)
    qp = qp_sales_row(db, plan)
    partial_ship = qp.sales_authorized_ship_partial if qp is not None else None

    lanes: dict[KanbanLane, KanbanLaneView] = {
        lane: KanbanLaneView(lane=lane, label=LANE_LABELS[lane]) for lane in LANE_ORDER
    }
    total = len(lines)
    for index, line in enumerate(lines, start=1):
        prepay_state = states.get(line.id)
        lane = kanban_lane(
            line_status=line.status,
            prepay_status=prepay_state,
            payment_method=line.payment_method,
            received=line.is_received,
        )
        if lane is None:
            continue  # cancelled — hidden
        pp = prepayments.get(line.id)
        paid_at = getattr(pp, "paid_at", None) if prepay_state == PrepaymentStatus.PAID.value else None
        card = KanbanCard(
            line_id=line.id,
            plan_id=plan.id,
            lane=lane,
            part=(
                (line.requirement.primary_mpn if line.requirement else None)
                or (line.offer.mpn if line.offer else None)
                or f"Line #{line.id}"
            ),
            vendor=line.offer.vendor_name if line.offer else None,
            quantity=line.quantity,
            unit_cost=_as_float(line.unit_cost),
            po_number=line.po_number,
            estimated_ship_date=line.estimated_ship_date,
            payment_method=line.payment_method,
            status=line.status,
            issue_type=line.issue_type if line.status == BuyPlanLineStatus.ISSUE.value else None,
            prepay_state=prepay_state,
            prepay_amount=_as_float(getattr(pp, "total_incl_fees", None)),
            prepay_payee=getattr(pp, "vendor_name", None),
            paid_at=paid_at,
            age_at=_age_anchor(line, lane, paid_at),
            edited_by_manager=line.id in edited_ids,
            note_count=notes.get(line.id, 0),
            file_count=files.get(line.id, 0),
            line_index=index,
            line_total=total,
            partial_ship=partial_ship,
            # Mark-received eligibility mirrors mark_line_received's state gate:
            # verified, or the paid-risk state — and not already received.
            can_receive=(
                not line.is_received
                and (line.status == BuyPlanLineStatus.VERIFIED.value or lane is KanbanLane.PAID_AWAITING_DELIVERY)
            ),
            received_at=line.received_at,
        )
        lanes[lane].cards.append(card)

    views = [lanes[lane] for lane in LANE_ORDER if lane is not KanbanLane.RESOURCING]
    if lanes[KanbanLane.RESOURCING].cards:
        views.append(lanes[KanbanLane.RESOURCING])
    return views
