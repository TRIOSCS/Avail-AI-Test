"""Tests for manual search button feature.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import RequisitionStatus, SourcingStatus
from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary


class TestRequirementLastSearchedAt:
    def test_requirement_has_last_searched_at_column(self, db_session):
        req = Requisition(name="Test RFQ", status=RequisitionStatus.OPEN, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.flush()
        assert r.last_searched_at is None

    def test_last_searched_at_accepts_datetime(self, db_session):
        req = Requisition(name="Test RFQ", status=RequisitionStatus.OPEN, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        now = datetime.now(timezone.utc)
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-002",
            manufacturer="TestMfr",
            target_qty=50,
            sourcing_status=SourcingStatus.OPEN,
            last_searched_at=now,
        )
        db_session.add(r)
        db_session.flush()
        assert r.last_searched_at == now


class TestSearchRequirementStamp:
    """search_requirement() stamps last_searched_at only when at least one source
    returned status=ok (even with zero results).

    Without the total-failure guard, the 5-minute rate guard would silence retries after
    a failed search.
    """

    @pytest.mark.parametrize(
        "req_name, mpn, source_statuses, expect_stamped",
        [
            (
                "Stamp Test",
                "STAMP-001",
                [{"source": "nexar", "results": 0, "ms": 50, "error": None, "status": "ok"}],
                True,
            ),
            (
                "Fail Test",
                "FAIL-001",
                [
                    {"source": "nexar", "results": 0, "ms": 50, "error": "quota", "status": "error"},
                    {"source": "mouser", "results": 0, "ms": 50, "error": "auth", "status": "error"},
                ],
                False,
            ),
        ],
        ids=["stamps_on_success", "does_not_stamp_on_total_failure"],
    )
    @patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
    async def test_search_requirement_stamp(
        self, mock_fetch, db_session, req_name, mpn, source_statuses, expect_stamped
    ):
        mock_fetch.return_value = ([], source_statuses)

        req = Requisition(name=req_name, status=RequisitionStatus.OPEN, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.commit()

        assert r.last_searched_at is None

        from app.search_service import search_requirement

        await search_requirement(r, db_session)

        db_session.refresh(r)
        if expect_stamped:
            assert r.last_searched_at is not None
        else:
            assert r.last_searched_at is None


def _seed_requirement(db_session, mpn="RATE-001", last_searched_at=None):
    """Create a requisition + requirement for testing."""
    req = Requisition(name="Rate Test RFQ", status=RequisitionStatus.OPEN, customer_name="Acme")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        manufacturer="TestMfr",
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        last_searched_at=last_searched_at,
    )
    db_session.add(r)
    db_session.flush()
    # Add a vendor summary so detail panel renders
    vs = VendorSightingSummary(
        requirement_id=r.id,
        vendor_name="Test Vendor",
        estimated_qty=200,
        listing_count=1,
        score=50.0,
        tier="Good",
    )
    db_session.add(vs)
    db_session.commit()
    return req, r


class TestBatchEdgeCases:
    def test_empty_batch(self, client, db_session):
        """Empty batch should return 200 with zero counts."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "[]"},
        )
        assert resp.status_code == 200
        assert "0/0" in resp.text

    def test_malformed_json_returns_400(self, client, db_session):
        """Invalid JSON in requirement_ids should return 400."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "not-json"},
        )
        assert resp.status_code == 400

    def test_batch_exceeding_max_size(self, client, db_session):
        """More than 50 requirement IDs should return 400."""
        ids = list(range(1, 52))  # 51 IDs
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps(ids)},
        )
        assert resp.status_code == 400
        assert "Maximum" in resp.json().get("error", resp.text)
