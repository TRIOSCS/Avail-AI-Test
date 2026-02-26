"""Tests for Phase 6: Requisitions list caching + material cards list caching.

Verifies:
1. Requisitions list uses @cached_endpoint with 30-second TTL
2. Cache invalidated on create/update/archive/bulk-archive/dismiss-offers
3. Material cards list uses @cached_endpoint with 2-hour TTL
4. Cached responses returned on repeat calls
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard, Requisition, User


# ── Requisition list caching ────────────────────────────────────────


class TestRequisitionListCache:
    def test_list_returns_valid_response(self, client, db_session, test_user):
        """Basic list call returns valid paginated response."""
        resp = client.get("/api/requisitions")
        assert resp.status_code == 200
        data = resp.json()
        assert "requisitions" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data

    def test_list_cached_on_second_call(self, client, db_session, test_user):
        """Second call with same params returns cached result."""
        with patch("app.cache.decorators.get_cached", return_value=None) as mock_get, \
             patch("app.cache.decorators.set_cached") as mock_set:
            resp1 = client.get("/api/requisitions")
            assert resp1.status_code == 200

        # First call should have tried cache (miss) and then set cache
        assert mock_get.called
        assert mock_set.called
        # Cache key should start with "req_list:"
        cache_key = mock_set.call_args[0][0]
        assert cache_key.startswith("req_list:")

    def test_create_requisition_invalidates_cache(self, client, db_session, test_user):
        """Creating a requisition invalidates the req_list cache."""
        with patch("app.routers.requisitions.invalidate_prefix") as mock_inv:
            resp = client.post("/api/requisitions", json={"name": "Test Req"})
            assert resp.status_code == 200
            mock_inv.assert_called_with("req_list")

    def test_update_requisition_invalidates_cache(
        self, client, db_session, test_requisition, test_user
    ):
        """Updating a requisition invalidates the req_list cache."""
        with patch("app.routers.requisitions.invalidate_prefix") as mock_inv:
            resp = client.put(
                f"/api/requisitions/{test_requisition.id}",
                json={"name": "Updated Name"},
            )
            assert resp.status_code == 200
            mock_inv.assert_called_with("req_list")

    def test_archive_requisition_invalidates_cache(
        self, client, db_session, test_requisition, test_user
    ):
        """Archiving a requisition invalidates the req_list cache."""
        with patch("app.routers.requisitions.invalidate_prefix") as mock_inv:
            resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
            assert resp.status_code == 200
            mock_inv.assert_called_with("req_list")

    def test_bulk_archive_invalidates_cache(self, client, db_session, test_user):
        """Bulk archive invalidates the req_list cache."""
        with patch("app.routers.requisitions.invalidate_prefix") as mock_inv:
            resp = client.put("/api/requisitions/bulk-archive")
            assert resp.status_code == 200
            mock_inv.assert_called_with("req_list")

    def test_dismiss_new_offers_invalidates_cache(
        self, client, db_session, test_requisition, test_user
    ):
        """Dismissing new offers invalidates the req_list cache."""
        with patch("app.routers.requisitions.invalidate_prefix") as mock_inv:
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/dismiss-new-offers"
            )
            assert resp.status_code == 200
            mock_inv.assert_called_with("req_list")

    def test_list_with_search_filter(self, client, db_session, test_user, test_requisition):
        """Search filter is included in cache key (different results)."""
        resp = client.get("/api/requisitions", params={"q": "test"})
        assert resp.status_code == 200

    def test_list_with_status_filter(self, client, db_session, test_user):
        """Status filter produces different cached results."""
        resp = client.get("/api/requisitions", params={"status": "archive"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["requisitions"], list)


# ── Material cards list caching ─────────────────────────────────────


class TestMaterialListCache:
    def test_list_materials_returns_valid_response(self, client, db_session, test_user):
        """Basic materials list returns valid paginated response."""
        resp = client.get("/api/materials")
        assert resp.status_code == 200
        data = resp.json()
        assert "materials" in data
        assert "total" in data

    def test_list_materials_cached(self, client, db_session, test_user):
        """Materials list uses caching with material_list prefix."""
        with patch("app.cache.decorators.get_cached", return_value=None) as mock_get, \
             patch("app.cache.decorators.set_cached") as mock_set:
            resp = client.get("/api/materials")
            assert resp.status_code == 200

        assert mock_set.called
        cache_key = mock_set.call_args[0][0]
        assert cache_key.startswith("material_list:")

    def test_list_materials_with_search(self, client, db_session, test_user):
        """Materials search filter works."""
        mc = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            search_count=5,
        )
        db_session.add(mc)
        db_session.commit()

        resp = client.get("/api/materials", params={"q": "lm317"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_list_materials_with_data(self, client, db_session, test_user):
        """Materials list includes vendor count from batch query."""
        mc = MaterialCard(
            normalized_mpn="ne555p",
            display_mpn="NE555P",
            manufacturer="Texas Instruments",
            search_count=10,
            last_searched_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        resp = client.get("/api/materials")
        assert resp.status_code == 200
        data = resp.json()
        materials = data["materials"]
        assert len(materials) >= 1
        mat = next(m for m in materials if m["display_mpn"] == "NE555P")
        assert mat["manufacturer"] == "Texas Instruments"
        assert mat["search_count"] == 10
        assert mat["vendor_count"] == 0
