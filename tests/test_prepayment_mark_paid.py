"""test_prepayment_mark_paid.py — the mark_prepayment_paid service transition (Task 3) +
the in-app mark-paid fallback + manager undo HTMX routes (Task 6).

Verifies app.services.prepayment_service.mark_prepayment_paid:
  - marking an APPROVED prepayment sets status=paid + the paid fields and clears pay_token;
  - marking a non-APPROVED prepayment raises ValueError (only approved → paid).
Plus the routers/prepayments.py HTMX fallback:
  - POST /v2/partials/prepayments/{id}/mark-paid — a manager (or plan owner) records the wire
    in-app (paid_via=in_app, paid_by_id set); a restricted non-owner is 404;
  - POST /v2/partials/prepayments/{id}/unmark-paid — a manager reverts paid→approved, clears
    the paid fields, re-mints pay_token; a non-manager is 403.
The paid fan-out (run_prepayment_notify_bg) is suppressed under TESTING, so no notify work
runs here.

Called by: pytest
Depends on: app.services.prepayment_service (mark_prepayment_paid),
            app.routers.prepayments (mark-paid/unmark-paid), tests.test_po_line_signoff
            (_make_plan/_make_user builders), conftest (db_session).
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    PrepaymentStatus,
)
from app.models.approvals import ApprovalRequest
from app.models.quality_plan import Prepayment
from app.services.prepayment_service import mark_prepayment_paid

# reuse the plan/user builders the sibling prepayment tests rely on
from tests.test_po_line_signoff import _make_plan, _make_user


def _prepay(db: Session, *, status: str, pay_token: str | None) -> Prepayment:
    """A minimal Prepayment in *status* on a fresh plan (buy_plan_id is NOT NULL)."""
    u = _make_user(db)
    plan = _make_plan(db, u)
    pp = Prepayment(
        buy_plan_id=plan.id,
        total_incl_fees=Decimal("20002.38"),
        currency="USD",
        created_by_id=u.id,
        status=status,
        pay_token=pay_token,
    )
    db.add(pp)
    db.commit()
    return pp


@pytest.fixture()
def approved_prepay(db_session: Session) -> Prepayment:
    return _prepay(db_session, status=PrepaymentStatus.APPROVED.value, pay_token="tok-abc123")


@pytest.fixture()
def requested_prepay(db_session: Session) -> Prepayment:
    return _prepay(db_session, status=PrepaymentStatus.REQUESTED.value, pay_token=None)


def test_mark_paid_sets_fields_and_clears_token(db_session: Session, approved_prepay: Prepayment):
    pp = approved_prepay  # status=approved, pay_token set
    mark_prepayment_paid(
        db_session,
        pp,
        wire_reference="WIRE-1",
        paid_amount=Decimal("20002.38"),
        paid_via="in_app",
        paid_by_id=pp.created_by_id,
        paid_by_label="MK",
    )
    assert pp.status == PrepaymentStatus.PAID.value
    assert pp.wire_reference == "WIRE-1"
    assert pp.paid_amount == Decimal("20002.38")
    assert pp.paid_via == "in_app"
    assert pp.paid_by_label == "MK"
    assert pp.paid_at is not None
    assert pp.pay_token is None


def test_mark_paid_requires_approved(db_session: Session, requested_prepay: Prepayment):
    with pytest.raises(ValueError):
        mark_prepayment_paid(
            db_session,
            requested_prepay,
            wire_reference="x",
            paid_amount=Decimal("1"),
            paid_via="in_app",
        )


# ── Task 6: the in-app mark-paid fallback + manager undo HTMX routes ──────────


def _approved_prepay_on_plan(db: Session, owner) -> Prepayment:
    """An APPROVED prepayment on *owner*'s plan, with its approved PREPAYMENT
    ApprovalRequest (so a mark-paid re-render lands it in Recently-resolved)."""
    plan = _make_plan(db, owner)
    pp = Prepayment(
        buy_plan_id=plan.id,
        total_incl_fees=Decimal("20002.38"),
        currency="USD",
        created_by_id=owner.id,
        status=PrepaymentStatus.APPROVED.value,
        pay_token="tok-approved-1",
    )
    db.add(pp)
    db.flush()
    db.add(
        ApprovalRequest(
            gate_type=ApprovalGateType.PREPAYMENT,
            status=ApprovalRequestStatus.APPROVED,
            subject_type=ApprovalSubjectType.PREPAYMENT,
            subject_id=pp.id,
            requested_by_id=owner.id,
            owner_id=owner.id,
        )
    )
    db.commit()
    return pp


def _client_as(db_session: Session, user) -> TestClient:
    """A TestClient authed as *user* (get_db → the test session).

    Dependency overrides are auto-restored by conftest's _restore_dependency_overrides
    around every test.
    """
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


def test_manager_marks_approved_paid(db_session: Session):
    manager = _make_user(db_session, role="manager")
    pp = _approved_prepay_on_plan(db_session, manager)

    with _client_as(db_session, manager) as c:
        r = c.post(
            f"/v2/partials/prepayments/{pp.id}/mark-paid",
            data={"wire_reference": "WIRE-77", "paid_amount": "20002.38"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200, r.text

    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.PAID.value
    assert pp.paid_via == "in_app"
    assert pp.paid_by_id == manager.id
    assert pp.wire_reference == "WIRE-77"
    assert pp.paid_amount == Decimal("20002.38")
    assert pp.pay_token is None


def test_mark_paid_defaults_amount_to_total(db_session: Session):
    """Omitting paid_amount defaults to the prepayment's total_incl_fees."""
    manager = _make_user(db_session, role="manager")
    pp = _approved_prepay_on_plan(db_session, manager)

    with _client_as(db_session, manager) as c:
        r = c.post(
            f"/v2/partials/prepayments/{pp.id}/mark-paid",
            data={"wire_reference": "WIRE-DEF"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200, r.text
    db_session.refresh(pp)
    assert pp.paid_amount == Decimal("20002.38")


def test_mark_paid_restricted_non_owner_404(db_session: Session):
    """A restricted role (sales) who does not own the plan cannot mark it paid."""
    owner = _make_user(db_session, role="buyer")
    pp = _approved_prepay_on_plan(db_session, owner)
    intruder = _make_user(db_session, role="sales")

    with _client_as(db_session, intruder) as c:
        r = c.post(
            f"/v2/partials/prepayments/{pp.id}/mark-paid",
            data={"wire_reference": "X"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 404
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.APPROVED.value  # untouched


def test_unmark_paid_reverts_and_remints_token(db_session: Session):
    manager = _make_user(db_session, role="manager")
    pp = _approved_prepay_on_plan(db_session, manager)
    # Mark it paid first (through the service so the paid fields are set).
    mark_prepayment_paid(
        db_session,
        pp,
        wire_reference="WIRE-9",
        paid_amount=Decimal("20002.38"),
        paid_via="in_app",
        paid_by_id=manager.id,
        paid_by_label=manager.name,
    )
    assert pp.status == PrepaymentStatus.PAID.value

    with _client_as(db_session, manager) as c:
        r = c.post(
            f"/v2/partials/prepayments/{pp.id}/unmark-paid",
            data={},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200, r.text

    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.APPROVED.value
    assert pp.paid_at is None
    assert pp.paid_by_id is None
    assert pp.paid_by_label is None
    assert pp.paid_via is None
    assert pp.wire_reference is None
    assert pp.paid_amount is None
    assert pp.pay_token and len(pp.pay_token) >= 32  # a fresh single-use token minted


def test_unmark_paid_manager_only(db_session: Session):
    """A non-manager (buyer, even the plan owner) cannot reverse a payment."""
    buyer = _make_user(db_session, role="buyer")
    pp = _approved_prepay_on_plan(db_session, buyer)
    mark_prepayment_paid(
        db_session,
        pp,
        wire_reference="WIRE-9",
        paid_amount=Decimal("20002.38"),
        paid_via="in_app",
        paid_by_id=buyer.id,
    )

    with _client_as(db_session, buyer) as c:
        r = c.post(
            f"/v2/partials/prepayments/{pp.id}/unmark-paid",
            data={},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 403
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.PAID.value  # still paid
