"""test_sightings_advance_status.py — Tests for PATCH advance-status endpoint.

Covers: valid transition, invalid transition (409), ActivityLog creation,
        unauthorized access (401), missing status field (400), not-found (404).

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, client, unauthenticated_client,
            test_requisition)
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import SourcingStatus
from app.models import Requirement, Requisition
from app.models.intelligence import ActivityLog


@pytest.fixture()
def requirement_open(db_session: Session, test_requisition: Requisition) -> Requirement:
    """Return the first requirement from test_requisition, set to OPEN status."""
    req = db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first()
    req.sourcing_status = SourcingStatus.OPEN
    db_session.commit()
    db_session.refresh(req)
    return req


class TestAdvanceStatusValid:
    """Valid status transitions update the requirement and create activity."""

    def test_open_to_sourcing(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.SOURCING},
        )
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.SOURCING

    def test_open_to_archived(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.ARCHIVED},
        )
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.ARCHIVED

    def test_sourcing_to_offered(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        requirement_open.sourcing_status = SourcingStatus.SOURCING
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.OFFERED},
        )
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.OFFERED


class TestAdvanceStatusInvalid:
    """Invalid transitions return 409."""

    def test_open_to_won_rejected(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.WON},
        )
        assert resp.status_code == 409
        # Status should remain unchanged
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.OPEN

    def test_archived_is_terminal(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        requirement_open.sourcing_status = SourcingStatus.ARCHIVED
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.OPEN},
        )
        assert resp.status_code == 409


class TestAdvanceStatusActivityLog:
    """Activity log is created on successful transition."""

    def test_activity_log_created(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.SOURCING},
        )
        assert resp.status_code == 200

        log = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == requirement_open.id,
                ActivityLog.activity_type == "status_change",
            )
            .first()
        )
        assert log is not None
        assert "open" in log.notes.lower()
        assert "sourcing" in log.notes.lower()

    def test_no_activity_log_on_invalid_transition(
        self, client: TestClient, db_session: Session, requirement_open: Requirement
    ):
        client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.WON},
        )

        log_count = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == requirement_open.id,
                ActivityLog.activity_type == "status_change",
            )
            .count()
        )
        assert log_count == 0


class TestAdvanceStatusAuth:
    """Unauthorized users get 401."""

    def test_unauthenticated_returns_401(self, unauthenticated_client: TestClient, requirement_open: Requirement):
        resp = unauthenticated_client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.SOURCING},
        )
        # FastAPI returns 401 or redirects for unauthenticated users
        assert resp.status_code in (401, 403)


class TestAdvanceStatusEdgeCases:
    """Edge cases: missing status, not found, same status."""

    def test_missing_status_returns_400(self, client: TestClient, requirement_open: Requirement):
        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={},
        )
        assert resp.status_code == 400

    def test_not_found_returns_404(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/sightings/999999/advance-status",
            data={"status": SourcingStatus.SOURCING},
        )
        assert resp.status_code == 404

    def test_same_status_is_noop(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        """Transitioning to the same status is a valid no-op."""
        resp = client.patch(
            f"/v2/partials/sightings/{requirement_open.id}/advance-status",
            data={"status": SourcingStatus.OPEN},
        )
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.OPEN
