"""test_prepayment_workspace.py — the Prepayments tab pane (Approvals Workspace 1.5).

Covers the prepayment pane (amount + payee, PO#/SO# copy chips, the loud test-report
warning, the "OK to pay — {method}" approve button), the request-side method dropdown
(PREPAYMENT_METHODS — ACH in, COD never), the router-level COD guard (400 friendly
partial BEFORE create_prepayment — the service is untouched), the NEW approver-only
method-adjust route (REQUESTED-only, stale-guarded, field-audited), and the
origin=approvals_workspace decide branch.

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs builders,
            app.routers.{prepayments,htmx.approvals_hub,htmx.buy_plans}.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    ApprovalRequestStatus,
    BuyPlanStatus,
    PaymentMethod,
    PrepaymentStatus,
)
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_buyplan_po_approver, require_user
from app.models import ActivityLog, User
from app.models.quality_plan import Prepayment
from tests.test_approvals_hub_tabs import (
    _pending_prepay_request as _bare_prepay_request,
)
from tests.test_approvals_hub_tabs import (
    _pending_verify_line,
    _plan,
    _req_quote,
)


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    """TestClient authed as test_user with every decide right incl. prepayments."""
    from app.main import app

    test_user.can_approve_buy_plans = True
    test_user.can_approve_purchase_orders = True
    test_user.can_approve_prepayments = True
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver, require_buyplan_po_approver):
            app.dependency_overrides.pop(dep, None)


def _prepay_on_line(db: Session, user: User, *, line_method: str | None = None, pp_method: str = "wire"):
    """A plan + cut line (+ optional line payment_method) + a pending prepayment."""
    req, q, rq = _req_quote(db, user)
    bp = _plan(db, req, q, status=BuyPlanStatus.ACTIVE.value)
    bp.sales_order_number = "SO-PP-1"
    line = _pending_verify_line(db, bp, rq, user)
    if line_method:
        line.payment_method = line_method
    pp = Prepayment(
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        total_incl_fees=Decimal("1250.00"),
        currency="USD",
        payment_method=pp_method,
        vendor_name="Acme Dist",  # the request-time payee snapshot
        created_by_id=user.id,
    )
    db.add(pp)
    db.flush()
    from app.constants import ApprovalGateType, ApprovalRecipientStatus, ApprovalSubjectType
    from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient

    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.commit()
    return bp, line, pp, ar


# ── Request modal: methods from PREPAYMENT_METHODS ───────────────────────


def test_request_modal_offers_ach_never_cod(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/prepayments/new?line_id={line.id}").text
    for method in ("wire", "paypal", "cc", "ach"):
        assert f"value='{method}'" in body  # the modal uses single-quoted attributes
    assert "value='cod'" not in body and 'value="cod"' not in body  # COD can never be prepaid


# ── COD guard (router, BEFORE create_prepayment) ─────────────────────────


def test_cod_line_cannot_request_prepayment_htmx(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    line.payment_method = PaymentMethod.COD.value
    db_session.commit()

    r = hub_client.post(
        "/v2/partials/prepayments",
        data={
            "buy_plan_id": bp.id,
            "buy_plan_line_id": line.id,
            "payment_method": "wire",
            "total_incl_fees": "100.00",
        },
    )
    assert r.status_code == 400  # the friendly hard guard
    assert "COD" in r.text and "nothing to pay in advance" in r.text
    assert db_session.query(Prepayment).filter(Prepayment.buy_plan_line_id == line.id).count() == 0


def test_cod_method_value_rejected_htmx(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    r = hub_client.post(
        "/v2/partials/prepayments",
        data={
            "buy_plan_id": bp.id,
            "buy_plan_line_id": line.id,
            "payment_method": "cod",  # forged/stale form
            "total_incl_fees": "100.00",
        },
    )
    assert r.status_code == 400
    assert db_session.query(Prepayment).filter(Prepayment.buy_plan_line_id == line.id).count() == 0


def test_cod_line_cannot_request_prepayment_json(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    line.payment_method = PaymentMethod.COD.value
    db_session.commit()

    r = hub_client.post(
        "/v2/prepayments",
        json={"buy_plan_id": bp.id, "buy_plan_line_id": line.id, "total_incl_fees": "100.00"},
    )
    assert r.status_code == 400


# ── Pane rendering ───────────────────────────────────────────────────────


def test_pane_shows_amount_payee_chips_and_ok_to_pay(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, line, pp, _ar = _prepay_on_line(db_session, test_user)

    body = hub_client.get(f"/v2/partials/approvals/prepayments/{pp.id}/pane").text
    assert "USD 1,250.00" in body  # amount always visible
    assert "Acme Dist" in body  # payee (offer vendor snapshot chain)
    assert f'data-copy-value="{line.po_number}"' in body  # PO# copy chip
    assert 'data-copy-value="SO-PP-1"' in body  # SO# copy chip
    assert "OK to pay — WIRE" in body  # the method lives in the field, not the button
    assert "Test report NOT sent to management" in body  # loud warning


def test_pane_method_dropdown_on_approval_card(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, _ar = _prepay_on_line(db_session, test_user)

    body = hub_client.get(f"/v2/partials/approvals/prepayments/{pp.id}/pane").text
    assert f"/v2/partials/approvals/prepayments/{pp.id}/method" in body
    for method in ("wire", "paypal", "cc", "ach"):
        assert f'value="{method}"' in body
    assert 'value="cod"' not in body
    assert 'name="expected_updated_at"' in body  # stale-guard token rides along


def test_pane_paid_shows_wire_reference(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, ar = _prepay_on_line(db_session, test_user)
    ar.status = ApprovalRequestStatus.APPROVED.value
    pp.status = PrepaymentStatus.PAID.value
    pp.paid_at = datetime.now(UTC)
    pp.wire_reference = "FT-2233"
    pp.paid_by_label = "MK"
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/prepayments/{pp.id}/pane").text
    assert "Paid" in body
    assert 'data-copy-value="FT-2233"' in body  # wire reference as a copy chip
    assert "MK" in body


def test_pane_missing_prepayment_404s(hub_client: TestClient):
    assert hub_client.get("/v2/partials/approvals/prepayments/999999/pane").status_code == 404


# ── Method-adjust route ──────────────────────────────────────────────────


def test_method_adjust_updates_logs_and_rerenders(hub_client: TestClient, db_session: Session, test_user: User):
    bp, _line, pp, _ar = _prepay_on_line(db_session, test_user, pp_method="wire")
    from app.services.stale_guard import stale_token

    token = stale_token(pp)
    r = hub_client.post(
        f"/v2/partials/approvals/prepayments/{pp.id}/method",
        data={"payment_method": "ach", "expected_updated_at": token},
    )
    assert r.status_code == 200
    assert "OK to pay — ACH" in r.text  # the pane re-renders with the new method
    assert r.headers.get("HX-Trigger") == "awListRefresh"

    db_session.expire_all()
    assert pp.payment_method == "ach"
    audit = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.activity_type == ActivityType.FIELD_EDIT.value,
            ActivityLog.prepayment_id == pp.id,
        )
        .one()
    )
    assert audit.details["edits"] == [{"field": "payment_method", "old": "wire", "new": "ach"}]
    assert audit.buy_plan_id == bp.id


def test_method_adjust_requires_approver_right(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, _ar = _prepay_on_line(db_session, test_user)
    test_user.can_approve_prepayments = False
    db_session.commit()

    r = hub_client.post(
        f"/v2/partials/approvals/prepayments/{pp.id}/method",
        data={"payment_method": "ach"},
    )
    assert r.status_code == 403
    db_session.expire_all()
    assert pp.payment_method == "wire"


def test_method_adjust_requested_only(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, _ar = _prepay_on_line(db_session, test_user)
    pp.status = PrepaymentStatus.APPROVED.value
    db_session.commit()

    r = hub_client.post(
        f"/v2/partials/approvals/prepayments/{pp.id}/method",
        data={"payment_method": "ach"},
    )
    assert r.status_code == 400
    db_session.expire_all()
    assert pp.payment_method == "wire"


def test_method_adjust_rejects_cod(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, _ar = _prepay_on_line(db_session, test_user)
    r = hub_client.post(
        f"/v2/partials/approvals/prepayments/{pp.id}/method",
        data={"payment_method": "cod"},
    )
    assert r.status_code == 400
    db_session.expire_all()
    assert pp.payment_method == "wire"


def test_method_adjust_stale_token_409s_without_writing(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, _ar = _prepay_on_line(db_session, test_user)
    r = hub_client.post(
        f"/v2/partials/approvals/prepayments/{pp.id}/method",
        data={"payment_method": "ach", "expected_updated_at": "2020-01-01T00:00:00+00:00"},
    )
    assert r.status_code == 409
    assert r.headers.get("HX-Reswap") == "none"  # non-destructive
    db_session.expire_all()
    assert pp.payment_method == "wire"  # nothing written
    assert db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.FIELD_EDIT.value).count() == 0


# ── Decide from the pane (origin=approvals_workspace) ────────────────────


def test_approve_from_pane_rerenders_pane(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, ar = _prepay_on_line(db_session, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    assert "OK to pay — WIRE" in r.text  # the approved stamp keeps the method
    assert r.headers.get("HX-Trigger") == "awListRefresh"
    db_session.expire_all()
    assert pp.status == PrepaymentStatus.APPROVED.value
    assert pp.pay_token  # the single-use pay link was minted (engine path untouched)


def test_reject_from_pane_voids_and_rerenders(hub_client: TestClient, db_session: Session, test_user: User):
    _bp, _line, pp, ar = _prepay_on_line(db_session, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "reject", "comment": "vendor terms changed", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    assert "Void" in r.text
    db_session.expire_all()
    assert pp.status == PrepaymentStatus.VOID.value


def test_prepay_without_line_still_renders_pane(hub_client: TestClient, db_session: Session, test_user: User):
    """A prepayment with no line (legacy rows) renders the pane without chips."""
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _ar, pp = _bare_prepay_request(db_session, bp, test_user)

    r = hub_client.get(f"/v2/partials/approvals/prepayments/{pp.id}/pane")
    assert r.status_code == 200
    assert "WireVendor" in r.text
