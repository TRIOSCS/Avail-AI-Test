"""Tests for manual search button feature.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.constants import RequisitionStatus, SourcingStatus
from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary


class TestRequirementLastSearchedAt:
    def test_requirement_has_last_searched_at_column(self, db_session):
        req = Requisition(name="Test RFQ", status=RequisitionStatus.ACTIVE, customer_name="Acme")
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
        req = Requisition(name="Test RFQ", status=RequisitionStatus.ACTIVE, customer_name="Acme")
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
    @patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
    async def test_search_requirement_stamps_last_searched_at_on_success(self, mock_fetch, db_session):
        """search_requirement() stamps last_searched_at when at least one source
        returned status=ok (even with zero results)."""
        mock_fetch.return_value = (
            [],
            [{"source": "nexar", "results": 0, "ms": 50, "error": None, "status": "ok"}],
        )

        req = Requisition(name="Stamp Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="STAMP-001",
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
        assert r.last_searched_at is not None

    @patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
    async def test_search_requirement_does_not_stamp_on_total_failure(self, mock_fetch, db_session):
        """search_requirement() does NOT stamp last_searched_at when every source
        errored.

        Without this guard, the 5-minute rate guard would silence retries after a failed
        search.
        """
        mock_fetch.return_value = (
            [],
            [
                {"source": "nexar", "results": 0, "ms": 50, "error": "quota", "status": "error"},
                {"source": "mouser", "results": 0, "ms": 50, "error": "auth", "status": "error"},
            ],
        )

        req = Requisition(name="Fail Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="FAIL-001",
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
        assert r.last_searched_at is None


def _seed_requirement(db_session, mpn="RATE-001", last_searched_at=None):
    """Create a requisition + requirement for testing."""
    req = Requisition(name="Rate Test RFQ", status=RequisitionStatus.ACTIVE, customer_name="Acme")
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


class TestSingleRefreshRateGuard:
    def test_refresh_returns_toast_when_recently_searched(self, client, db_session):
        """Refresh within 5 minutes should return info toast, not re-search."""
        now = datetime.now(timezone.utc)
        _, r = _seed_requirement(db_session, last_searched_at=now)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Already searched" in trigger

    def test_refresh_proceeds_when_not_recently_searched(self, client, db_session):
        """Refresh after 5 minutes should proceed normally."""
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        _, r = _seed_requirement(db_session, last_searched_at=old)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Already searched" not in trigger

    def test_refresh_proceeds_when_never_searched(self, client, db_session):
        """First-time search should always proceed."""
        _, r = _seed_requirement(db_session, last_searched_at=None)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Already searched" not in trigger


class TestBatchRefreshRateGuard:
    def test_batch_skips_recently_searched(self, client, db_session):
        """Batch refresh should skip recently-searched requirements."""
        now = datetime.now(timezone.utc)
        _, r1 = _seed_requirement(db_session, mpn="BATCH-001", last_searched_at=now)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        _, r2 = _seed_requirement(db_session, mpn="BATCH-002", last_searched_at=old)
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": f"[{r1.id}, {r2.id}]"},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower()


class TestRateGuardBoundary:
    def test_exactly_at_boundary_allows_refresh(self, client, db_session):
        """At exactly REFRESH_RATE_LIMIT_SECONDS, refresh should proceed."""
        boundary = datetime.now(timezone.utc) - timedelta(seconds=300)
        _, r = _seed_requirement(db_session, mpn="BOUNDARY-001", last_searched_at=boundary)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Already searched" not in trigger

    def test_one_second_before_boundary_blocks(self, client, db_session):
        """At 299 seconds, refresh should be blocked."""
        recent = datetime.now(timezone.utc) - timedelta(seconds=299)
        _, r = _seed_requirement(db_session, mpn="BOUNDARY-002", last_searched_at=recent)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Already searched" in trigger


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
