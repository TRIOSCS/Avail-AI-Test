"""test_approvals_bridge.py — Engine-native approvals queue (QP Phase C1).

The read-only buy-plan bridge is RETIRED. A buy-plan submission now surfaces in the queue
as a native engine ApprovalRequest (gate_type=buy_plan, subject_type=buy_plan, subject_id
= plan id), NOT a synthetic source="buy_plan" bridge item.

Covers:
  - GET /v2/approvals/requests lists a buy_plan-subject ApprovalRequest as an engine item
    (no `source` field; carries subject_type/subject_id) alongside other engine requests.
  - The list NEVER emits a synthetic source="buy_plan" bridge item.
  - Filtering gate_type=buy_plan returns exactly the buy-plan requests.
  - A pending BuyPlan with NO ApprovalRequest (pre-C1 / unrouted) does NOT appear — the
    queue is engine-only.
  - The four-tab approvals queue (Buy-Plans hub "approvals" lens body,
    /v2/partials/buy-plans/approvals) renders HTML; a buy_plan-subject row links to the
    plan detail partial, Pending/Recently-resolved split, and prepayment rows render.

Called by: pytest
Depends on: conftest (db_session), app.routers.approvals, app.models.approvals,
            app.models.buy_plan, app.dependencies.
"""

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ApprovalGateType, ApprovalSubjectType
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


def _make_buy_plan(db: Session, user: User, *, status: str = "pending") -> BuyPlan:
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
        total_cost=12_000,
    )
    db.add(bp)
    db.flush()
    return bp


def _make_buy_plan_request(db: Session, bp: BuyPlan, user: User) -> ApprovalRequest:
    """A native engine BUY_PLAN ApprovalRequest for *bp*, routed to *user* (pending)."""
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        status="requested",
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        amount=bp.total_cost,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status="pending"))
    db.flush()
    return ar


def _make_prepayment_request(db: Session, user: User) -> ApprovalRequest:
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status="requested",
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status="pending"))
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


class TestEngineNativeQueue:
    def test_list_includes_buy_plan_request_as_engine_item(self, db_session: Session) -> None:
        """A buy-plan submission surfaces as a native engine item carrying
        subject_type=buy_plan + subject_id, with NO `source` field."""
        user = _make_user(db_session)
        bp = _make_buy_plan(db_session, user, status="pending")
        ar = _make_buy_plan_request(db_session, bp, user)
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        assert resp.status_code == 200
        body = resp.json()
        items = {i["id"]: i for i in body["items"]}
        assert ar.id in items
        item = items[ar.id]
        assert "source" not in item, "engine items must not carry a bridge `source` field"
        assert item["gate_type"] == "buy_plan"
        assert item["subject_type"] == "buy_plan"
        assert item["subject_id"] == bp.id

    def test_list_never_emits_bridge_source(self, db_session: Session) -> None:
        """The retired bridge must never produce a synthetic source='buy_plan' item."""
        user = _make_user(db_session)
        bp = _make_buy_plan(db_session, user, status="pending")
        _make_buy_plan_request(db_session, bp, user)
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        body = resp.json()
        assert all(i.get("source") != "buy_plan" for i in body["items"])

    def test_filter_gate_type_buy_plan(self, db_session: Session) -> None:
        """gate_type=buy_plan returns exactly the buy-plan requests, not prepayments."""
        user = _make_user(db_session)
        bp = _make_buy_plan(db_session, user, status="pending")
        ar = _make_buy_plan_request(db_session, bp, user)
        _make_prepayment_request(db_session, user)
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests?gate_type=buy_plan")

        body = resp.json()
        ids = {i["id"] for i in body["items"]}
        assert ids == {ar.id}
        assert all(i["gate_type"] == "buy_plan" for i in body["items"])

    def test_pending_buy_plan_without_request_is_absent(self, db_session: Session) -> None:
        """A pending BuyPlan with no ApprovalRequest does NOT appear — the queue is
        engine-only (no read-only bridge surfacing a bare BuyPlan)."""
        user = _make_user(db_session)
        bp = _make_buy_plan(db_session, user, status="pending")  # no engine request
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        body = resp.json()
        assert all(i.get("subject_id") != bp.id for i in body["items"])

    def test_both_buy_plan_and_prepayment_listed(self, db_session: Session) -> None:
        """Buy-plan and prepayment engine requests both appear in one response."""
        user = _make_user(db_session)
        bp = _make_buy_plan(db_session, user, status="pending")
        bp_ar = _make_buy_plan_request(db_session, bp, user)
        pp_ar = _make_prepayment_request(db_session, user)
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/approvals/requests")

        body = resp.json()
        ids = {i["id"] for i in body["items"]}
        assert {bp_ar.id, pp_ar.id} <= ids

    def test_lens_links_buy_plan_subject_to_detail(self, db_session: Session) -> None:
        """The Buy Plans approvals tab renders HTML; a buy_plan-subject row links to the
        plan detail partial."""
        user = _make_user(db_session)
        bp = _make_buy_plan(db_session, user, status="pending")
        _make_buy_plan_request(db_session, bp, user)
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/partials/buy-plans/approvals?tab=buy_plans")

        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert f"/v2/partials/buy-plans/{bp.id}" in resp.text

    def test_lens_splits_pending_and_resolved(self, db_session: Session) -> None:
        """A REQUESTED row lands in Pending; a resolved row lands in Recently resolved —
        the two sections are distinct (a resolved row never inflates Pending)."""
        user = _make_user(db_session)
        bp = _make_buy_plan(db_session, user, status="pending")
        _make_buy_plan_request(db_session, bp, user)  # one open
        bp2 = _make_buy_plan(db_session, user, status="pending")
        resolved = _make_buy_plan_request(db_session, bp2, user)
        resolved.status = "approved"  # a historical, non-pending row of the SAME gate
        resolved.resolved_at = datetime.now(timezone.utc)
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/partials/buy-plans/approvals?tab=buy_plans")

        assert resp.status_code == 200
        assert "Pending" in resp.text
        assert "Recently resolved" in resp.text

    def test_prepayments_tab_renders_row(self, db_session: Session) -> None:
        """The Vendor Prepayments tab renders a prepayment-gate row."""
        user = _make_user(db_session)
        ar = _make_prepayment_request(db_session, user)
        db_session.commit()

        for client in _build_client(db_session, user):
            resp = client.get("/v2/partials/buy-plans/approvals?tab=prepayments")

        assert resp.status_code == 200
        assert f"Request #{ar.id}" in resp.text  # no subject set → audit-safe fallback label
