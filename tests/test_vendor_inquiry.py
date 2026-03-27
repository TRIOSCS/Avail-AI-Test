"""Tests for app/routers/vendor_inquiry.py — Vendor lookup and inquiry endpoints.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, sales_user),
            mocked vendor_email_lookup service
"""

from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

# ── Helpers ──────────────────────────────────────────────────────────

LOOKUP_URL = "/api/vendor-lookup"
INQUIRY_URL = "/api/vendor-inquiry"

MOCK_VENDOR_RESULTS = {
    "LM317T": [
        {
            "vendor_name": "Arrow Electronics",
            "emails": ["sales@arrow.com"],
            "sources": ["sightings"],
        }
    ],
}


# ── Vendor Lookup Tests ─────────────────────────────────────────────


class TestVendorLookup:
    """POST /api/vendor-lookup."""

    @patch(
        "app.routers.vendor_inquiry.find_vendors_for_parts",
        new_callable=AsyncMock,
        return_value=MOCK_VENDOR_RESULTS,
    )
    def test_lookup_with_valid_mpns_returns_vendor_list(self, mock_find, client):
        """Valid parts list returns vendors with summary stats."""
        resp = client.post(LOOKUP_URL, json={"parts": [{"mpn": "LM317T", "qty": 100}]})
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "summary" in data
        assert data["summary"]["parts_searched"] == 1
        assert data["summary"]["unique_vendors"] >= 1
        assert data["summary"]["unique_emails"] >= 1
        mock_find.assert_called_once()

    @patch(
        "app.routers.vendor_inquiry.find_vendors_for_parts",
        new_callable=AsyncMock,
        return_value={},
    )
    def test_lookup_with_no_results_returns_empty(self, mock_find, client):
        """Parts with no known vendors return empty results and zero summary."""
        resp = client.post(LOOKUP_URL, json={"parts": [{"mpn": "UNKNOWN-MPN-XYZ", "qty": 1}]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == {}
        assert data["summary"]["unique_vendors"] == 0
        assert data["summary"]["unique_emails"] == 0

    def test_lookup_with_empty_parts_list_returns_422(self, client):
        """Empty parts list fails Pydantic validation (min_length=1)."""
        resp = client.post(LOOKUP_URL, json={"parts": []})
        assert resp.status_code == 422


# ── Vendor Inquiry Tests ─────────────────────────────────────────────


class TestVendorInquiry:
    """POST /api/vendor-inquiry."""

    @patch(
        "app.routers.vendor_inquiry.require_fresh_token",
        new_callable=AsyncMock,
        return_value="mock-token",
    )
    @patch(
        "app.routers.vendor_inquiry.find_vendors_for_parts",
        new_callable=AsyncMock,
        return_value=MOCK_VENDOR_RESULTS,
    )
    @patch(
        "app.routers.vendor_inquiry.build_inquiry_groups",
        return_value=[
            {
                "vendor_name": "Arrow Electronics",
                "emails": ["sales@arrow.com"],
                "subject": "Stock Inquiry: LM317T",
                "body": "Do you have LM317T x100?",
            }
        ],
    )
    def test_dry_run_returns_preview(self, mock_groups, mock_find, mock_token, client):
        """dry_run=True returns email preview without sending."""
        resp = client.post(
            INQUIRY_URL,
            json={
                "parts": [{"mpn": "LM317T", "qty": 100}],
                "dry_run": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["dry_run"] is True
        assert data["total_emails"] == 1
        assert len(data["groups"]) == 1

    @patch(
        "app.routers.vendor_inquiry.require_fresh_token",
        new_callable=AsyncMock,
        return_value="mock-token",
    )
    @patch(
        "app.routers.vendor_inquiry.find_vendors_for_parts",
        new_callable=AsyncMock,
        return_value={"LM317T": []},
    )
    @patch(
        "app.routers.vendor_inquiry.build_inquiry_groups",
        return_value=[],
    )
    def test_no_vendors_found_returns_error(self, mock_groups, mock_find, mock_token, client):
        """When no vendor emails are found, returns ok=False with error message."""
        resp = client.post(
            INQUIRY_URL,
            json={
                "parts": [{"mpn": "LM317T", "qty": 100}],
                "dry_run": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "No vendor emails found" in data["error"]

    def test_inquiry_without_buyer_role_returns_403(self, db_session, sales_user):
        """Non-buyer user gets 403 from require_buyer dependency."""
        from app.database import get_db
        from app.dependencies import require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        def _override_user():
            return sales_user

        def _override_buyer():
            raise HTTPException(status_code=403, detail="Buyer role required")

        async def _override_fresh_token():
            return "mock-token"

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_user
        app.dependency_overrides[require_buyer] = _override_buyer
        app.dependency_overrides[require_fresh_token] = _override_fresh_token

        try:
            with TestClient(app) as c:
                resp = c.post(
                    INQUIRY_URL,
                    json={
                        "parts": [{"mpn": "LM317T", "qty": 100}],
                        "dry_run": True,
                    },
                )
                assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_buyer, None)
            app.dependency_overrides.pop(require_fresh_token, None)
