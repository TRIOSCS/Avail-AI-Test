"""Tests for manual search button feature.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

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


class TestSearchRequirementStamp:
    @patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
    async def test_search_requirement_stamps_last_searched_at(self, mock_fetch, db_session):
        """search_requirement() should set requirement.last_searched_at after
        success."""
        mock_fetch.return_value = ([], [])

        req = Requisition(name="Stamp Test", status="active", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="STAMP-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        assert r.last_searched_at is None

        from app.search_service import search_requirement

        await search_requirement(r, db_session)

        db_session.refresh(r)
        assert r.last_searched_at is not None
