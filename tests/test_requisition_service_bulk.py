"""test_requisition_service_bulk.py — Characterization tests for the bulk requisition
service helpers.

Pins the two behaviors a review flagged as untested:
  1. SALES-role ownership filter in `batch_archive_for_user` (sales may only
     archive their own requisitions).
  2. Terminal-status exclusion (already-terminal requisitions are never
     re-archived / returned).

Called by: pytest
Depends on: app.services.requisition_service, app.constants, conftest fixtures
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models import Requisition, User
from app.schemas.responses import BatchAssignResponse, BulkArchiveResponse
from app.services.requisition_service import batch_archive_for_user


def _make_req(db: Session, *, owner_id: int, status: str = RequisitionStatus.ACTIVE) -> Requisition:
    """Seed a minimal requisition and return the committed row."""
    req = Requisition(
        name=f"REQ-{owner_id}-{status}",
        customer_name="Acme Electronics",
        status=status,
        created_by=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def test_batch_archive_for_user_sales_only_archives_own(db_session: Session, sales_user: User, test_user: User):
    """A SALES user passing a mix of their own and another user's req IDs gets only
    their own archived."""
    own = _make_req(db_session, owner_id=sales_user.id)
    other = _make_req(db_session, owner_id=test_user.id)

    archived = batch_archive_for_user(db_session, sales_user, [own.id, other.id])

    assert own.id in archived
    assert other.id not in archived
    assert archived == [own.id]

    db_session.expire_all()
    assert db_session.get(Requisition, own.id).status == RequisitionStatus.ARCHIVED
    assert db_session.get(Requisition, other.id).status == RequisitionStatus.ACTIVE


def test_batch_archive_excludes_terminal(db_session: Session, test_user: User):
    """A requisition already in a terminal status (WON) is not re-archived."""
    won = _make_req(db_session, owner_id=test_user.id, status=RequisitionStatus.WON)

    archived = batch_archive_for_user(db_session, test_user, [won.id])

    assert won.id not in archived
    assert archived == []

    db_session.expire_all()
    assert db_session.get(Requisition, won.id).status == RequisitionStatus.WON


def test_terminal_constant_values_and_not_a_member():
    """TERMINAL holds exactly the four done-statuses and is not an enum member."""
    assert RequisitionStatus.TERMINAL == frozenset(
        {
            RequisitionStatus.ARCHIVED,
            RequisitionStatus.WON,
            RequisitionStatus.LOST,
            RequisitionStatus.CANCELLED,
        }
    )
    assert "TERMINAL" not in RequisitionStatus.__members__


def test_bulk_archive_response_count_must_match_ids():
    """Count != len(ids) is rejected; matching values are accepted."""
    ok = BulkArchiveResponse(archived_count=2, archived_ids=[1, 2])
    assert ok.archived_count == len(ok.archived_ids)
    with pytest.raises(ValidationError):
        BulkArchiveResponse(archived_count=3, archived_ids=[1, 2])


def test_batch_assign_response_requires_assigned_to_and_count():
    """assigned_to is required; count must match ids."""
    ok = BatchAssignResponse(assigned_count=1, assigned_ids=[5], assigned_to="Jane")
    assert ok.assigned_to == "Jane"
    with pytest.raises(ValidationError):
        BatchAssignResponse(assigned_count=1, assigned_ids=[5])  # missing assigned_to
    with pytest.raises(ValidationError):
        BatchAssignResponse(assigned_count=2, assigned_ids=[5], assigned_to="Jane")
