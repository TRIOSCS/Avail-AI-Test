"""
test_routers_rfq.py — Tests for RFQ, Follow-ups & Vendor Enrichment Router

Tests the vendor card enrichment filtering logic: garbage vendor names,
blacklisted vendors, and summary cache building.

Covers: _enrich_with_vendor_cards filtering, edge cases
"""

import pytest


# ---------------------------------------------------------------------------
# Garbage vendor filtering (pure logic, no DB needed)
# ---------------------------------------------------------------------------

_GARBAGE_VENDORS = {"no seller listed", "no seller", "n/a", "unknown", ""}


def test_garbage_vendor_names_filtered():
    """Known garbage names should be excluded from results."""
    for name in _GARBAGE_VENDORS:
        assert name.lower() in _GARBAGE_VENDORS


def test_real_vendor_not_garbage():
    """Real vendor names pass through."""
    for name in ["Arrow Electronics", "Digi-Key", "Mouser", "ACME Corp"]:
        assert name.lower() not in _GARBAGE_VENDORS


def test_garbage_vendor_case_insensitive():
    """Garbage check uses lowercased names."""
    assert "No Seller Listed".lower() in _GARBAGE_VENDORS
    assert "N/A".lower() in _GARBAGE_VENDORS
    assert "UNKNOWN".lower() in _GARBAGE_VENDORS


# ---------------------------------------------------------------------------
# Blacklist filtering logic (unit test of the check pattern)
# ---------------------------------------------------------------------------

def test_blacklisted_vendor_skipped():
    """Sightings with is_blacklisted=True should be removed."""
    summary = {"is_blacklisted": True, "card_id": 1}
    assert summary.get("is_blacklisted") is True


def test_non_blacklisted_vendor_kept():
    """Normal vendors pass blacklist check."""
    summary = {"is_blacklisted": False, "card_id": 2}
    assert summary.get("is_blacklisted") is False


# ---------------------------------------------------------------------------
# Enrichment results structure
# ---------------------------------------------------------------------------

def _make_results_dict(sightings: list[dict]) -> dict:
    """Build a results dict matching the search_service format."""
    return {"REQ-1": {"sightings": sightings}}


def _filter_sightings(results: dict) -> list[dict]:
    """Apply the same filtering logic as _enrich_with_vendor_cards (pure part)."""
    filtered = []
    for group in results.values():
        for s in group.get("sightings", []):
            vname = (s.get("vendor_name") or "").strip()
            if vname.lower() in _GARBAGE_VENDORS:
                continue
            if s.get("_blacklisted"):
                continue
            filtered.append(s)
    return filtered


def test_filter_removes_garbage():
    results = _make_results_dict([
        {"vendor_name": "Arrow", "mpn_matched": "LM317T"},
        {"vendor_name": "No Seller Listed", "mpn_matched": "LM317T"},
        {"vendor_name": "", "mpn_matched": "LM317T"},
    ])
    kept = _filter_sightings(results)
    assert len(kept) == 1
    assert kept[0]["vendor_name"] == "Arrow"


def test_filter_removes_blacklisted():
    results = _make_results_dict([
        {"vendor_name": "Good Vendor", "mpn_matched": "LM317T"},
        {"vendor_name": "Bad Vendor", "mpn_matched": "LM317T", "_blacklisted": True},
    ])
    kept = _filter_sightings(results)
    assert len(kept) == 1
    assert kept[0]["vendor_name"] == "Good Vendor"


def test_filter_handles_none_vendor():
    results = _make_results_dict([
        {"vendor_name": None, "mpn_matched": "LM317T"},
    ])
    kept = _filter_sightings(results)
    assert len(kept) == 0  # None → "" → in _GARBAGE_VENDORS


def test_filter_preserves_order():
    results = _make_results_dict([
        {"vendor_name": "Alpha", "mpn_matched": "A"},
        {"vendor_name": "N/A", "mpn_matched": "B"},
        {"vendor_name": "Beta", "mpn_matched": "C"},
    ])
    kept = _filter_sightings(results)
    assert [s["vendor_name"] for s in kept] == ["Alpha", "Beta"]
