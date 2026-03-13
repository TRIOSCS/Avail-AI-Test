"""Tests for Apollo sync router.

Tests all /api/apollo/* endpoints with mocked service layer.
Called by: pytest
Depends on: app.routers.apollo_sync, conftest fixtures
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _skip_if_apollo_router_disabled(client):
    has_route = any(getattr(route, "path", "") == "/api/apollo/discover/{domain}" for route in client.app.routes)
    if not has_route:
        pytest.skip("Apollo sync router disabled in MVP mode")


class TestApolloDiscover:
    def test_discover_success(self, client):
        mock_result = {
            "domain": "acme.com",
            "contacts": [
                {
                    "apollo_id": "abc",
                    "full_name": "Jane Doe",
                    "title": "VP Procurement",
                }
            ],
            "total_found": 1,
        }
        with patch(
            "app.routers.apollo_sync.discover_contacts",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.get("/api/apollo/discover/acme.com")
        assert resp.status_code == 200
        assert resp.json()["total_found"] == 1

    def test_discover_with_params(self, client):
        mock_result = {"domain": "acme.com", "contacts": [], "total_found": 0}
        with patch(
            "app.routers.apollo_sync.discover_contacts",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.get("/api/apollo/discover/acme.com?max_results=5")
        assert resp.status_code == 200


class TestApolloEnrich:
    def test_enrich_success(self, client, db_session):
        from app.models import VendorCard

        vc = VendorCard(display_name="Acme", normalized_name="acme_rt", source="manual")
        db_session.add(vc)
        db_session.commit()

        mock_result = {
            "enriched": 1,
            "verified": 1,
            "credits_used": 1,
            "credits_remaining": 94,
            "contacts": [],
        }
        with patch(
            "app.routers.apollo_sync.enrich_selected_contacts",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/apollo/enrich",
                json={
                    "apollo_ids": ["abc123"],
                    "vendor_card_id": vc.id,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["enriched"] == 1

    def test_enrich_empty_ids(self, client):
        resp = client.post(
            "/api/apollo/enrich",
            json={
                "apollo_ids": [],
                "vendor_card_id": 1,
            },
        )
        assert resp.status_code == 422


class TestApolloCredits:
    def test_credits_success(self, client):
        mock_result = {
            "lead_credits_remaining": 90,
            "lead_credits_used": 5,
            "direct_dial_remaining": 160,
            "direct_dial_used": 0,
            "ai_credits_remaining": 5000,
            "ai_credits_used": 0,
        }
        with patch(
            "app.routers.apollo_sync.get_credits",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.get("/api/apollo/credits")
        assert resp.status_code == 200
        assert resp.json()["lead_credits_remaining"] == 90


class TestApolloSync:
    def test_sync_success(self, client):
        mock_result = {"synced": 3, "skipped": 1, "errors": 0}
        with patch(
            "app.routers.apollo_sync.sync_contacts_to_apollo",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post("/api/apollo/sync-contacts")
        assert resp.status_code == 200
        assert resp.json()["synced"] == 3
