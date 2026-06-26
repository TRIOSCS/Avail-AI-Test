"""test_approvals_routes.py — Tests for app/routers/approvals.py (Task 9).

Covers:
  - Pending recipient can approve (200, status becomes approved)
  - Non-recipient gets 403
  - Reject without comment → 400 with {"error": ...}
  - GET /v2/approvals/requests returns list with items+total
  - GET /v2/approvals/requests/{id} returns single request

Called by: pytest
Depends on: conftest (db_session, test_user), app.routers.approvals,
            app.services.approvals.service, app.models.approvals,
            app.models.quality_plan, app.dependencies.
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
from app.models.quality_plan import QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_approver(db: Session) -> User:
    u = User(
        email=f"approver-{uuid.uuid4().hex[:6]}@test.com",
        name="Approver",
        role="admin",
        azure_id=f"azure-approver-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_outsider(db: Session) -> User:
    u = User(
        email=f"outsider-{uuid.uuid4().hex[:6]}@test.com",
        name="Outsider",
        role="buyer",
        azure_id=f"azure-outsider-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_buy_plan(db: Session, user: User) -> BuyPlan:
    req = Requisition(
        name=f"REQ-TEST-{uuid.uuid4().hex[:6]}",
        customer_name="TestCo",
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
    )
    db.add(bp)
    db.flush()
    return bp


def _make_quality_plan(db: Session, bp: BuyPlan, user: User) -> QualityPlan:
    qp = QualityPlan(
        buy_plan_id=bp.id,
        created_by_id=user.id,
        status="in_review",
    )
    db.add(qp)
    db.flush()
    return qp


def _make_approval_chain(
    db: Session, qp: QualityPlan, user: User
) -> tuple[ApprovalRequest, ApprovalStep, ApprovalStepRecipient]:
    ar = ApprovalRequest(
        gate_type="buy_plan",
        status="requested",
        subject_quality_plan_id=qp.id,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()

    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()

    recipient = ApprovalStepRecipient(step_id=step.id, user_id=user.id, status="pending")
    db.add(recipient)
    db.commit()
    return ar, step, recipient


def _build_client(db: Session, acting_user: User, *, override_gatekeeper: bool = True) -> TestClient:
    """Build a TestClient with DB + user overrides.

    If override_gatekeeper=True, also overrides require_approval_gatekeeper to return
    acting_user directly (bypasses the recipient check). Set False to test the real
    gate.
    """
    from app.dependencies import require_approval_gatekeeper
    from app.main import app

    def _db():
        yield db

    def _user():
        return acting_user

    overrides: dict = {
        get_db: _db,
        require_user: _user,
    }
    if override_gatekeeper:
        overrides[require_approval_gatekeeper] = _user

    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


# ── Tests ────────────────────────────────────────────────────────────────


class TestPostDecision:
    def test_approve_by_pending_recipient(self, db_session: Session) -> None:
        """Pending recipient can approve → 200 + status=approved."""
        approver = _make_approver(db_session)
        bp = _make_buy_plan(db_session, approver)
        qp = _make_quality_plan(db_session, bp, approver)
        ar, _, _ = _make_approval_chain(db_session, qp, approver)

        for client in _build_client(db_session, approver, override_gatekeeper=True):
            resp = client.post(
                f"/v2/approvals/requests/{ar.id}/decision",
                data={"action": "approve", "comment": "LGTM"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["id"] == ar.id

    def test_non_recipient_gets_403(self, db_session: Session) -> None:
        """User with no PENDING recipient row is blocked by the gatekeeper (403)."""
        approver = _make_approver(db_session)
        outsider = _make_outsider(db_session)
        bp = _make_buy_plan(db_session, approver)
        qp = _make_quality_plan(db_session, bp, approver)
        ar, _, _ = _make_approval_chain(db_session, qp, approver)

        # Do NOT override gatekeeper — let it enforce the real check with outsider as user
        for client in _build_client(db_session, outsider, override_gatekeeper=False):
            resp = client.post(
                f"/v2/approvals/requests/{ar.id}/decision",
                data={"action": "approve"},
            )

        assert resp.status_code == 403

    def test_reject_without_comment_returns_400(self, db_session: Session) -> None:
        """Reject with no comment → 400 with {"error": ...}."""
        approver = _make_approver(db_session)
        bp = _make_buy_plan(db_session, approver)
        qp = _make_quality_plan(db_session, bp, approver)
        ar, _, _ = _make_approval_chain(db_session, qp, approver)

        for client in _build_client(db_session, approver, override_gatekeeper=True):
            resp = client.post(
                f"/v2/approvals/requests/{ar.id}/decision",
                data={"action": "reject"},
            )

        assert resp.status_code == 400
        assert "error" in resp.json()


class TestGetRequests:
    def test_list_returns_items_and_total(self, db_session: Session) -> None:
        """GET /v2/approvals/requests returns {items, total}."""
        approver = _make_approver(db_session)
        bp = _make_buy_plan(db_session, approver)
        qp = _make_quality_plan(db_session, bp, approver)
        _make_approval_chain(db_session, qp, approver)

        for client in _build_client(db_session, approver, override_gatekeeper=False):
            resp = client.get("/v2/approvals/requests")

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] >= 1

    def test_get_single_request(self, db_session: Session) -> None:
        """GET /v2/approvals/requests/{id} returns the request with matching id."""
        approver = _make_approver(db_session)
        bp = _make_buy_plan(db_session, approver)
        qp = _make_quality_plan(db_session, bp, approver)
        ar, _, _ = _make_approval_chain(db_session, qp, approver)

        for client in _build_client(db_session, approver, override_gatekeeper=False):
            resp = client.get(f"/v2/approvals/requests/{ar.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == ar.id
        assert body["gate_type"] == "buy_plan"
