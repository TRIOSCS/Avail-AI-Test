"""prepayment_service.py — Business logic for creating prepayment records.

Purpose: Persists a Prepayment row and immediately spawns a routed
         ApprovalRequest (gate_type=PREPAYMENT) via ApprovalService.create_request.
         The approval request is routed to all Users with can_approve_prepayments=True
         whose prepayment_approval_limit is NULL (unlimited) or high enough to cover
         the amount — i.e. eligible when total_incl_fees <= prepayment_approval_limit
         (matching the routing check request.amount <= limit). A limit *below* the
         amount makes that approver ineligible.

Called by: app.routers.prepayments (POST /v2/prepayments).
Depends on: app.models.quality_plan (Prepayment), app.models.buy_plan (BuyPlanLine),
            app.models.vendors (VendorCard), app.dependencies
            (PREPAYMENT_BLOCKED_PLAN_STATUSES, get_buyplan_for_user),
            app.services.approvals.service (create_request),
            app.constants (ApprovalGateType, ApprovalRequestStatus, ApprovalSubjectType,
            BuyPlanLineStatus, PaymentMethod).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ..constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanLineStatus,
)
from ..dependencies import PREPAYMENT_BLOCKED_PLAN_STATUSES, get_buyplan_for_user
from ..models.approvals import ApprovalRequest
from ..models.buy_plan import BuyPlanLine
from ..models.quality_plan import Prepayment
from ..models.vendors import VendorCard
from ..services.approvals.service import create_request


def prepayment_state_for_lines(db: Session, line_ids: Sequence[int]) -> dict[int, str]:
    """Return each PO line's *live* prepayment state in a single query (no per-line
    N+1).

    Read straight off the ``Prepayment.status`` lifecycle (the source of truth since the
    closure work): a line maps to ``'requested'`` (awaiting approval), ``'approved'`` (a wire
    is authorised / imminent), or ``'paid'`` (the wire went out). A ``void`` prepayment is
    deliberately OMITTED — a voided (stood-down) wire is no longer active, so the line is
    treated as having no prepayment and the Request-prepayment button returns, letting a fresh
    request be raised. Lines with no prepayment at all are likewise absent (callers treat "not
    in map" as "no prepayment"). Precedence paid > approved > requested if a line somehow has
    several (the duplicate-guard makes that rare).

    Shared by the plan-detail line table and the Approvals-hub PO Approval tab so both can
    (a) show a "Prepayment pending / Prepaid / Paid" badge and (b) swap the live request
    button for a non-interactive pill without any extra DB round-trips.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        line_ids: The ``BuyPlanLine.id`` values to resolve (empty → ``{}``).

    Returns:
        ``{line_id: 'requested' | 'approved' | 'paid'}`` for lines with an active prepayment
        (``void`` omitted so the line re-opens for a fresh request).
    """
    from ..constants import PrepaymentStatus

    ids = [i for i in line_ids if i is not None]
    if not ids:
        return {}

    # Precedence for the badge/pill when a line carries more than one prepayment over time
    # (e.g. an old void + a fresh request): the most-progressed active state wins. ``void`` is
    # excluded from the query entirely so it can never mask a live request.
    precedence = {
        PrepaymentStatus.PAID.value: 3,
        PrepaymentStatus.APPROVED.value: 2,
        PrepaymentStatus.REQUESTED.value: 1,
    }

    rows = (
        db.query(Prepayment.buy_plan_line_id, Prepayment.status)
        .filter(
            Prepayment.buy_plan_line_id.in_(ids),
            Prepayment.status.in_(list(precedence.keys())),
        )
        .all()
    )

    state: dict[int, str] = {}
    for line_id, status in rows:
        if line_id is None or status not in precedence:
            continue
        if line_id not in state or precedence[status] > precedence[state[line_id]]:
            state[line_id] = status
    return state


def create_prepayment(
    db: Session,
    *,
    buy_plan_id: int,
    buy_plan_line_id: int,
    vendor_card_id: int | None,
    payment_method: str | None,
    total_incl_fees: Decimal,
    test_report_sent: bool,
    buyer_remarks: str | None,
    created_by,  # User ORM object
    vendor_name: str | None = None,
    currency: str = "USD",
) -> tuple[Prepayment, ApprovalRequest]:
    """Persist a Prepayment and spawn a routed prepayment approval request.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        buy_plan_id: FK to buy_plans_v3.id (required).
        buy_plan_line_id: FK to buy_plan_lines.id — the specific PO line being prepaid
            (required). Must belong to *buy_plan_id* and have a cut PO.
        vendor_card_id: FK to vendor_cards.id (optional).
        payment_method: PaymentMethod value (wire / cc / paypal) or None.
        total_incl_fees: Total payment amount including fees (used for limit routing).
        test_report_sent: Whether the vendor already returned the test report.
        buyer_remarks: Free-text notes from the buyer.
        created_by: The authenticated User triggering the prepayment.
        vendor_name: Client-prefilled payee name — a *fallback only*. The authoritative
            payee is always derived server-side (the line's offer, else the vendor card);
            this value is used solely when neither server source resolves, so a
            client-supplied string can never override the real payee.
        currency: ISO currency code for the amount (default "USD"). Captured onto the
            Prepayment and threaded to the approval request so limits/notices are honest.

    Returns:
        A (Prepayment, ApprovalRequest) tuple — both flushed, not yet committed.

    Raises:
        NoEligibleApproverError: Propagated from route_request when no eligible
            approver exists for the PREPAYMENT gate at this amount.
        HTTPException(404): If *created_by* may not access *buy_plan_id* (restricted
            roles not owning the parent requisition).
        ValueError: If *total_incl_fees* <= 0, the parent plan is terminal/inbound,
            *buy_plan_line_id* does not belong to *buy_plan_id*, the line has no cut PO
            (no po_number / not PENDING_VERIFY|VERIFIED), or a prepayment for the line is
            already awaiting approval OR already approved (race-safe double-pay guard).
    """
    # Amount must be positive: a zero/negative total silently satisfies any approver limit
    # and routes to the lowest tier (finding #4).
    if total_incl_fees is None or total_incl_fees <= 0:
        raise ValueError("Prepayment amount must be greater than zero.")

    # Ownership gate (service-layer so the router stays thin): a Prepayment + routed
    # ApprovalRequest must not be attachable to a buy plan the actor can't access.
    plan = get_buyplan_for_user(db, created_by, buy_plan_id)

    # Plan-status gate (finding #5): a dead/inbound plan keeps VERIFIED lines, but a new
    # prepayment on it makes no sense. Mirrored in dependencies.can_request_prepayment so
    # the button hides exactly where this rejects.
    if plan.status in PREPAYMENT_BLOCKED_PLAN_STATUSES:
        raise ValueError(f"Cannot request a prepayment on a {plan.status} buy plan.")

    # Lock the line to serialize concurrent prepayment requests on the same PO (a no-op on
    # SQLite, enforced on PostgreSQL). The lock + the REQUESTED re-check below together are
    # the race-safe duplicate-pending guard.
    line = db.query(BuyPlanLine).filter(BuyPlanLine.id == buy_plan_line_id).with_for_update().one_or_none()
    if line is None or line.buy_plan_id != buy_plan_id:
        raise ValueError("Line does not belong to this buy plan.")
    if not line.po_number or line.status not in (
        BuyPlanLineStatus.PENDING_VERIFY.value,
        BuyPlanLineStatus.VERIFIED.value,
    ):
        raise ValueError("This PO is not ready for a prepayment request.")

    # One live prepayment per PO: block a second prepayment that is still REQUESTED *or*
    # already APPROVED on this line — an approved (about-to-be-wired) prepayment must also
    # block a duplicate, else the same PO gets paid twice (finding #1). Enum members (no
    # .value) match the ApprovalRequest comparison convention in
    # services/approvals/queue.py + service.py.
    existing = (
        db.query(ApprovalRequest.id)
        .join(Prepayment, Prepayment.id == ApprovalRequest.subject_id)
        .filter(
            ApprovalRequest.subject_type == ApprovalSubjectType.PREPAYMENT,
            ApprovalRequest.gate_type == ApprovalGateType.PREPAYMENT,
            ApprovalRequest.status.in_([ApprovalRequestStatus.REQUESTED, ApprovalRequestStatus.APPROVED]),
            Prepayment.buy_plan_line_id == buy_plan_line_id,
        )
        .first()
    )
    if existing:
        raise ValueError("A prepayment for this PO is already awaiting approval or approved.")

    # Snapshot the payee so the approver / AP always see who is being paid even if the line
    # or offer later changes (finding #3): prefer authoritative server sources (never let a
    # client string name the payee over a real source) — the line's offer vendor_name (a
    # NOT-NULL string), else the passed vendor card's display_name, else the client-
    # prefilled fallback, else None.
    payee_name = None
    if line.offer is not None:
        payee_name = line.offer.vendor_name
    elif vendor_card_id is not None:
        card = db.get(VendorCard, vendor_card_id)
        payee_name = card.display_name if card is not None else None
    if payee_name is None:
        payee_name = vendor_name

    prepayment = Prepayment(
        buy_plan_id=plan.id,
        buy_plan_line_id=buy_plan_line_id,
        vendor_card_id=vendor_card_id,
        vendor_name=payee_name,
        payment_method=payment_method,
        total_incl_fees=total_incl_fees,
        currency=currency,
        test_report_sent=test_report_sent,
        buyer_remarks=buyer_remarks,
        created_by_id=created_by.id if created_by is not None else None,
    )
    db.add(prepayment)
    db.flush()  # Assign prepayment.id before wiring as subject FK

    request = create_request(
        db,
        gate_type=ApprovalGateType.PREPAYMENT,
        amount=total_incl_fees,
        subject=prepayment,
        requested_by=created_by,
        owner=created_by,
        currency=prepayment.currency,
    )

    return prepayment, request


def mark_prepayment_paid(
    db: Session,
    prepayment: Prepayment,
    *,
    wire_reference: str,
    paid_amount: Decimal,
    paid_via: str,
    paid_by_id: int | None = None,
    paid_by_label: str | None = None,
) -> Prepayment:
    """Transition an APPROVED prepayment to PAID and fan out the paid notice.

    The wire actually went out (confirmed via the tokenized accounting-email link or the
    in-app manager fallback): stamp the paid fields, clear the single-use ``pay_token`` (so a
    replayed link is inert), commit, and fire the best-effort in-app fan-out to the buyer,
    salesperson, and managers.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        prepayment: The Prepayment to mark paid — must be in status ``approved``.
        wire_reference: The bank/wire reference recorded by the confirmer.
        paid_amount: The amount actually wired (defaults to ``total_incl_fees`` at the call
            site; captured here as a Decimal — never a float).
        paid_via: ``accounting_email`` (tokenized link) or ``in_app`` (manager fallback).
        paid_by_id: The Avail User who recorded the payment (in-app path); None for the
            accounting-email path (accounting has no User row).
        paid_by_label: The name/initials shown on the record (the confirmer's initials on the
            email path, the Avail user's name on the in-app path).

    Returns:
        The same Prepayment, now committed in status ``paid``.

    Raises:
        ValueError: If *prepayment* is not in status ``approved`` (only an approved
            prepayment can be marked paid — a requested one is not yet authorized, and a
            paid/void one is already terminal).
    """
    from ..constants import PrepaymentStatus
    from .prepayment_notifications import (
        notify_prepayment_paid,
        run_prepayment_notify_bg,
        schedule_prepayment_notify,
    )

    if prepayment.status != PrepaymentStatus.APPROVED.value:
        raise ValueError("Only an approved prepayment can be marked paid.")

    prepayment.status = PrepaymentStatus.PAID.value
    prepayment.paid_at = datetime.now(UTC)
    prepayment.wire_reference = wire_reference
    prepayment.paid_amount = paid_amount
    prepayment.paid_via = paid_via
    prepayment.paid_by_id = paid_by_id
    prepayment.paid_by_label = paid_by_label
    prepayment.pay_token = None
    db.commit()

    # Fan out the paid notice best-effort — loop-aware so a sync/CLI/test caller never dangles
    # a coroutine (this sync service is driven by the async confirm route + in-app fallback).
    schedule_prepayment_notify(run_prepayment_notify_bg(notify_prepayment_paid, prepayment.id))
    return prepayment
