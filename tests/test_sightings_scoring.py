"""Tests for sightings page priority scoring.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

from app.models.intelligence import ActivityLog
from app.models.sourcing import Requirement, Requisition


def test_requirement_has_priority_score(db_session):
    """Requirement model should have a priority_score column."""
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-001",
        manufacturer="TestMfr",
        priority_score=72.5,
    )
    db_session.add(r)
    db_session.flush()
    assert r.priority_score == 72.5


def test_requirement_has_assigned_buyer_id(db_session, test_user):
    """Requirement model should have an assigned_buyer_id column."""
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-002",
        manufacturer="TestMfr",
        assigned_buyer_id=test_user.id,
    )
    db_session.add(r)
    db_session.flush()
    assert r.assigned_buyer_id == test_user.id


def test_activity_log_has_requirement_id(db_session, test_user):
    """ActivityLog model should have a requirement_id FK column."""
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-003",
        manufacturer="TestMfr",
    )
    db_session.add(r)
    db_session.flush()
    log = ActivityLog(
        user_id=test_user.id,
        activity_type="rfq_sent",
        channel="email",
        requisition_id=req.id,
        requirement_id=r.id,
    )
    db_session.add(log)
    db_session.flush()
    assert log.requirement_id == r.id
