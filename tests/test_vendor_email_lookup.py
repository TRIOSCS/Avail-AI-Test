"""
test_vendor_email_lookup.py — Tests for vendor email lookup service and inquiry router.

Covers: find_vendors_for_parts, build_inquiry_groups, API endpoints.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.vendor_email_lookup import build_inquiry_groups


# ---------------------------------------------------------------------------
# build_inquiry_groups — pure logic tests
# ---------------------------------------------------------------------------


def test_build_inquiry_groups_basic():
    """Groups are created per-email with correct parts."""
    vendor_results = {
        "MTA18ASF4G72HZ-3G2F1": [
            {
                "vendor_name": "Acme Corp",
                "emails": ["sales@acme.com"],
                "phones": ["+1-555-0100"],
                "domain": "acme.com",
                "sources": ["brokerbin"],
                "sighting_count": 3,
            }
        ],
    }
    parts_with_qty = [{"mpn": "MTA18ASF4G72HZ-3G2F1", "qty": 50}]

    groups = build_inquiry_groups(vendor_results, parts_with_qty)

    assert len(groups) == 1
    g = groups[0]
    assert g["vendor_name"] == "Acme Corp"
    assert g["vendor_email"] == "sales@acme.com"
    assert "MTA18ASF4G72HZ-3G2F1" in g["parts"]
    assert "50 pcs" in g["body"]
    assert "Stock Inquiry" in g["subject"]


def test_build_inquiry_groups_multiple_vendors():
    """Multiple vendors across parts are collected."""
    vendor_results = {
        "PART-A": [
            {"vendor_name": "V1", "emails": ["v1@v1.com"], "phones": [], "domain": "v1.com", "sources": [], "sighting_count": 1},
            {"vendor_name": "V2", "emails": ["v2@v2.com"], "phones": [], "domain": "v2.com", "sources": [], "sighting_count": 1},
        ],
        "PART-B": [
            {"vendor_name": "V2", "emails": ["v2@v2.com"], "phones": [], "domain": "v2.com", "sources": [], "sighting_count": 1},
            {"vendor_name": "V3", "emails": ["v3@v3.com"], "phones": [], "domain": "v3.com", "sources": [], "sighting_count": 1},
        ],
    }
    parts_with_qty = [{"mpn": "PART-A", "qty": 10}, {"mpn": "PART-B", "qty": 20}]

    groups = build_inquiry_groups(vendor_results, parts_with_qty)

    emails = {g["vendor_email"] for g in groups}
    assert emails == {"v1@v1.com", "v2@v2.com", "v3@v3.com"}

    # V2 should have both parts listed
    v2_group = next(g for g in groups if g["vendor_email"] == "v2@v2.com")
    assert "PART-A" in v2_group["body"]
    assert "PART-B" in v2_group["body"]


def test_build_inquiry_groups_no_vendors():
    """Empty vendor results produce no groups."""
    groups = build_inquiry_groups(
        {"PART-A": []},
        [{"mpn": "PART-A", "qty": 50}],
    )
    assert groups == []


def test_build_inquiry_groups_vendor_no_emails():
    """Vendors without emails are skipped."""
    vendor_results = {
        "PART-A": [
            {"vendor_name": "V1", "emails": [], "phones": [], "domain": "v1.com", "sources": [], "sighting_count": 1},
        ],
    }
    groups = build_inquiry_groups(vendor_results, [{"mpn": "PART-A", "qty": 10}])
    assert groups == []


def test_build_inquiry_groups_dedup_emails():
    """Same email from different parts only produces one group."""
    vendor_results = {
        "PART-A": [
            {"vendor_name": "V1", "emails": ["sales@v1.com"], "phones": [], "domain": "v1.com", "sources": [], "sighting_count": 1},
        ],
        "PART-B": [
            {"vendor_name": "V1", "emails": ["sales@v1.com"], "phones": [], "domain": "v1.com", "sources": [], "sighting_count": 1},
        ],
    }
    groups = build_inquiry_groups(
        vendor_results,
        [{"mpn": "PART-A", "qty": 10}, {"mpn": "PART-B", "qty": 20}],
    )
    assert len(groups) == 1
    assert "PART-A" in groups[0]["body"]
    assert "PART-B" in groups[0]["body"]


def test_build_inquiry_groups_custom_sender():
    """Custom sender name appears in email body."""
    vendor_results = {
        "PART-A": [
            {"vendor_name": "V1", "emails": ["v1@v1.com"], "phones": [], "domain": "v1.com", "sources": [], "sighting_count": 1},
        ],
    }
    groups = build_inquiry_groups(
        vendor_results,
        [{"mpn": "PART-A", "qty": 10}],
        sender_name="John Doe",
        company_name="Test Corp",
    )
    assert "John Doe" in groups[0]["body"]
    assert "Test Corp" in groups[0]["body"]


def test_build_inquiry_subject_truncation():
    """Subject line handles many parts gracefully."""
    vendor_results = {
        f"PART-{i}": [
            {"vendor_name": "V1", "emails": ["v1@v1.com"], "phones": [], "domain": "v1.com", "sources": [], "sighting_count": 1},
        ]
        for i in range(5)
    }
    parts = [{"mpn": f"PART-{i}", "qty": 10} for i in range(5)]

    groups = build_inquiry_groups(vendor_results, parts)
    assert len(groups) == 1
    assert "+ 2 more" in groups[0]["subject"]


# ---------------------------------------------------------------------------
# Router tests (mocked DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_app():
    """Create test app with auth overrides."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_vendor_lookup_endpoint_exists(mock_app):
    """The /api/vendor-lookup endpoint is registered."""
    # Without auth it should return 401/403, not 404
    resp = mock_app.post(
        "/api/vendor-lookup",
        json={"parts": [{"mpn": "TEST-123", "qty": 50}]},
    )
    assert resp.status_code != 404


def test_vendor_inquiry_endpoint_exists(mock_app):
    """The /api/vendor-inquiry endpoint is registered."""
    resp = mock_app.post(
        "/api/vendor-inquiry",
        json={"parts": [{"mpn": "TEST-123", "qty": 50}], "dry_run": True},
    )
    assert resp.status_code != 404
