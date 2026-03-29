"""Tests for manual search button feature.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

from datetime import datetime, timezone

from app.models.sourcing import Requirement, Requisition


class TestRequirementLastSearchedAt:
    def test_requirement_has_last_searched_at_column(self, db_session):
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        assert r.last_searched_at is None

    def test_last_searched_at_accepts_datetime(self, db_session):
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        now = datetime.now(timezone.utc)
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-002",
            manufacturer="TestMfr",
            target_qty=50,
            sourcing_status="open",
            last_searched_at=now,
        )
        db_session.add(r)
        db_session.flush()
        assert r.last_searched_at == now
