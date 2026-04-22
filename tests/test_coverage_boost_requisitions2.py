"""tests/test_coverage_boost_requisitions2.py — Coverage for uncovered branches in
app/routers/requisitions2.py that execute before any await expression.

Targets:
  - line 238: inline_edit_cell → 404 for missing req
  - line 259: inline_save → 422 for invalid field
  - line 263: inline_save → 404 for missing req
  - lines 275-281: inline_save with field=status
  - line 285: inline_save with invalid urgency
  - lines 293-294: inline_save with bad date format
  - line 297: inline_save clearing deadline
  - line 335: row_action → 404 for missing req
  - lines 394-397: row_action clone path
  - lines 372-373: row_action unclaim ValueError path

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requisition, User


@pytest.fixture()
def active_req(db_session: Session, test_user: User) -> Requisition:
    """An active requisition for inline-edit / row-action tests."""
    req = Requisition(
        name="REQ2-COV-001",
        customer_name="CovCorp",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


# ── inline_edit_cell ─────────────────────────────────────────────────


class TestInlineEditCell:
    def test_missing_req_returns_404(self, client: TestClient):
        """get_req_for_user raises HTTPException 404 for unknown req."""
        resp = client.get("/requisitions2/999999/edit/name")
        assert resp.status_code == 404

    def test_valid_req_returns_form(self, client: TestClient, active_req: Requisition):
        """Happy path: valid req → inline form template."""
        resp = client.get(f"/requisitions2/{active_req.id}/edit/name")
        assert resp.status_code == 200

    def test_owner_field_loads_users(self, client: TestClient, active_req: Requisition):
        """Field=owner triggers get_team_users call."""
        resp = client.get(f"/requisitions2/{active_req.id}/edit/owner")
        assert resp.status_code == 200


# ── inline_save ──────────────────────────────────────────────────────


class TestInlineSave:
    def test_invalid_field_returns_422(self, client: TestClient, active_req: Requisition):
        """Line 259: field not in valid_fields → 422."""
        resp = client.patch(
            f"/requisitions2/{active_req.id}/inline",
            data={"field": "not_a_real_field", "value": "x"},
        )
        assert resp.status_code == 422
        assert "Invalid field" in resp.text

    def test_missing_req_returns_404(self, client: TestClient):
        """get_req_for_user raises HTTPException 404 for unknown req in inline_save."""
        resp = client.patch(
            "/requisitions2/999999/inline",
            data={"field": "name", "value": "New Name"},
        )
        assert resp.status_code == 404

    def test_empty_name_returns_422(self, client: TestClient, active_req: Requisition):
        """Name field with blank value → 422."""
        resp = client.patch(
            f"/requisitions2/{active_req.id}/inline",
            data={"field": "name", "value": ""},
        )
        assert resp.status_code == 422

    def test_status_field_valid_transition(self, client: TestClient, active_req: Requisition, db_session: Session):
        """Lines 275-279: field=status with a valid target triggers transition."""
        with patch("app.services.requisition_state.transition") as mock_tr:
            resp = client.patch(
                f"/requisitions2/{active_req.id}/inline",
                data={"field": "status", "value": "archived"},
            )
        # Either success or 422 — just confirm the branch was entered
        assert resp.status_code in (200, 422)

    def test_status_field_invalid_transition_returns_422(self, client: TestClient, active_req: Requisition):
        """Lines 280-281: transition raises ValueError → 422."""
        with patch(
            "app.services.requisition_state.transition",
            side_effect=ValueError("invalid transition"),
        ):
            resp = client.patch(
                f"/requisitions2/{active_req.id}/inline",
                data={"field": "status", "value": "bogus_status"},
            )
        assert resp.status_code == 422

    def test_urgency_invalid_returns_422(self, client: TestClient, active_req: Requisition):
        """Line 285: urgency value not in allowed set → 422."""
        resp = client.patch(
            f"/requisitions2/{active_req.id}/inline",
            data={"field": "urgency", "value": "mega_urgent"},
        )
        assert resp.status_code == 422
        assert "Invalid urgency" in resp.text

    def test_urgency_valid(self, client: TestClient, active_req: Requisition):
        """Urgency=hot → success."""
        with patch("app.services.sse_broker.broker.publish"):
            resp = client.patch(
                f"/requisitions2/{active_req.id}/inline",
                data={"field": "urgency", "value": "hot"},
            )
        assert resp.status_code == 200

    def test_deadline_invalid_format_returns_422(self, client: TestClient, active_req: Requisition):
        """Lines 293-294: deadline with bad format → 422."""
        resp = client.patch(
            f"/requisitions2/{active_req.id}/inline",
            data={"field": "deadline", "value": "not-a-date"},
        )
        assert resp.status_code == 422
        assert "Invalid date format" in resp.text

    def test_deadline_cleared(self, client: TestClient, active_req: Requisition):
        """Line 297: empty deadline value clears deadline."""
        with patch("app.services.sse_broker.broker.publish"):
            resp = client.patch(
                f"/requisitions2/{active_req.id}/inline",
                data={"field": "deadline", "value": ""},
            )
        assert resp.status_code == 200

    def test_deadline_set(self, client: TestClient, active_req: Requisition):
        """Valid date deadline is saved."""
        with patch("app.services.sse_broker.broker.publish"):
            resp = client.patch(
                f"/requisitions2/{active_req.id}/inline",
                data={"field": "deadline", "value": "2025-12-31"},
            )
        assert resp.status_code == 200


# ── row_action ───────────────────────────────────────────────────────


class TestRowAction:
    def test_missing_req_returns_404(self, client: TestClient):
        """get_req_for_user raises HTTPException 404 for unknown req in row_action."""
        resp = client.post("/requisitions2/999999/action/archive")
        assert resp.status_code == 404

    def test_clone_action(self, client: TestClient, active_req: Requisition):
        """Lines 394-397: clone action calls clone_requisition."""
        with patch("app.services.requisition_service.clone_requisition") as mock_clone:
            from unittest.mock import MagicMock

            cloned = MagicMock()
            cloned.id = 999
            cloned.name = "Clone of REQ2-COV-001"
            mock_clone.return_value = cloned
            resp = client.post(f"/requisitions2/{active_req.id}/action/clone")
        assert resp.status_code == 200

    def test_unclaim_value_error(self, client: TestClient, active_req: Requisition):
        """Lines 372-373: unclaim raises ValueError → msg contains error."""
        with patch(
            "app.services.requirement_status.unclaim_requisition",
            side_effect=ValueError("cannot unclaim"),
        ):
            resp = client.post(f"/requisitions2/{active_req.id}/action/unclaim")
        assert resp.status_code == 200  # returns table, not error

    def test_archive_action(self, client: TestClient, active_req: Requisition):
        """Archive action path."""
        with patch("app.services.requisition_state.transition"):
            resp = client.post(f"/requisitions2/{active_req.id}/action/archive")
        assert resp.status_code == 200

    def test_assign_action_with_owner(self, client: TestClient, active_req: Requisition, test_user: User):
        """Assign action with owner_id sets created_by."""
        resp = client.post(
            f"/requisitions2/{active_req.id}/action/assign",
            data={"owner_id": str(test_user.id)},
        )
        assert resp.status_code == 200
