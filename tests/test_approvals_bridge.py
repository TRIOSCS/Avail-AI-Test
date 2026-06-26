"""test_approvals_bridge.py — Tests for Task 12: unified approvals queue bridges buy-
plan approvals.

Covers:
  - GET /v2/approvals/requests returns both engine ApprovalRequest items AND pending BuyPlan items.
  - BuyPlan item includes source="buy_plan", gate_type="buy_plan", and a detail_url linking to the
    existing buy-plan detail partial (/v2/partials/buy-plans/{id}).
  - BuyPlan with non-pending status is NOT included.
  - GET /v2/approvals/queue renders the HTML queue template (200, text/html).
  - The HTML queue contains the buy-plan row with a link to the detail page.

Called by: pytest
Depends on: conftest (db_session, test_user), app.routers.approvals,
            app.models.buy_plan, app.models.approvals, app.dependencies.
"""

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_user(db: Session, *, can_approve: bool = True) -> User:
    u = User(
        email=f"bridge-user-{uuid.uuid4().hex[:6]}@test.com",
        name="Bridge User",
        role="admin",
        azure_id=f"azure-bridge-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=can_approve,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_pending_buy_plan(db: Session, user: User, *, status: str = "pending") -> BuyPlan:
    req = Requisition(
        name=f"REQ-BRIDGE-{uuid.uuid4().hex[:6]}",
        customer_name="BridgeCo",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QB-{uuid.uuid4().hex[:8]}",
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
        status=status,
        so_status="pending",
        submitted_by_id=user.id,
    )
    db.add(bp)
    db.flush()
    return bp


def _make_engine_request(db: Session, user: User) -> ApprovalRequest:
    ar = ApprovalRequest(
        gate_type="prepayment",
        status="requested",
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    ApprovalStepRecipient(step_id=step.id, user_id=user.id, status="pending")
    db.flush()
    return ar


def _build_client(db: Session, user: User):
    from app.main import app

    def _db():
        yield db

    def _user():
        return user

    overrides = {get_db: _db, require_user: _user}
    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


# ── Tests ────────────────────────────────────────────────────────────────


class TestUnifiedQueue:
    def test_list_includes_pending_buy_plan(self, db_session: Session) -> None:
        """GET /v2/approvals/requests returns pending BuyPlan as an item with
        source=buy_plan."""
        user = _make_user(db_session)
        bp = _make_pending_buy_plan(db_session, user, status="pending")
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        assert resp.status_code == 200
        body = resp.json()
        bp_items = [i for i in body["items"] if i.get("source") == "buy_plan" and i.get("subject_id") == bp.id]
        assert len(bp_items) == 1, f"Expected 1 buy_plan item, got: {bp_items}"

    def test_list_buy_plan_item_has_detail_url(self, db_session: Session) -> None:
        """Buy-plan item includes detail_url pointing at the existing buy-plan detail
        partial."""
        user = _make_user(db_session)
        bp = _make_pending_buy_plan(db_session, user, status="pending")
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        body = resp.json()
        bp_items = [i for i in body["items"] if i.get("source") == "buy_plan" and i.get("subject_id") == bp.id]
        assert len(bp_items) == 1
        item = bp_items[0]
        assert item["gate_type"] == "buy_plan"
        assert item["detail_url"] == f"/v2/partials/buy-plans/{bp.id}"

    def test_list_both_engine_and_buy_plan_items(self, db_session: Session) -> None:
        """Queue lists a prepayment engine request AND a pending buy-plan in the same
        response."""
        user = _make_user(db_session)
        ar = _make_engine_request(db_session, user)
        bp = _make_pending_buy_plan(db_session, user, status="pending")
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        body = resp.json()
        sources = {i.get("source") for i in body["items"]}
        assert "buy_plan" in sources, f"Missing buy_plan source; sources={sources}"
        # Engine request items don't have a source field (or source=engine)
        engine_items = [i for i in body["items"] if i.get("id") == ar.id]
        assert len(engine_items) == 1

    def test_non_pending_buy_plan_excluded(self, db_session: Session) -> None:
        """BuyPlan with status != 'pending' is NOT included in the unified queue."""
        user = _make_user(db_session)
        # "active" = buy-plan status after manager approved → should NOT appear in approval queue
        _make_pending_buy_plan(db_session, user, status="active")
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        body = resp.json()
        active_bp_items = [i for i in body["items"] if i.get("source") == "buy_plan" and i.get("status") == "active"]
        assert len(active_bp_items) == 0, "Non-pending buy plans should not appear in the approval queue"

    def test_queue_html_renders_buy_plan_row_with_link(self, db_session: Session) -> None:
        """GET /v2/approvals/queue returns HTML with buy-plan row linking to detail
        partial."""
        user = _make_user(db_session)
        bp = _make_pending_buy_plan(db_session, user, status="pending")
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/queue")

        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        body = resp.text
        assert f"/v2/partials/buy-plans/{bp.id}" in body
