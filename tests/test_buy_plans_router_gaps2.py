"""tests/test_buy_plans_router_gaps2.py — Second coverage-gap pass for buy_plans router.

Covers uncovered paths that were not in test_buy_plans_router_gaps.py:
  Lines 911-912: buy_plan_verify_po_partial ValueError → 400
  Lines 916-919: buy_plan_verify_po_partial origin=approvals_hub returns render_tab_body

Called by: pytest
Depends on: app/routers/htmx/buy_plans.py, conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from starlette.responses import HTMLResponse


def _ok_html() -> HTMLResponse:
    return HTMLResponse("<html><body>ok</body></html>")


# ── buy_plan fixture ──────────────────────────────────────────────────────────


@pytest.fixture()
def buy_plan(db_session, test_requisition, test_user):
    """A DRAFT buy plan owned by test_requisition."""
    from app.constants import BuyPlanStatus
    from app.models.buy_plan import BuyPlan

    plan = BuyPlan(
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        created_at=datetime.now(UTC),
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


# ── PO approver client fixture ────────────────────────────────────────────────


@pytest.fixture()
def po_approver_client(db_session, test_user):
    """TestClient with require_buyplan_po_approver overridden to test_user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_buyplan_po_approver, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return test_user

    overrides = {
        get_db: _db,
        require_user: _user,
        require_admin: _user,
        require_buyer: _user,
        require_buyplan_po_approver: _user,
    }
    for dep, fn in overrides.items():
        app.dependency_overrides[dep] = fn
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


# ── buy_plan_verify_po_partial: ValueError → 400 (lines 911-912) ──────────────


def test_verify_po_value_error_returns_400(po_approver_client, buy_plan):
    """verify_po raises ValueError → 400 (lines 911-912).

    The existing test_buy_plans_router_gaps.py covers the success and my_queue paths but
    not the ValueError branch. This test closes that gap.
    """
    with patch(
        "app.services.buyplan_workflow.verify_po",
        side_effect=ValueError("line not awaiting verification"),
    ):
        resp = po_approver_client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/verify-po",
            data={"action": "approve"},
        )
    assert resp.status_code == 400


def test_verify_po_permission_error_returns_400(po_approver_client, buy_plan):
    """verify_po raises PermissionError → 400 (line 911: except (ValueError,
    PermissionError)).

    Both exceptions share the same handler on line 911.
    """
    with patch(
        "app.services.buyplan_workflow.verify_po",
        side_effect=PermissionError("not a po approver"),
    ):
        resp = po_approver_client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/verify-po",
            data={"action": "approve"},
        )
    assert resp.status_code == 400


# ── buy_plan_verify_po_partial: origin=approvals_hub (lines 916-919) ──────────


def test_verify_po_origin_approvals_hub(po_approver_client, buy_plan):
    """origin=approvals_hub → render_tab_body("po-approval", hub_scope) (lines 916-919).

    The render_tab_body import is lazy (inside the conditional), so patch at its
    source location: app.routers.htmx.approvals_hub.render_tab_body.
    """
    with patch("app.services.buyplan_workflow.verify_po"):
        with patch("app.services.buyplan_workflow.check_completion", return_value=None):
            with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
                with patch(
                    "app.routers.htmx.approvals_hub.render_tab_body",
                    return_value=_ok_html(),
                ) as mock_render:
                    resp = po_approver_client.post(
                        f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/verify-po",
                        data={"action": "approve", "origin": "approvals_hub", "hub_scope": "pending"},
                    )
    assert resp.status_code == 200
    mock_render.assert_called_once()
    # Verify the tab and scope arguments were forwarded correctly
    call_args = mock_render.call_args[0]
    assert "po-approval" in call_args
    assert "pending" in call_args


def test_verify_po_origin_approvals_hub_default_scope(po_approver_client, buy_plan):
    """origin=approvals_hub without hub_scope → defaults to 'all' (line 900)."""
    with patch("app.services.buyplan_workflow.verify_po"):
        with patch("app.services.buyplan_workflow.check_completion", return_value=None):
            with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
                with patch(
                    "app.routers.htmx.approvals_hub.render_tab_body",
                    return_value=_ok_html(),
                ) as mock_render:
                    resp = po_approver_client.post(
                        f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/verify-po",
                        data={"action": "approve", "origin": "approvals_hub"},
                    )
    assert resp.status_code == 200
    call_args = mock_render.call_args[0]
    assert "all" in call_args
