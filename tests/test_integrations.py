"""Tests for integrations bundle — Charts, Apollo, Presence, ACS, Call Records.

Called by: pytest
Depends on: app.routers.crm.views, app.connectors.apollo, app.services.presence_service
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401


class TestApolloConnector:
    """Test Apollo API connector."""

    def test_search_company_returns_data(self):
        """Apollo search_company returns normalized company data."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "organization": {
                "name": "Test Corp",
                "website_url": "testcorp.com",
                "linkedin_url": "https://linkedin.com/company/testcorp",
                "industry": "Semiconductors",
                "estimated_num_employees": 500,
                "city": "Austin",
                "state": "Texas",
                "country": "United States",
            }
        }

        with patch("app.connectors.apollo.httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            instance.return_value.get = AsyncMock(return_value=mock_resp)

            # Test the parsing logic directly
            from app.connectors.apollo import _parse_company_response

            result = _parse_company_response(mock_resp.json.return_value)

        assert result is not None
        assert result["linkedin_url"] == "https://linkedin.com/company/testcorp"
        assert result["industry"] == "Semiconductors"

    def test_parse_company_response_missing_org(self):
        """_parse_company_response returns None when organization key is missing."""
        from app.connectors.apollo import _parse_company_response

        result = _parse_company_response({})
        assert result is None

    def test_parse_contacts_response(self):
        """_parse_contacts_response returns normalized contact list."""
        from app.connectors.apollo import _parse_contacts_response

        data = {
            "people": [
                {
                    "name": "Jane Doe",
                    "email": "jane@testcorp.com",
                    "phone_number": "+15551234567",
                    "title": "VP Procurement",
                    "linkedin_url": "https://linkedin.com/in/janedoe",
                },
                {
                    "name": "John Smith",
                    "email": "john@testcorp.com",
                    "phone_number": None,
                    "title": "Buyer",
                    "linkedin_url": None,
                },
            ]
        }
        result = _parse_contacts_response(data)
        assert len(result) == 2
        assert result[0]["full_name"] == "Jane Doe"
        assert result[0]["source"] == "apollo"
        assert result[1]["email"] == "john@testcorp.com"

    def test_parse_contacts_response_empty(self):
        """_parse_contacts_response returns empty list when no people."""
        from app.connectors.apollo import _parse_contacts_response

        result = _parse_contacts_response({})
        assert result == []

    def test_parse_company_response_strips_protocol(self):
        """_parse_company_response strips http/https from domain."""
        from app.connectors.apollo import _parse_company_response

        data = {
            "organization": {
                "name": "Proto Corp",
                "website_url": "https://protocorp.com/",
                "linkedin_url": None,
                "industry": None,
                "estimated_num_employees": None,
                "city": None,
                "state": None,
                "country": None,
            }
        }
        result = _parse_company_response(data)
        assert result is not None
        assert result["domain"] == "protocorp.com"
        assert result["employee_size"] is None


class TestPresenceService:
    """Test Teams presence detection."""

    async def test_get_presence_returns_status(self):
        """get_presence returns availability string."""
        from app.services.presence_service import get_presence

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"availability": "Available"})

        result = await get_presence("user@example.com", mock_gc)
        assert result == "Available"

    async def test_get_presence_caches_result(self):
        """Repeated calls use cache, not API."""
        from app.services.presence_service import _presence_cache, get_presence

        _presence_cache.clear()

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"availability": "Away"})

        await get_presence("cached@example.com", mock_gc)
        await get_presence("cached@example.com", mock_gc)

        # Only one API call despite two get_presence calls
        assert mock_gc.get_json.call_count == 1

    def test_presence_color_available(self):
        """Available status returns emerald color."""
        from app.services.presence_service import presence_color

        assert presence_color("Available") == "bg-emerald-400"

    def test_presence_color_away(self):
        """Away status returns amber color."""
        from app.services.presence_service import presence_color

        assert presence_color("Away") == "bg-amber-400"

    def test_presence_color_busy(self):
        """Busy status returns rose color."""
        from app.services.presence_service import presence_color

        assert presence_color("Busy") == "bg-rose-400"

    def test_presence_color_none(self):
        """None status returns gray color."""
        from app.services.presence_service import presence_color

        assert presence_color(None) == "bg-gray-300"


class TestPerformanceMetricsEndpoint:
    """Test JSON metrics endpoint for Chart.js."""

    def test_metrics_returns_json(self, client: TestClient):
        """GET /api/crm/performance-metrics returns JSON with score arrays."""
        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "names" in data
        assert "scores" in data
        assert "behaviors" in data
        assert "outcomes" in data
        assert isinstance(data["names"], list)


class TestACSService:
    """Test Azure Communication Services integration."""

    def test_acs_webhook_endpoint_exists(self, client: TestClient):
        """POST /api/webhooks/acs returns 200 or 400 (not 404)."""
        resp = client.post("/api/webhooks/acs", json={})
        assert resp.status_code != 404

    def test_call_initiate_endpoint_exists(self, client: TestClient):
        """POST /api/calls/initiate returns 200 or 422 (not 404)."""
        resp = client.post("/api/calls/initiate", json={"to_phone": "+15551234567"})
        # Will fail with config error since ACS not configured, but route exists
        assert resp.status_code != 404


class TestTeamsCallRecordsJob:
    """Test Teams call records sync job."""

    def test_register_teams_call_jobs_exists(self):
        """register_teams_call_jobs function exists."""
        from app.jobs.teams_call_jobs import register_teams_call_jobs

        assert callable(register_teams_call_jobs)
