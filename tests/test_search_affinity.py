"""Tests for vendor affinity integration into the search service.

What: Verifies that vendor affinity suggestions are merged into search results
Called by: pytest
Depends on: app.search_service.search_requirement, app.services.vendor_affinity_service
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User
from app.search_service import search_requirement
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session) -> User:
    u = User(
        email="affinity-search@trioscs.com",
        name="Affinity Search Test",
        role="buyer",
        azure_id="aff-search-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="AFF-SEARCH-001",
        customer_name="Test Co",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db: Session, requisition: Requisition, mpn: str = "LM317T") -> Requirement:
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn=mpn,
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


MOCK_AFFINITY = [
    {
        "vendor_name": "Vendor Alpha",
        "vendor_id": 101,
        "mpn_count": 5,
        "manufacturer": "TI",
        "level": 1,
        "confidence": 0.65,
        "reasoning": "Vendor supplied 5 other MPN(s) from TI",
    },
    {
        "vendor_name": "Vendor Beta",
        "vendor_id": 102,
        "mpn_count": 3,
        "manufacturer": "TI",
        "level": 2,
        "confidence": 0.44,
        "reasoning": "Vendor shares commodity tags (3 matching tag(s))",
    },
    {
        "vendor_name": "Vendor Gamma",
        "vendor_id": 103,
        "mpn_count": 2,
        "manufacturer": "TI",
        "level": 3,
        "confidence": 0.32,
        "reasoning": "Vendor supplies parts in the same AI-classified category (2 MPN(s))",
    },
]

MOCK_FRESH = [
    {
        "vendor_name": "Arrow",
        "mpn_matched": "LM317T",
        "vendor_sku": "ARR-1",
        "source_type": "nexar",
        "is_authorized": True,
        "confidence": 5,
        "manufacturer": "TI",
        "qty_available": 1000,
        "unit_price": 0.50,
        "currency": "USD",
    },
]

MOCK_STATS = [
    {"source": "nexar", "results": 1, "ms": 100, "error": None, "status": "ok"},
]


# ── Tests ────────────────────────────────────────────────────────────────


class TestSearchIncludesVendorAffinity:
    @pytest.mark.asyncio
    async def test_search_includes_vendor_affinity(self, db_session):
        """Affinity suggestions appear in search results with
        source_type='vendor_affinity'."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        with (
            patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.find_vendor_affinity", return_value=list(MOCK_AFFINITY)),
        ):
            mock_fetch.return_value = (list(MOCK_FRESH), list(MOCK_STATS))
            result = await search_requirement(req, db_session)

        sightings = result["sightings"]
        affinity_results = [s for s in sightings if s.get("source_type") == "vendor_affinity"]
        assert len(affinity_results) == 3

        for ar in affinity_results:
            assert ar["is_affinity"] is True
            assert ar["is_historical"] is False

        vendor_names = {ar["vendor_name"] for ar in affinity_results}
        assert "Vendor Alpha" in vendor_names
        assert "Vendor Beta" in vendor_names
        assert "Vendor Gamma" in vendor_names


class TestAffinityResultFields:
    @pytest.mark.asyncio
    async def test_affinity_results_have_correct_fields(self, db_session):
        """Affinity results include source_badge, confidence_pct, and reasoning."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        with (
            patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.find_vendor_affinity", return_value=list(MOCK_AFFINITY)),
        ):
            mock_fetch.return_value = (list(MOCK_FRESH), list(MOCK_STATS))
            result = await search_requirement(req, db_session)

        affinity_results = [s for s in result["sightings"] if s.get("is_affinity")]

        for ar in affinity_results:
            assert ar["source_badge"] == "Vendor Match"
            assert isinstance(ar["confidence_pct"], int)
            assert 0 <= ar["confidence_pct"] <= 100
            assert isinstance(ar["reasoning"], str)
            assert len(ar["reasoning"]) > 0

        # Check specific confidence values (65%, 44%, 32%)
        alpha = next(r for r in affinity_results if r["vendor_name"] == "Vendor Alpha")
        assert alpha["confidence_pct"] == 65

        beta = next(r for r in affinity_results if r["vendor_name"] == "Vendor Beta")
        assert beta["confidence_pct"] == 44


class TestAffinityDedupWithLiveResults:
    @pytest.mark.asyncio
    async def test_affinity_dedup_with_live_results(self, db_session):
        """If a vendor already appears in live results, skip the affinity suggestion."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        # Affinity includes "Arrow" which is already in live results
        affinity_with_dupe = list(MOCK_AFFINITY) + [
            {
                "vendor_name": "Arrow",
                "vendor_id": 200,
                "mpn_count": 8,
                "manufacturer": "TI",
                "level": 1,
                "confidence": 0.70,
                "reasoning": "Vendor supplied 8 other MPN(s) from TI",
            },
        ]

        with (
            patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.find_vendor_affinity", return_value=affinity_with_dupe),
        ):
            mock_fetch.return_value = (list(MOCK_FRESH), list(MOCK_STATS))
            result = await search_requirement(req, db_session)

        sightings = result["sightings"]
        affinity_results = [s for s in sightings if s.get("is_affinity")]

        # Arrow should NOT appear as an affinity result (already in live)
        affinity_vendor_names = {ar["vendor_name"].lower() for ar in affinity_results}
        assert "arrow" not in affinity_vendor_names

        # The 3 non-duplicate affinity vendors should still be there
        assert len(affinity_results) == 3

        # Arrow should appear exactly once in total results (from live)
        arrow_results = [s for s in sightings if s.get("vendor_name", "").lower() == "arrow"]
        assert len(arrow_results) == 1
        assert arrow_results[0].get("source_type") == "nexar"
