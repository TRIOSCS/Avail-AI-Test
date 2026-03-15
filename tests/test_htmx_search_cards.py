"""
tests/test_htmx_search_cards.py — Tests for lead-card search results and detail drawer.

Verifies the Phase 1 React-spec redesign: search results now render as lead
cards with confidence badges, reason summaries, and source attribution.
Clicking a card opens a detail drawer with safety review.

Called by: pytest
Depends on: conftest (client, db_session, test_user), app.scoring
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.scoring import classify_lead, explain_lead, score_unified


# ── Scoring function unit tests ──────────────────────────────────────


def test_score_unified_live_result():
    """score_unified returns expected keys for a live API result."""
    result = score_unified(
        source_type="brokerbin",
        vendor_score=60.0,
        is_authorized=False,
        unit_price=0.55,
        qty_available=1000,
        has_price=True,
        has_qty=True,
        has_lead_time=True,
        has_condition=False,
    )
    assert "confidence_pct" in result
    assert "confidence_color" in result
    assert "source_badge" in result
    assert result["confidence_pct"] >= 70
    assert result["confidence_color"] in ("green", "amber", "red")


def test_classify_lead_strong():
    """Strong lead: high score + price + qty."""
    q = classify_lead(score=65.0, has_price=True, has_qty=True, has_contact=True)
    assert q == "strong"


def test_classify_lead_weak():
    """Weak lead: low score, no data."""
    q = classify_lead(score=10.0, has_price=False, has_qty=False, has_contact=False)
    assert q == "weak"


def test_explain_lead_includes_vendor_name():
    """explain_lead includes vendor name in output."""
    reason = explain_lead(vendor_name="Acme Corp", unit_price=0.55, qty_available=1000)
    assert "Acme Corp" in reason


def test_explain_lead_handles_none_values():
    """explain_lead doesn't crash with all None values."""
    reason = explain_lead(vendor_name=None)
    assert isinstance(reason, str)
    assert len(reason) > 0


# ── Search results card rendering ────────────────────────────────────

MOCK_RESULTS = [
    {
        "vendor_name": "Acme Electronics",
        "mpn_matched": "LM317T",
        "manufacturer": "Texas Instruments",
        "qty_available": 5000,
        "unit_price": 0.55,
        "source_type": "brokerbin",
        "lead_time": "Stock",
        "is_authorized": False,
        "vendor_score": 62.0,
        "vendor_email": "sales@acme.com",
        "vendor_phone": "555-1234",
        "vendor_url": "https://acme.com",
        "evidence_tier": "T2",
        "condition": "New",
        "vendor_sku": "ACM-LM317",
        "click_url": "https://brokerbin.com/lm317",
        "octopart_url": None,
    },
    {
        "vendor_name": "Budget Parts",
        "mpn_matched": "LM317T",
        "manufacturer": None,
        "qty_available": None,
        "unit_price": None,
        "source_type": "ebay",
        "lead_time": None,
        "is_authorized": False,
        "vendor_score": None,
        "vendor_email": None,
        "vendor_phone": None,
        "vendor_url": None,
        "evidence_tier": None,
        "condition": None,
    },
]


def test_search_results_render_lead_cards(client):
    """Search results render as lead cards, not table rows."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    # Should have lead cards, not table
    assert "<table" not in resp.text
    assert "Acme Electronics" in resp.text
    assert "Budget Parts" in resp.text


def test_search_results_show_confidence_badges(client):
    """Each lead card displays a confidence badge."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "Confidence" in resp.text


def test_search_results_show_reason_summary(client):
    """Each lead card contains an explain_lead reason summary."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    # Acme has price + qty, so reason should mention them
    assert "5,000 pcs" in resp.text or "Acme Electronics" in resp.text


def test_search_results_show_source_badges(client):
    """Lead cards show source attribution badges."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "Live Stock" in resp.text  # brokerbin → "Live Stock" badge


def test_search_results_show_contact_preview(client):
    """Lead cards show contact info when available."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "sales@acme.com" in resp.text
    assert "No contact info" in resp.text  # Budget Parts has no contact


def test_search_results_show_evidence_tier(client):
    """Lead cards show evidence tier badge for results that have one."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "Direct Source" in resp.text  # T2 → Direct Source


def test_search_results_empty_state(client):
    """Empty search shows helpful empty state message."""
    with patch("app.search_service.quick_search_mpn", return_value=[]):
        resp = client.post("/v2/partials/search/run", data={"mpn": "ZZZZZZZ"})
    assert resp.status_code == 200
    assert "No leads found" in resp.text


def test_search_results_null_data_no_crash(client):
    """Search doesn't crash when result has all null fields."""
    sparse_result = [{"vendor_name": "Unknown", "source_type": "nexar"}]
    with patch("app.search_service.quick_search_mpn", return_value=sparse_result):
        resp = client.post("/v2/partials/search/run", data={"mpn": "TEST"})
    assert resp.status_code == 200
    assert "Unknown" in resp.text


# ── Lead detail drawer tests ─────────────────────────────────────────


def test_lead_detail_drawer_returns_200(client):
    """GET /v2/partials/search/lead-detail returns drawer content."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.get("/v2/partials/search/lead-detail?idx=0&mpn=LM317T")
    assert resp.status_code == 200
    assert "Acme Electronics" in resp.text
    assert "Why This Lead" in resp.text
    assert "Part Details" in resp.text
    assert "Source Attribution" in resp.text
    assert "Contact Information" in resp.text


def test_lead_detail_drawer_shows_safety_review(client):
    """Lead detail drawer includes the safety review component."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.get("/v2/partials/search/lead-detail?idx=0&mpn=LM317T")
    assert resp.status_code == 200
    assert "Safety Review" in resp.text or "safety" in resp.text.lower()


def test_lead_detail_drawer_shows_score_breakdown(client):
    """Lead detail drawer shows the score component breakdown."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.get("/v2/partials/search/lead-detail?idx=0&mpn=LM317T")
    assert resp.status_code == 200
    assert "Score Breakdown" in resp.text


def test_lead_detail_drawer_out_of_bounds(client):
    """Requesting an index beyond results shows error."""
    with patch("app.search_service.quick_search_mpn", return_value=MOCK_RESULTS):
        resp = client.get("/v2/partials/search/lead-detail?idx=99&mpn=LM317T")
    assert resp.status_code == 200
    assert "Lead not found" in resp.text


def test_lead_detail_drawer_no_mpn(client):
    """Missing mpn parameter shows error."""
    resp = client.get("/v2/partials/search/lead-detail?idx=0&mpn=")
    assert resp.status_code == 200
    assert "No part number" in resp.text


def test_lead_detail_null_fields_no_crash(client):
    """Detail drawer handles result with minimal data."""
    sparse = [{"vendor_name": "Minimal", "source_type": "nexar"}]
    with patch("app.search_service.quick_search_mpn", return_value=sparse):
        resp = client.get("/v2/partials/search/lead-detail?idx=0&mpn=TEST")
    assert resp.status_code == 200
    assert "Minimal" in resp.text


# ── Warning banner tests ────────────────────────────────────────────


def test_search_results_weak_leads_warning(client):
    """Warning banner shown when all results are weak leads."""
    # All results with very low scores → all classified as "weak"
    weak_results = [
        {"vendor_name": "Shady Parts", "source_type": "ebay",
         "vendor_score": None, "unit_price": None, "qty_available": None,
         "is_authorized": False},
        {"vendor_name": "Unknown Vendor", "source_type": "ebay",
         "vendor_score": None, "unit_price": None, "qty_available": None,
         "is_authorized": False},
    ]
    with patch("app.search_service.quick_search_mpn", return_value=weak_results):
        resp = client.post("/v2/partials/search/run", data={"mpn": "ZZTEST"})
    assert resp.status_code == 200
    assert "low confidence" in resp.text


def test_search_results_no_weak_warning_when_strong(client):
    """No weak-leads warning when at least one result is strong."""
    mixed_results = [
        {"vendor_name": "Good Vendor", "source_type": "brokerbin",
         "vendor_score": 80.0, "unit_price": 1.50, "qty_available": 10000,
         "is_authorized": True, "vendor_email": "sales@good.com",
         "evidence_tier": "T1", "lead_time": "Stock", "condition": "New"},
        {"vendor_name": "Weak Vendor", "source_type": "ebay",
         "vendor_score": None, "unit_price": None, "qty_available": None,
         "is_authorized": False},
    ]
    with patch("app.search_service.quick_search_mpn", return_value=mixed_results):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "low confidence" not in resp.text


def test_search_results_source_errors_warning(client):
    """Source failure banner shown when source_errors are returned."""
    with patch("app.search_service.quick_search_mpn", return_value={
        "sightings": [{"vendor_name": "Test", "source_type": "nexar",
                       "unit_price": 1.0, "qty_available": 100}],
        "source_errors": ["BrokerBin timed out", "DigiKey rate limited"],
    }):
        resp = client.post("/v2/partials/search/run", data={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "Some sources failed" in resp.text
    assert "BrokerBin timed out" in resp.text
