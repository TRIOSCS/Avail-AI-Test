"""test_prepayment_lifecycle_transitions.py — approve/reject stamp the Prepayment (Task
2).

Verifies the prepay decide route (POST /v2/partials/approvals/prepay-requests/{id}/decide):
  - APPROVE stamps status=approved + approved_by_id/approved_at and mints a >=32-char
    single-use pay_token (the "OK TO WIRE" email link);
  - REJECT flips status=void with a void_reason (the stand-down).

Called by: pytest
Depends on: conftest (db_session, test_user), app.routers.htmx.buy_plans, and the shared
            prepay-approval builders in tests.test_approvals_hub_tabs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, PrepaymentStatus
from app.database import get_db
from app.dependencies import require_user
from app.models import User

# Reuse the proven prepay-approval + plan/line builders.
from tests.test_approvals_hub_tabs import _pending_prepay_request, _plan, _req_quote


@pytest.fixture()
def approved_manager_client(db_session: Session, test_user: User):
    """TestClient authed as test_user — the pending recipient that authorizes the
    decision.

    The prepay decide route only needs require_user; the pending recipient slot (created
    by _pending_prepay_request against test_user) is what authorizes approve/reject.
    """
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def pending_prepay(db_session: Session, test_user: User):
    """A REQUESTED prepayment + its pending PREPAYMENT approval request → (prepayment,
    request)."""
    req, q, _rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar, pp = _pending_prepay_request(db_session, bp, test_user)
    return pp, ar


def test_approve_stamps_and_mints_token(db_session, approved_manager_client, pending_prepay):
    pp, req = pending_prepay
    r = approved_manager_client.post(
        f"/v2/partials/approvals/prepay-requests/{req.id}/decide",
        data={"action": "approve"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200, r.text
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.APPROVED.value
    assert pp.approved_by_id is not None and pp.approved_at is not None
    assert pp.pay_token and len(pp.pay_token) >= 32


def test_reject_voids(db_session, approved_manager_client, pending_prepay):
    pp, req = pending_prepay
    r = approved_manager_client.post(
        f"/v2/partials/approvals/prepay-requests/{req.id}/decide",
        data={"action": "reject", "comment": "no"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200, r.text
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.VOID.value
    assert pp.void_reason
    assert pp.voided_at is not None and pp.voided_by_id is not None
