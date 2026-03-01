"""Tests for NetComponents search queue and log models.

Verifies model creation, column defaults, foreign keys, and that
the migration schema is compatible with the SQLite test database.

Called by: pytest
Depends on: conftest.py (db_session, test_requisition fixtures)
"""

from datetime import datetime, timezone

from app.models import NcSearchLog, NcSearchQueue, Sighting


def test_nc_search_queue_import():
    """NcSearchQueue model can be imported from app.models."""
    assert NcSearchQueue.__tablename__ == "nc_search_queue"


def test_nc_search_log_import():
    """NcSearchLog model can be imported from app.models."""
    assert NcSearchLog.__tablename__ == "nc_search_log"


def test_nc_search_queue_create(db_session, test_requisition):
    """Can create an nc_search_queue entry with required fields."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="STM32F103C8T6",
        normalized_mpn="STM32F103C8T6",
        manufacturer="STMicroelectronics",
        status="pending",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.id is not None
    assert item.mpn == "STM32F103C8T6"
    assert item.status == "pending"
    assert item.priority == 3  # default
    assert item.search_count == 0  # default


def test_nc_search_queue_defaults(db_session, test_requisition):
    """Verify default values on nc_search_queue."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM358DR",
        normalized_mpn="LM358DR",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.gate_decision is None
    assert item.gate_reason is None
    assert item.last_searched_at is None
    assert item.results_count is None
    assert item.error_message is None


def test_nc_search_queue_status_transitions(db_session, test_requisition):
    """Can update status through the full lifecycle."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="AD8232ACPZ",
        normalized_mpn="AD8232ACPZ",
        status="pending",
    )
    db_session.add(item)
    db_session.commit()

    for status in ("queued", "searching", "completed", "failed"):
        item.status = status
        db_session.commit()
        db_session.refresh(item)
        assert item.status == status


def test_nc_search_log_create(db_session, test_requisition):
    """Can create an nc_search_log entry linked to a queue item."""
    req = test_requisition.requirements[0]
    queue_item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="P5040NSN72QC",
        normalized_mpn="P5040NSN72QC",
        status="completed",
    )
    db_session.add(queue_item)
    db_session.commit()

    log = NcSearchLog(
        queue_id=queue_item.id,
        duration_ms=3200,
        results_found=15,
        sightings_created=12,
        page_html_hash="a1b2c3d4" * 8,
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)

    assert log.id is not None
    assert log.queue_id == queue_item.id
    assert log.duration_ms == 3200
    assert log.results_found == 15
    assert log.sightings_created == 12


def test_nc_search_log_error(db_session, test_requisition):
    """Can record an error in the search log."""
    req = test_requisition.requirements[0]
    queue_item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="XC7A35T",
        normalized_mpn="XC7A35T",
        status="failed",
    )
    db_session.add(queue_item)
    db_session.commit()

    log = NcSearchLog(
        queue_id=queue_item.id,
        error="Timeout waiting for results",
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)

    assert log.error == "Timeout waiting for results"
    assert log.results_found is None


def test_sighting_source_searched_at(db_session, test_requisition):
    """Sighting model now has source_searched_at column."""
    req = test_requisition.requirements[0]
    now = datetime.now(timezone.utc)
    sighting = Sighting(
        requirement_id=req.id,
        vendor_name="Test Vendor",
        source_type="netcomponents",
        source_searched_at=now,
        qty_available=500,
    )
    db_session.add(sighting)
    db_session.commit()
    db_session.refresh(sighting)

    assert sighting.source_searched_at is not None
    assert sighting.source_type == "netcomponents"


def test_nc_search_queue_unique_requirement(db_session, test_requisition):
    """requirement_id has a unique constraint — can't queue same requirement twice."""
    req = test_requisition.requirements[0]
    item1 = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
    )
    db_session.add(item1)
    db_session.commit()

    item2 = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
    )
    db_session.add(item2)
    import pytest as pt
    import sqlalchemy

    with pt.raises(sqlalchemy.exc.IntegrityError):
        db_session.commit()
    db_session.rollback()
