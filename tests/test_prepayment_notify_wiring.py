"""test_prepayment_notify_wiring.py — notifications fired on request + approve (Task 6).

Verifies the two call sites dispatch the accounting/AP notice via run_prepayment_notify_bg:
  - the HTMX create route dispatches notify_prepayment_requested;
  - the prepay decide route dispatches notify_prepayment_approved on approve;
  - the prepay decide route dispatches notify_prepayment_voided on reject (the stand-down).
The background runner itself is patched (AsyncMock) so no email/Teams/session work runs.

Called by: pytest
Depends on: conftest (db_session, test_user, client), app.routers.prepayments,
            app.routers.htmx.buy_plans, and the shared builders in
            tests.test_approvals_hub_tabs / tests.test_prepayment_request_ui.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, PrepaymentStatus
from app.database import get_db
from app.dependencies import require_user
from app.models import SystemConfig, User
from app.models.quality_plan import Prepayment
from app.services import prepayment_notifications as pn

# Reuse the proven prepay-approval + plan/line builders.
from tests.test_approvals_hub_tabs import (
    _pending_buy_plan_request,
    _pending_prepay_request,
    _plan,
    _req_quote,
)
from tests.test_prepayment_request_ui import _plan_with_line, _seed_approver

_RUNNER = "app.services.prepayment_notifications.run_prepayment_notify_bg"


@pytest.fixture()
def approver_client(db_session: Session, test_user: User):
    """TestClient authed as test_user (the prepay decide route only needs require_user;
    the pending recipient row is what authorizes the decision)."""
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


# ── Create → requested ────────────────────────────────────────────────────


def test_create_dispatches_requested(client: TestClient, db_session: Session, test_user: User):
    _seed_approver(db_session)  # eligible approver so routing succeeds
    _bp, line = _plan_with_line(db_session, test_user)
    db_session.commit()

    with patch(_RUNNER, new_callable=AsyncMock) as bg:
        r = client.post(
            "/v2/partials/prepayments",
            data={
                "buy_plan_id": line.buy_plan_id,
                "buy_plan_line_id": line.id,
                "payment_method": "wire",
                "total_incl_fees": "20002.38",
                "test_report_sent": "false",
                "buyer_remarks": "ok",
            },
            headers={"HX-Request": "true"},
        )

    assert r.status_code == 200, r.text
    assert bg.called
    assert bg.call_args.args[0].__name__ == "notify_prepayment_requested"


# ── Approve → approved ; Reject → nothing ─────────────────────────────────


def test_approve_dispatches_approved(approver_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar, _pp = _pending_prepay_request(db_session, bp, test_user)

    with patch(_RUNNER, new_callable=AsyncMock) as bg:
        r = approver_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve", "origin": "approvals_hub"},
        )

    assert r.status_code == 200, r.text
    assert any(c.args and c.args[0].__name__ == "notify_prepayment_approved" for c in bg.call_args_list)


def test_reject_dispatches_voided(approver_client: TestClient, db_session: Session, test_user: User):
    """Reject now voids the prepayment and fires the DO-NOT-WIRE stand-down (prepay
    closure).

    It must NOT fire notify_prepayment_approved (the wire was never authorized).
    """
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar, _pp = _pending_prepay_request(db_session, bp, test_user)

    with patch(_RUNNER, new_callable=AsyncMock) as bg:
        r = approver_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "reject", "comment": "not this vendor", "origin": "approvals_hub"},
        )

    assert r.status_code == 200, r.text
    dispatched = [c.args[0].__name__ for c in bg.call_args_list if c.args]
    assert "notify_prepayment_voided" in dispatched
    assert "notify_prepayment_approved" not in dispatched


# ── Gate guard: a non-PREPAYMENT request is rejected, never mis-fires ──────


def test_non_prepayment_request_rejected(approver_client: TestClient, db_session: Session, test_user: User):
    """The prepayment-specific decide route must refuse a non-PREPAYMENT ApprovalRequest
    (400) BEFORE deciding it — so it can't decide a buy-plan gate here nor fire the OK-
    TO-WIRE notice against a wrong subject_id.

    svc_decide is stubbed to a no-op so the guard is isolated from the buy-plan approval
    bridge (which would 400 on its own for other reasons): a route without the gate
    guard would sail through the stub and dispatch notify_prepayment_approved against
    the buy plan id.
    """
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar = _pending_buy_plan_request(db_session, bp, test_user)  # BUY_PLAN gate, not PREPAYMENT
    db_session.commit()

    with (
        patch("app.services.approvals.service.decide", new=lambda *a, **k: None),
        patch(_RUNNER, new_callable=AsyncMock) as bg,
    ):
        r = approver_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve", "origin": "approvals_hub"},
        )

    assert r.status_code == 400, r.text
    assert not bg.called


# ── A1: the "approved" notify re-checks status in its OWN fresh session ───
#
# The resend/approve call sites check status BEFORE dispatching the fire-and-forget
# background task; between that check and the task actually running (its own fresh
# SessionLocal), the prepayment can flip away from approved (e.g. an in-app mark-paid
# races in). notify_prepayment_approved must re-read the committed status and no-op
# rather than emailing a stale OK-TO-WIRE for a prepayment that's already paid/void.


async def test_approved_notify_skips_when_status_no_longer_approved(db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    pp = Prepayment(
        buy_plan_id=bp.id,
        total_incl_fees=2500,
        currency="USD",
        created_by_id=test_user.id,
        status=PrepaymentStatus.PAID.value,  # flipped away from approved before the notify ran
    )
    db_session.add(pp)
    # Both channels configured — WITHOUT the status re-check the mocks below would be
    # called, proving the guard (not an empty-config no-op) is what stops the send.
    db_session.add(
        SystemConfig(key="accounting_group_email", value="accounting@trio.test", updated_at=datetime.now(UTC))
    )
    db_session.add(
        SystemConfig(
            key="prepayment_teams_webhook",
            value="https://outlook.office.com/webhook/prepay",
            updated_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    with (
        patch.object(pn, "_send_group_email", new=AsyncMock(return_value=True)) as email,
        patch.object(pn, "post_teams_channel_card", new=AsyncMock()) as card,
    ):
        result = await pn.notify_prepayment_approved(pp.id, db=db_session)

    assert result == {"email_sent": False, "teams_sent": False, "recipients": []}
    assert not email.called
    assert not card.called
