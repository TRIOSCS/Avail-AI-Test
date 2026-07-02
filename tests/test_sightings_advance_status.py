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

from app.constants import ActivityType, SourcingStatus
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


def advance_status(client: TestClient, requirement_id, status=None):
    """PATCH the advance-status endpoint; omit ``status`` to send an empty body."""
    data = {} if status is None else {"status": status}
    return client.patch(f"/v2/partials/sightings/{requirement_id}/advance-status", data=data)


class TestAdvanceStatusValid:
    """Valid status transitions update the requirement and create activity."""

    def test_open_to_sourcing(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        resp = advance_status(client, requirement_open.id, SourcingStatus.SOURCING)
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.SOURCING

    def test_open_to_archived(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        resp = advance_status(client, requirement_open.id, SourcingStatus.ARCHIVED)
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.ARCHIVED

    def test_sourcing_to_offered(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        requirement_open.sourcing_status = SourcingStatus.SOURCING
        db_session.commit()

        resp = advance_status(client, requirement_open.id, SourcingStatus.OFFERED)
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.OFFERED

    def test_open_to_won_skip_ahead(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        """Skip-ahead is legal (single source of truth): open → won succeeds."""
        resp = advance_status(client, requirement_open.id, SourcingStatus.WON)
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.WON

    def test_archived_reopens_to_open(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        """A requirement is re-openable: archived → open (un-archive) succeeds."""
        requirement_open.sourcing_status = SourcingStatus.ARCHIVED
        db_session.commit()

        resp = advance_status(client, requirement_open.id, SourcingStatus.OPEN)
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.OPEN


class TestAdvanceStatusInvalid:
    """Invalid transitions return 409."""

    def test_won_to_sourcing_rejected(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        """Won only transitions to lost/archived — won → sourcing is illegal."""
        requirement_open.sourcing_status = SourcingStatus.WON
        db_session.commit()

        resp = advance_status(client, requirement_open.id, SourcingStatus.SOURCING)
        assert resp.status_code == 409
        # Status should remain unchanged
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.WON

    def test_archived_only_reopens_to_open(
        self, client: TestClient, db_session: Session, requirement_open: Requirement
    ):
        """Archived only transitions to open — archived → sourcing is illegal."""
        requirement_open.sourcing_status = SourcingStatus.ARCHIVED
        db_session.commit()

        resp = advance_status(client, requirement_open.id, SourcingStatus.SOURCING)
        assert resp.status_code == 409


class TestAdvanceStatusActivityLog:
    """Activity log is created on successful transition."""

    def test_activity_log_created(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        resp = advance_status(client, requirement_open.id, SourcingStatus.SOURCING)
        assert resp.status_code == 200

        log = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == requirement_open.id,
                ActivityLog.activity_type == ActivityType.STATUS_CHANGED,
            )
            .first()
        )
        assert log is not None
        assert "open" in log.notes.lower()
        assert "sourcing" in log.notes.lower()

    def test_no_activity_log_on_invalid_transition(
        self, client: TestClient, db_session: Session, requirement_open: Requirement
    ):
        # won → sourcing is illegal, so no status-change activity is logged.
        requirement_open.sourcing_status = SourcingStatus.WON
        db_session.commit()

        advance_status(client, requirement_open.id, SourcingStatus.SOURCING)

        log_count = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == requirement_open.id,
                ActivityLog.activity_type == ActivityType.STATUS_CHANGED,
            )
            .count()
        )
        assert log_count == 0


class TestAdvanceStatusAuth:
    """Unauthorized users get 401."""

    def test_unauthenticated_returns_401(self, unauthenticated_client: TestClient, requirement_open: Requirement):
        resp = advance_status(unauthenticated_client, requirement_open.id, SourcingStatus.SOURCING)
        # FastAPI returns 401 or redirects for unauthenticated users
        assert resp.status_code in (401, 403)


class TestAdvanceStatusEdgeCases:
    """Edge cases: missing status, not found, same status."""

    def test_missing_status_returns_400(self, client: TestClient, requirement_open: Requirement):
        resp = advance_status(client, requirement_open.id)
        assert resp.status_code == 400

    def test_not_found_returns_404(self, client: TestClient):
        resp = advance_status(client, 999999, SourcingStatus.SOURCING)
        assert resp.status_code == 404

    def test_same_status_is_noop(self, client: TestClient, db_session: Session, requirement_open: Requirement):
        """Transitioning to the same status is a valid no-op."""
        resp = advance_status(client, requirement_open.id, SourcingStatus.OPEN)
        assert resp.status_code == 200
        db_session.refresh(requirement_open)
        assert requirement_open.sourcing_status == SourcingStatus.OPEN
