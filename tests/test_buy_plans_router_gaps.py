"""tests/test_buy_plans_router_gaps.py — Coverage gap tests for buy_plans router.

Targets the missing lines from app/routers/htmx/buy_plans.py:
  306-356 (sales_order_create), 389-390 (prepay_request_decide errors),
  534/537-538 (submit auto_approved + ValueError), 595/607-616 (approve RISK3/errors/queue),
  644-647/650 (halt errors/queue), 676-677 (receive ValueError),
  708-711/717-718 (confirm_po bad date + ValueError), 746-750 (_resource ValueError),
  786 (resource no reason_code), 881-884/887 (verify_po completion/queue),
  913-914 (flag_issue ValueError), 941-942 (resolve_issue errors).

Called by: pytest
Depends on: conftest fixtures, FastAPI TestClient
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import HTMLResponse

# ── Helpers ──────────────────────────────────────────────────────────


def _ok_html() -> HTMLResponse:
    return HTMLResponse("<html><body>ok</body></html>")


# ── Fixtures ─────────────────────────────────────────────────────────


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


@pytest.fixture()
def approver_client(db_session, test_user):
    """TestClient with require_buyplan_approver overridden to return test_user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_buyplan_approver, require_user
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
        require_buyplan_approver: _user,
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


@pytest.fixture()
def po_approver_client(db_session, test_user):
    """TestClient with require_buyplan_po_approver overridden to return test_user."""
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


# ── sales_order_create (lines 306-356) ───────────────────────────────


def test_create_so_missing_requisition_id(client):
    """POST without requisition_id → 400 (line 307-308)."""
    resp = client.post(
        "/v2/partials/buy-plans/sales-orders/create",
        data={"offer_1": "1"},
    )
    assert resp.status_code == 400


def test_create_so_non_numeric_requisition_id(client):
    """POST with non-numeric requisition_id → 400 (lines 310-312)."""
    resp = client.post(
        "/v2/partials/buy-plans/sales-orders/create",
        data={"requisition_id": "abc", "offer_1": "1"},
    )
    assert resp.status_code == 400


def test_create_so_duplicate_error(client, test_requisition):
    """DuplicateSalesOrderError → 200 with HX-Trigger + HX-Push-Url (lines 334-348)."""
    from app.services.buyplan_builder import DuplicateSalesOrderError

    exc = DuplicateSalesOrderError(existing_plan_id=99, status="draft")

    with patch(
        "app.services.buyplan_builder.create_sales_order_from_offers",
        side_effect=exc,
    ):
        with patch("app.dependencies.require_requisition_access"):
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=AsyncMock(return_value=_ok_html()),
            ):
                resp = client.post(
                    "/v2/partials/buy-plans/sales-orders/create",
                    data={"requisition_id": str(test_requisition.id), "offer_1": "1"},
                )

    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    assert "99" in resp.headers.get("HX-Push-Url", "")


def test_create_so_value_error(client, test_requisition):
    """Generic ValueError → 400 (lines 349-352)."""
    with patch(
        "app.services.buyplan_builder.create_sales_order_from_offers",
        side_effect=ValueError("no requirements selected"),
    ):
        with patch("app.dependencies.require_requisition_access"):
            resp = client.post(
                "/v2/partials/buy-plans/sales-orders/create",
                data={"requisition_id": str(test_requisition.id), "offer_1": "1"},
            )

    assert resp.status_code == 400


def test_create_so_success(client, test_requisition):
    """Happy-path create renders detail and sets HX-Push-Url (lines 354-356)."""
    mock_plan = MagicMock()
    mock_plan.id = 42

    with patch(
        "app.services.buyplan_builder.create_sales_order_from_offers",
        return_value=mock_plan,
    ):
        with patch("app.dependencies.require_requisition_access"):
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=AsyncMock(return_value=_ok_html()),
            ):
                resp = client.post(
                    "/v2/partials/buy-plans/sales-orders/create",
                    data={
                        "requisition_id": str(test_requisition.id),
                        "offer_1": "1",
                        "sell_1": "2.50",
                    },
                )

    assert resp.status_code == 200
    assert "/v2/buy-plans/42" in resp.headers.get("HX-Push-Url", "")


# ── prepay_request_decide error branches (lines 389-390) ─────────────


def _seed_prepay_request(db_session, test_user):
    """Seed a real PREPAYMENT ApprovalRequest so the decide route passes its AR-exists +
    gate_type guards and reaches svc_decide (which the tests mock)."""
    from tests.test_approvals_hub_tabs import _pending_prepay_request, _plan, _req_quote

    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status="active")
    ar, _pp = _pending_prepay_request(db_session, bp, test_user)
    return ar


def test_prepay_decide_permission_error(db_session, approver_client, test_user):
    """svc_decide raises PermissionError → 403 (line 388-389)."""
    ar = _seed_prepay_request(db_session, test_user)
    with patch(
        "app.services.approvals.service.decide",
        side_effect=PermissionError("not a recipient"),
    ):
        resp = approver_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve"},
        )
    assert resp.status_code == 403


def test_prepay_decide_value_error(db_session, approver_client, test_user):
    """svc_decide raises ValueError → 400 (lines 389-390)."""
    ar = _seed_prepay_request(db_session, test_user)
    with patch(
        "app.services.approvals.service.decide",
        side_effect=ValueError("already decided"),
    ):
        resp = approver_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve"},
        )
    assert resp.status_code == 400


# ── buy_plan_submit_partial (lines 534, 537-538) ──────────────────────


def test_submit_auto_approved_path(client, buy_plan):
    """auto_approved flag triggers notify_approved branch (line 534)."""
    mock_plan = MagicMock()
    mock_plan.id = buy_plan.id
    mock_plan.auto_approved = True

    with patch("app.services.buyplan_workflow.submit_buy_plan", return_value=mock_plan):
        with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=AsyncMock(return_value=_ok_html()),
            ):
                resp = client.post(
                    f"/v2/partials/buy-plans/{buy_plan.id}/submit",
                    data={"sales_order_number": "SO-999"},
                )
    assert resp.status_code == 200


def test_submit_value_error(client, buy_plan):
    """submit_buy_plan raises ValueError → 400 (lines 537-538)."""
    with patch(
        "app.services.buyplan_workflow.submit_buy_plan",
        side_effect=ValueError("already submitted"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/submit",
            data={"sales_order_number": "SO-001"},
        )
    assert resp.status_code == 400


# ── buy_plan_approve_partial (lines 595, 607-613, 616) ───────────────


def test_approve_risk3_fallback_no_open_request(approver_client, buy_plan):
    """No open ApprovalRequest → falls back to legacy approve_buy_plan (line 595)."""
    # DB has no ApprovalRequest rows → open_request is None → RISK3 path
    with patch("app.services.buyplan_workflow.approve_buy_plan") as mock_approve:
        with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=AsyncMock(return_value=_ok_html()),
            ):
                resp = approver_client.post(
                    f"/v2/partials/buy-plans/{buy_plan.id}/approve",
                    data={"action": "approve"},
                )
    mock_approve.assert_called_once()
    assert resp.status_code == 200


def test_approve_reject_notify(approver_client, buy_plan):
    """Action=reject → notify_rejected is called (lines 607-613)."""
    with patch("app.services.buyplan_workflow.approve_buy_plan"):
        with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()) as mock_notify:
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=AsyncMock(return_value=_ok_html()),
            ):
                resp = approver_client.post(
                    f"/v2/partials/buy-plans/{buy_plan.id}/approve",
                    data={"action": "reject", "notes": "not approved"},
                )
    assert resp.status_code == 200
    mock_notify.assert_called()


def test_approve_permission_error(approver_client, buy_plan):
    """PermissionError in approve path → 403 (lines 608-611)."""
    with patch(
        "app.services.buyplan_workflow.approve_buy_plan",
        side_effect=PermissionError("insufficient rights"),
    ):
        resp = approver_client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/approve",
            data={"action": "approve"},
        )
    assert resp.status_code == 403


def test_approve_value_error(approver_client, buy_plan):
    """ValueError in approve path → 400 (lines 612-613)."""
    with patch(
        "app.services.buyplan_workflow.approve_buy_plan",
        side_effect=ValueError("wrong status"),
    ):
        resp = approver_client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/approve",
            data={"action": "approve"},
        )
    assert resp.status_code == 400


def test_approve_stale_my_queue_origin_falls_through_to_detail(approver_client, buy_plan):
    """The my_queue origin retired with its surface — a stale origin=my_queue post falls
    through to the default detail-partial re-render."""
    detail_mock = AsyncMock(return_value=_ok_html())
    with patch("app.services.buyplan_workflow.approve_buy_plan"):
        with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=detail_mock,
            ):
                resp = approver_client.post(
                    f"/v2/partials/buy-plans/{buy_plan.id}/approve",
                    data={"action": "approve", "origin": "my_queue"},
                )
    assert resp.status_code == 200
    # The fall-through IS the claim: the default detail partial was rendered
    # and its body is what came back (not a my_queue surface, not an error page).
    detail_mock.assert_awaited_once()
    assert resp.text == _ok_html().body.decode()


# ── buy_plan_halt_partial (lines 644-647, 650) ───────────────────────


def test_halt_permission_error(client, buy_plan):
    """halt_plan raises PermissionError → 403 (lines 644-645)."""
    with patch(
        "app.services.buyplan_workflow.halt_plan",
        side_effect=PermissionError("not a supervisor"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/halt",
            data={"reason": "stop"},
        )
    assert resp.status_code == 403


def test_halt_value_error(client, buy_plan):
    """halt_plan raises ValueError → 400 (lines 646-647)."""
    with patch(
        "app.services.buyplan_workflow.halt_plan",
        side_effect=ValueError("cannot halt a draft"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/halt",
            data={"reason": "stop"},
        )
    assert resp.status_code == 400


def test_halt_blank_reason_400(client, buy_plan):
    """A blank halt reason is rejected BEFORE the service runs (epic K)."""
    resp = client.post(f"/v2/partials/buy-plans/{buy_plan.id}/halt", data={"reason": "  "})
    assert resp.status_code == 400


def test_halt_stale_my_queue_origin_falls_through_to_detail(client, buy_plan):
    """The my_queue origin retired with its surface — a stale origin=my_queue post falls
    through to the default detail-partial re-render."""
    mock_plan = MagicMock()
    mock_plan.id = buy_plan.id

    detail_mock = AsyncMock(return_value=_ok_html())
    with patch("app.services.buyplan_workflow.halt_plan", return_value=mock_plan):
        with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=detail_mock,
            ):
                resp = client.post(
                    f"/v2/partials/buy-plans/{buy_plan.id}/halt",
                    data={"origin": "my_queue", "reason": "stop"},
                )
    assert resp.status_code == 200
    # The fall-through IS the claim: the default detail partial rendered the body.
    detail_mock.assert_awaited_once()
    assert resp.text == _ok_html().body.decode()


# ── buy_plan_confirm_po_partial (lines 708-711, 717-718) ─────────────


def test_confirm_po_bad_ship_date(client, buy_plan):
    """Invalid ISO date string → datetime.now() fallback, not 400 (lines 708-711)."""
    with patch("app.services.buyplan_workflow.confirm_po") as mock_confirm:
        with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=AsyncMock(return_value=_ok_html()),
            ):
                resp = client.post(
                    f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/confirm-po",
                    data={"po_number": "PO-123", "estimated_ship_date": "not-a-date"},
                )
    assert resp.status_code == 200
    # confirm_po was called with a datetime (the fallback)
    mock_confirm.assert_called_once()
    _, kwargs = mock_confirm.call_args[0], mock_confirm.call_args
    # ship_date arg is the 4th positional
    call_args = mock_confirm.call_args[0]
    assert isinstance(call_args[3], datetime)


def test_confirm_po_value_error(client, buy_plan):
    """confirm_po raises ValueError → 400 (lines 717-718)."""
    with patch(
        "app.services.buyplan_workflow.confirm_po",
        side_effect=ValueError("line not awaiting PO"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/confirm-po",
            data={"po_number": "PO-001"},
        )
    assert resp.status_code == 400


# ── _resource_lines_and_alert ValueError (lines 746-750) ─────────────


def test_resource_line_value_error(client, buy_plan):
    """resource_line raises ValueError → 400 with logger.warning (lines 746-750)."""
    with patch(
        "app.services.buyplan_workflow.resource_line",
        side_effect=ValueError("line not claimable"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/resource",
            data={"reason_code": "vendor_cancel"},
        )
    assert resp.status_code == 400


# ── buy_plan_resource_line_partial missing reason_code (line 786) ─────


def test_resource_missing_reason_code(client, buy_plan):
    """Empty reason_code → 400 before any service call (line 786)."""
    resp = client.post(
        f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/resource",
        data={"reason_code": ""},
    )
    assert resp.status_code == 400


# ── buy_plan_verify_po_partial (lines 881-884, 887) ──────────────────


def test_verify_po_completion_triggers_notify(po_approver_client, buy_plan):
    """verify_po's own internal (approve-only) check_completion call already decides
    completion by the time it returns the line — the route reads that off
    ``line.buy_plan.status`` (an identity-map hit, not a second completion scan) to
    decide whether to notify (lines 881-884)."""
    from app.constants import BuyPlanStatus

    completed_line = MagicMock()
    completed_line.buy_plan.status = BuyPlanStatus.COMPLETED.value

    with patch("app.services.buyplan_workflow.verify_po", return_value=completed_line):
        with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()) as mock_notify:
            with patch(
                "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                new=AsyncMock(return_value=_ok_html()),
            ):
                resp = po_approver_client.post(
                    f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/verify-po",
                    data={"action": "approve"},
                )
    assert resp.status_code == 200
    mock_notify.assert_called()


def test_verify_po_stale_my_queue_origin_falls_through_to_detail(po_approver_client, buy_plan):
    """The my_queue origin retired with its surface — a stale origin=my_queue post falls
    through to the default detail-partial re-render."""
    with patch("app.services.buyplan_workflow.verify_po"):
        with patch("app.services.buyplan_workflow.check_completion", return_value=None):
            with patch("app.services.buyplan_notifications.run_notify_bg", new=AsyncMock()):
                detail_mock = AsyncMock(return_value=_ok_html())
                with patch(
                    "app.routers.htmx.buy_plans.buy_plan_detail_partial",
                    new=detail_mock,
                ):
                    resp = po_approver_client.post(
                        f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/verify-po",
                        data={"action": "approve", "origin": "my_queue"},
                    )
    assert resp.status_code == 200
    # The fall-through IS the claim: the default detail partial rendered the body.
    detail_mock.assert_awaited_once()
    assert resp.text == _ok_html().body.decode()


# ── buy_plan_flag_issue_partial (lines 913-914) ───────────────────────


def test_flag_issue_value_error(client, buy_plan):
    """flag_line_issue raises ValueError → 400 (lines 913-914)."""
    with patch(
        "app.services.buyplan_workflow.flag_line_issue",
        side_effect=ValueError("invalid issue type"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/issue",
            data={"issue_type": "bad_type"},
        )
    assert resp.status_code == 400


# ── buy_plan_resolve_issue_partial (lines 941-942) ────────────────────


def test_resolve_issue_permission_error(client, buy_plan):
    """resolve_line_issue raises PermissionError → 403 (line 940-941)."""
    with patch(
        "app.services.buyplan_workflow.resolve_line_issue",
        side_effect=PermissionError("supervisors only"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/resolve-issue",
        )
    assert resp.status_code == 403


def test_resolve_issue_value_error(client, buy_plan):
    """resolve_line_issue raises ValueError → 400 (line 941-942)."""
    with patch(
        "app.services.buyplan_workflow.resolve_line_issue",
        side_effect=ValueError("line not flagged"),
    ):
        resp = client.post(
            f"/v2/partials/buy-plans/{buy_plan.id}/lines/1/resolve-issue",
        )
    assert resp.status_code == 400
