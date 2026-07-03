"""test_approvals_queue.py — Tests for app/services/approvals/queue.py per-gate helpers.

Phase 3 (Approvals hub 3-tab restructure) retired the 3-way ``build_queue_view`` in favour
of leaner per-gate helpers the Approvals hub calls directly:
  - ``pending_rows_for_gate(db, user, gate_type)`` — REQUESTED rows (org-wide), each a RowVM
    with ``can_act`` True only for an eligible PENDING recipient + the routed approver names;
  - ``resolved_rows_for_gate(db, gate_type)`` — terminal rows, capped + coalesce-ordered;
  - ``pending_count_for_gate(db, gate_type)`` — org-wide REQUESTED count (a tab pill).

Called by: pytest
Depends on: conftest (db_session), app.services.approvals.queue,
            app.services.approvals.service, app.models.{approvals,auth,buy_plan,
            quality_plan,quotes,sourcing,vendors}, app.constants.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    PaymentMethod,
)
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quality_plan import Prepayment, QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard
from app.services.approvals.queue import (
    pending_count_for_gate,
    pending_rows_for_gate,
    resolved_rows_for_gate,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _user(db: Session, *, name: str = "Approver", **toggles) -> User:
    u = User(
        email=f"u-{uuid.uuid4().hex[:6]}@test.com",
        name=name,
        role="admin",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(timezone.utc),
        **toggles,
    )
    db.add(u)
    db.flush()
    return u


def _bp(db: Session, user: User, *, customer: str = "TestCo") -> BuyPlan:
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name=customer,
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status="draft",
        so_status="pending",
        total_cost=Decimal("1000.00"),
    )
    db.add(bp)
    db.flush()
    return bp


def _qp(db: Session, bp: BuyPlan, user: User) -> QualityPlan:
    qp = QualityPlan(buy_plan_id=bp.id, created_by_id=user.id, status="in_review")
    db.add(qp)
    db.flush()
    return qp


def _prepay(db: Session, bp: BuyPlan, user: User, *, method=PaymentMethod.WIRE, vendor="Acme Components") -> Prepayment:
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name=vendor)
    db.add(vc)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id,
        vendor_card_id=vc.id,
        total_incl_fees=Decimal("2500.00"),
        currency="USD",
        payment_method=method,
        created_by_id=user.id,
    )
    db.add(pp)
    db.flush()
    return pp


def _seed(
    db: Session,
    gate,
    *,
    subject_type,
    subject_id: int,
    status=ApprovalRequestStatus.REQUESTED,
    pending_recipients=(),
    requester: User | None = None,
    owner: User | None = None,
    amount: Decimal | None = None,
    resolved_at: datetime | None = None,
    created_at: datetime | None = None,
) -> ApprovalRequest:
    """Seed one ApprovalRequest (+ a step + PENDING recipients) directly for full
    control."""
    ar = ApprovalRequest(
        gate_type=gate,
        status=status,
        subject_type=subject_type,
        subject_id=subject_id,
        amount=amount,
        currency="USD",
        requested_by_id=requester.id if requester else None,
        owner_id=owner.id if owner else None,
        resolved_at=resolved_at,
    )
    if created_at is not None:
        ar.created_at = created_at
    db.add(ar)
    db.flush()
    if pending_recipients:
        step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
        db.add(step)
        db.flush()
        for u in pending_recipients:
            db.add(ApprovalStepRecipient(step_id=step.id, user_id=u.id, status=ApprovalRecipientStatus.PENDING))
        db.flush()
    return ar


# ── Tests ────────────────────────────────────────────────────────────────


def test_pending_rows_only_for_that_gate(db_session: Session) -> None:
    me = _user(db_session)
    bp = _bp(db_session, me)
    qp = _qp(db_session, bp, me)
    pp = _prepay(db_session, bp, me)
    so = _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(me,),
    )
    _seed(
        db_session,
        ApprovalGateType.PREPAYMENT,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        pending_recipients=(me,),
    )
    _seed(
        db_session,
        ApprovalGateType.PURCHASE_ORDER,
        subject_type=ApprovalSubjectType.QUALITY_PLAN,
        subject_id=qp.id,
        pending_recipients=(me,),
    )

    rows = pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN)

    assert [r.id for r in rows] == [so.id]
    assert all(r.gate_type == "buy_plan" for r in rows)


def test_row_vm_carries_subject_type_and_id(db_session: Session) -> None:
    me = _user(db_session)
    bp = _bp(db_session, me)
    _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(me,),
    )
    row = pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN)[0]
    assert row.subject_type == ApprovalSubjectType.BUY_PLAN
    assert row.subject_id == bp.id


def test_pending_vs_resolved_split(db_session: Session) -> None:
    me = _user(db_session)
    bp = _bp(db_session, me)
    now = datetime.now(timezone.utc)
    pending = _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(me,),
    )
    for st in (ApprovalRequestStatus.APPROVED, ApprovalRequestStatus.REJECTED):
        _seed(
            db_session,
            ApprovalGateType.BUY_PLAN,
            subject_type=ApprovalSubjectType.BUY_PLAN,
            subject_id=bp.id,
            status=st,
            resolved_at=now,
        )
    _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        status=ApprovalRequestStatus.CANCELLED,
    )  # resolved_at None

    assert [r.id for r in pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN)] == [pending.id]
    assert len(resolved_rows_for_gate(db_session, ApprovalGateType.BUY_PLAN)) == 3


def test_resolved_capped_at_10_and_coalesce_ordered(db_session: Session) -> None:
    me = _user(db_session)
    bp = _bp(db_session, me)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    approved_ids = []
    for i in range(12):
        ar = _seed(
            db_session,
            ApprovalGateType.BUY_PLAN,
            subject_type=ApprovalSubjectType.BUY_PLAN,
            subject_id=bp.id,
            status=ApprovalRequestStatus.APPROVED,
            resolved_at=base + timedelta(days=i),
        )
        approved_ids.append((i, ar.id))
    cancelled = _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        status=ApprovalRequestStatus.CANCELLED,
        resolved_at=None,
        created_at=base + timedelta(days=100),
    )

    resolved = resolved_rows_for_gate(db_session, ApprovalGateType.BUY_PLAN)

    assert len(resolved) == 10
    assert resolved[0].id == cancelled.id  # coalesce(resolved_at, updated_at, created_at) newest
    oldest_three = {ar_id for day, ar_id in approved_ids if day < 3}
    assert oldest_three.isdisjoint({r.id for r in resolved})


def test_pending_count_for_gate_is_org_wide(db_session: Session) -> None:
    me = _user(db_session)
    other = _user(db_session)
    bp = _bp(db_session, other)
    for _ in range(3):
        _seed(
            db_session,
            ApprovalGateType.BUY_PLAN,
            subject_type=ApprovalSubjectType.BUY_PLAN,
            subject_id=bp.id,
            pending_recipients=(other,),
        )

    # org-wide REQUESTED, even though "me" is awaiting none
    assert pending_count_for_gate(db_session, ApprovalGateType.BUY_PLAN) == 3
    assert pending_count_for_gate(db_session, ApprovalGateType.PREPAYMENT) == 0


def test_can_act_only_for_eligible_pending_recipient(db_session: Session) -> None:
    me = _user(db_session)
    other = _user(db_session)
    bp = _bp(db_session, me)
    a = _seed(
        db_session,
        ApprovalGateType.PREPAYMENT,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=_prepay(db_session, bp, me).id,
        pending_recipients=(me,),
    )
    b = _seed(
        db_session,
        ApprovalGateType.PREPAYMENT,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=_prepay(db_session, bp, me).id,
        pending_recipients=(other,),
    )

    by_id = {r.id: r for r in pending_rows_for_gate(db_session, me, ApprovalGateType.PREPAYMENT)}
    assert by_id[a.id].can_act is True
    assert by_id[b.id].can_act is False


def test_org_wide_shows_unactionable_row_with_approver_names(db_session: Session) -> None:
    me = _user(db_session)
    other = _user(db_session, name="Bob Approver")
    bp = _bp(db_session, me)
    b = _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(other,),
    )

    row = next(r for r in pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN) if r.id == b.id)
    assert row.can_act is False
    assert "Bob Approver" in row.approver_names


def test_subject_label_and_href_per_gate(db_session: Session) -> None:
    me = _user(db_session)
    bp = _bp(db_session, me, customer="ACME Corp")
    pp = _prepay(db_session, bp, me, method=PaymentMethod.WIRE, vendor="Acme Components")
    _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(me,),
    )
    _seed(
        db_session,
        ApprovalGateType.PREPAYMENT,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        pending_recipients=(me,),
    )

    so_row = pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN)[0]
    assert so_row.subject_label == f"Plan #{bp.id}"
    assert so_row.subject_href == f"/v2/partials/buy-plans/{bp.id}"

    pp_row = pending_rows_for_gate(db_session, me, ApprovalGateType.PREPAYMENT)[0]
    assert "Acme Components" in pp_row.subject_label
    assert pp_row.payment_method == "wire"
    assert pp_row.subject_href == f"/v2/partials/buy-plans/{bp.id}"


def test_amount_source_per_gate(db_session: Session) -> None:
    me = _user(db_session)
    bp = _bp(db_session, me)
    pp = _prepay(db_session, bp, me)
    _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        amount=Decimal("4200.00"),
        pending_recipients=(me,),
    )
    _seed(
        db_session,
        ApprovalGateType.PREPAYMENT,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        amount=Decimal("2500.00"),
        pending_recipients=(me,),
    )

    assert pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN)[0].amount == Decimal("4200.00")
    assert pending_rows_for_gate(db_session, me, ApprovalGateType.PREPAYMENT)[0].amount == Decimal("2500.00")


def test_pending_capped_oldest_first(db_session, monkeypatch) -> None:
    """PENDING_CAP bounds the pending list, oldest-first so the work most in need of a
    decision is never the part that gets hidden."""
    from app.services.approvals import queue as queue_mod

    monkeypatch.setattr(queue_mod, "PENDING_CAP", 2)
    me = _user(db_session)
    bp = _bp(db_session, me)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    oldest = _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(me,),
        created_at=base,
    )
    second = _seed(
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(me,),
        created_at=base + timedelta(days=1),
    )
    _seed(  # newest — must be the one dropped by the cap
        db_session,
        ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        pending_recipients=(me,),
        created_at=base + timedelta(days=2),
    )

    rows = pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN)
    assert [r.id for r in rows] == [oldest.id, second.id]


def test_routed_request_via_service_is_actionable(db_session: Session) -> None:
    """Integration: a request created through the real create_request/route path is can_act
    for its approver."""
    from app.services.approvals.service import create_request

    me = _user(db_session, can_approve_buy_plans=True)
    bp = _bp(db_session, me)
    create_request(db_session, gate_type=ApprovalGateType.BUY_PLAN, amount=None, subject=bp, requested_by=me, owner=me)
    db_session.flush()

    rows = pending_rows_for_gate(db_session, me, ApprovalGateType.BUY_PLAN)
    assert len(rows) == 1
    assert rows[0].can_act is True
