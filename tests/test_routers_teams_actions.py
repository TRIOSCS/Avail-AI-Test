"""
tests/test_routers_teams_actions.py — Security tests for Teams Action.Submit webhook.

Validates signed/expiring token enforcement on /api/teams/card-action and confirms
buy-plan status mutation only occurs for valid action tokens.

Called by: pytest
Depends on: app/routers/teams_actions.py, app/services/teams_action_tokens.py
"""

from datetime import datetime, timezone

import pytest

from app.models.quotes import BuyPlan
from app.services.teams_action_tokens import create_teams_action_token


@pytest.fixture(autouse=True)
def _override_teams_action_session(monkeypatch, db_session):
    """Route uses SessionLocal directly; bind it to the test session."""
    monkeypatch.setattr("app.routers.teams_actions.SessionLocal", lambda: db_session)


@pytest.fixture(autouse=True)
def _ensure_teams_router_mounted():
    """MVP mode can skip this router; mount it explicitly for endpoint tests."""
    from app.main import app
    from app.routers.teams_actions import router as teams_actions_router

    has_route = any(
        getattr(route, "path", None) == "/api/teams/card-action" and "POST" in getattr(route, "methods", set())
        for route in app.router.routes
    )
    if not has_route:
        app.include_router(teams_actions_router)
    yield


def _mk_buy_plan(db_session, test_requisition, test_quote, test_user, status="pending_approval"):
    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status=status,
        submitted_by_id=test_user.id,
        line_items=[],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def _card_title(payload: dict) -> str:
    return payload["attachments"][0]["content"]["body"][0]["text"]


def test_card_action_blocks_missing_token(client, db_session, test_requisition, test_quote, test_user):
    plan = _mk_buy_plan(db_session, test_requisition, test_quote, test_user)
    plan_id = plan.id

    resp = client.post("/api/teams/card-action", json={"value": {"action": "buyplan_approve", "plan_id": plan_id}})
    assert resp.status_code == 200
    assert _card_title(resp.json()) == "Action Blocked"

    updated = db_session.get(BuyPlan, plan_id)
    assert updated is not None
    assert updated.status == "pending_approval"
    assert updated.approved_at is None


def test_card_action_approve_with_valid_token(client, db_session, test_requisition, test_quote, test_user):
    plan = _mk_buy_plan(db_session, test_requisition, test_quote, test_user)
    plan_id = plan.id
    token = create_teams_action_token(plan_id, "buyplan_approve")

    resp = client.post(
        "/api/teams/card-action",
        json={"value": {"action": "buyplan_approve", "plan_id": plan_id, "action_token": token}},
    )
    assert resp.status_code == 200
    assert _card_title(resp.json()) == "Approved"

    updated = db_session.get(BuyPlan, plan_id)
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approved_at is not None


def test_card_action_blocks_mismatched_action_token(client, db_session, test_requisition, test_quote, test_user):
    plan = _mk_buy_plan(db_session, test_requisition, test_quote, test_user)
    plan_id = plan.id
    approve_token = create_teams_action_token(plan_id, "buyplan_approve")

    resp = client.post(
        "/api/teams/card-action",
        json={"value": {"action": "buyplan_reject", "plan_id": plan_id, "action_token": approve_token}},
    )
    assert resp.status_code == 200
    assert _card_title(resp.json()) == "Action Blocked"

    updated = db_session.get(BuyPlan, plan_id)
    assert updated is not None
    assert updated.status == "pending_approval"
    assert updated.rejected_at is None
