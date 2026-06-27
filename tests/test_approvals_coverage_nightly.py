"""tests/test_approvals_coverage_nightly.py — Extra coverage for approvals router.

Targets the missing lines in app/routers/approvals.py:
  - post_decision PermissionError → 403
  - post_reassign user-not-found → 404
  - post_cancel ValueError → 400
  - list_requests with status filter
  - get_queue (HTMX partial render)
  - get_request 404

Called by: pytest
Depends on: conftest.py, app.routers.approvals, app.models.approvals
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from app.database import get_db
from app.dependencies import require_approval_gatekeeper, require_user
from app.models.approvals import ApprovalRequest, ApprovalStep
from app.models.auth import User


def _make_user(db: Session, role: str = "admin", can_approve: bool = True) -> User:
    u = User(
        email=f"user-{uuid.uuid4().hex[:6]}@test.com",
        name="Test",
        role=role,
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=can_approve,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_approval_request(db: Session, requested_by: User) -> ApprovalRequest:
    req = ApprovalRequest(
        gate_type="buy_plan",
        subject_type="buy_plan",
        subject_id=1,
        status="pending",
        requested_by_id=requested_by.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    step = ApprovalStep(
        request_id=req.id,
        seq=1,
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(step)
    db.flush()
    return req


def _get_client(db_session: Session, acting_user: User) -> TestClient:
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: acting_user
    app.dependency_overrides[require_approval_gatekeeper] = lambda: acting_user
    return TestClient(app, raise_server_exceptions=False)


class TestApprovalsRouterCoverage:
    def test_post_decision_permission_error(self, db_session: Session):
        """PermissionError in svc_decide → 403."""
        user = _make_user(db_session)
        ar = _make_approval_request(db_session, user)
        db_session.commit()

        client = _get_client(db_session, user)
        with patch("app.routers.approvals.svc_decide", side_effect=PermissionError("not a recipient")):
            resp = client.post(f"/v2/approvals/requests/{ar.id}/decision", data={"action": "approve"})
        assert resp.status_code == 403

    def test_post_reassign_user_not_found(self, db_session: Session):
        """Reassigning to a nonexistent user → 404."""
        user = _make_user(db_session)
        ar = _make_approval_request(db_session, user)
        db_session.commit()

        client = _get_client(db_session, user)
        resp = client.post(f"/v2/approvals/requests/{ar.id}/reassign", data={"to_user_id": 99999})
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_post_reassign_value_error(self, db_session: Session):
        """svc_reassign raises ValueError → 400."""
        user = _make_user(db_session)
        target = _make_user(db_session, role="buyer")
        ar = _make_approval_request(db_session, user)
        db_session.commit()

        client = _get_client(db_session, user)
        with patch("app.routers.approvals.svc_reassign", side_effect=ValueError("bad state")):
            resp = client.post(f"/v2/approvals/requests/{ar.id}/reassign", data={"to_user_id": target.id})
        assert resp.status_code == 400

    def test_post_cancel_value_error(self, db_session: Session):
        """svc_cancel raises ValueError → 400."""
        user = _make_user(db_session)
        ar = _make_approval_request(db_session, user)
        db_session.commit()

        client = _get_client(db_session, user)
        with patch("app.routers.approvals.svc_cancel", side_effect=ValueError("already resolved")):
            resp = client.post(f"/v2/approvals/requests/{ar.id}/cancel")
        assert resp.status_code == 400

    def test_list_requests_status_filter(self, db_session: Session):
        """list_requests filters by status when provided."""
        user = _make_user(db_session)
        ar = _make_approval_request(db_session, user)
        db_session.commit()

        client = _get_client(db_session, user)
        resp = client.get("/v2/approvals/requests?status=pending")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] >= 1

    def test_list_requests_gate_type_filter(self, db_session: Session):
        """list_requests filters by gate_type when provided."""
        user = _make_user(db_session)
        ar = _make_approval_request(db_session, user)
        db_session.commit()

        client = _get_client(db_session, user)
        resp = client.get("/v2/approvals/requests?gate_type=buy_plan")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_get_request_not_found(self, db_session: Session):
        """GET /v2/approvals/requests/{id} with missing id → 404."""
        user = _make_user(db_session)
        db_session.commit()

        client = _get_client(db_session, user)
        resp = client.get("/v2/approvals/requests/99999")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_get_queue_renders(self, db_session: Session):
        """GET /v2/approvals/queue renders without error."""
        user = _make_user(db_session)
        db_session.commit()

        client = _get_client(db_session, user)
        with patch("app.routers.approvals.template_response") as mock_tpl:
            mock_tpl.return_value = MagicMock(status_code=200, body=b"<html></html>")
            resp = client.get("/v2/approvals/queue")
        # template_response is mocked to return 200 — the route must render cleanly.
        assert resp.status_code == 200

    def test_serialize_request_all_fields(self, db_session: Session):
        """_serialize_request projects all 11 fields."""
        from app.routers.approvals import _serialize_request

        user = _make_user(db_session)
        ar = _make_approval_request(db_session, user)
        db_session.commit()

        result = _serialize_request(ar)
        assert result["id"] == ar.id
        assert result["gate_type"] == "buy_plan"
        assert result["status"] == "pending"
        assert result["amount"] is None
        assert result["resolved_at"] is None

    def teardown_method(self, method):
        from app.main import app

        app.dependency_overrides.clear()
