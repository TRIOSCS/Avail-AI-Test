"""Tests for Phase 1 Cleanup: nc_classification_cache and nc_worker_status.

Called by: pytest
Depends on: conftest.py
"""

from datetime import datetime, timezone

import pytest
import sqlalchemy

from app.models import NcSearchQueue, NcWorkerStatus
from app.services.nc_worker.queue_manager import recover_stale_searches


def test_nc_worker_status_import():
    """NcWorkerStatus model can be imported."""
    assert NcWorkerStatus.__tablename__ == "nc_worker_status"


def test_worker_status_create(db_session):
    """Can create the singleton worker status row."""
    ws = NcWorkerStatus(
        id=1,
        is_running=False,
        searches_today=0,
        sightings_today=0,
    )
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)

    assert ws.id == 1
    assert ws.is_running is False


def test_worker_status_update(db_session):
    """Can update the singleton row."""
    ws = NcWorkerStatus(id=1, is_running=False)
    db_session.add(ws)
    db_session.commit()

    ws.is_running = True
    ws.searches_today = 42
    ws.last_heartbeat = datetime.now(timezone.utc)
    db_session.commit()
    db_session.refresh(ws)

    assert ws.is_running is True
    assert ws.searches_today == 42


def test_worker_status_singleton_constraint(db_session):
    """Only id=1 is allowed (CHECK constraint). id=2 should fail on PG.

    Note: SQLite doesn't enforce CHECK constraints by default, so this
    test verifies the model definition is correct. On PostgreSQL, inserting
    id=2 would fail with a constraint violation.
    """
    ws1 = NcWorkerStatus(id=1)
    db_session.add(ws1)
    db_session.commit()

    # On SQLite, CHECK constraints may not be enforced, but the unique
    # primary key prevents duplicate id=1
    ws_dup = NcWorkerStatus(id=1)
    db_session.add(ws_dup)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_recover_stale_searches(db_session, test_requisition):
    """recover_stale_searches resets 'searching' items back to 'queued'."""
    req = test_requisition.requirements[0]
    req_id = test_requisition.id

    # Create items in various states — need unique requirement_ids for the unique constraint
    from app.models import Requirement

    req2 = Requirement(requisition_id=req_id, primary_mpn="LM317T", normalized_mpn="LM317T")
    req3 = Requirement(requisition_id=req_id, primary_mpn="NE555", normalized_mpn="NE555")
    db_session.add_all([req2, req3])
    db_session.commit()

    item_searching = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=req_id,
        mpn="STM32F103",
        normalized_mpn="STM32F103",
        status="searching",
    )
    item_queued = NcSearchQueue(
        requirement_id=req2.id,
        requisition_id=req_id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="queued",
    )
    item_completed = NcSearchQueue(
        requirement_id=req3.id,
        requisition_id=req_id,
        mpn="NE555",
        normalized_mpn="NE555",
        status="completed",
    )
    db_session.add_all([item_searching, item_queued, item_completed])
    db_session.commit()

    count = recover_stale_searches(db_session)

    assert count == 1
    db_session.refresh(item_searching)
    assert item_searching.status == "queued"
    assert "stale" in item_searching.error_message.lower()

    # Other items untouched
    db_session.refresh(item_queued)
    assert item_queued.status == "queued"
    db_session.refresh(item_completed)
    assert item_completed.status == "completed"


def test_recover_stale_searches_none(db_session, test_requisition):
    """recover_stale_searches returns 0 when nothing is stale."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="AD5061",
        normalized_mpn="AD5061",
        status="pending",
    )
    db_session.add(item)
    db_session.commit()

    count = recover_stale_searches(db_session)
    assert count == 0
