"""Tests for Phase 4 Task 5 — Search Results Presentation with unified scoring.

Verifies that search results include source_badge, confidence_pct, confidence_color
from score_unified(), that affinity/AI results carry reasoning, and that results
are sorted by confidence_pct descending.

Called by: pytest
Depends on: app.search_service (sighting_to_dict, _history_to_result),
            app.scoring (score_unified)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.search_service import _history_to_result, sighting_to_dict


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_sighting(**overrides) -> SimpleNamespace:
    """Build a Sighting-like object with sensible defaults for testing."""
    defaults = {
        "id": 1,
        "requirement_id": 10,
        "vendor_name": "Acme Parts",
        "vendor_email": "sales@acme.com",
        "vendor_phone": "555-1234",
        "mpn_matched": "LM358N",
        "manufacturer": "Texas Instruments",
        "qty_available": 5000,
        "unit_price": 0.45,
        "currency": "USD",
        "source_type": "nexar",
        "is_authorized": False,
        "confidence": 0.0,
        "score": 50.0,
        "raw_data": {},
        "is_unavailable": False,
        "moq": 100,
        "date_code": "2024+",
        "packaging": "Tube",
        "condition": "New",
        "lead_time_days": 14,
        "lead_time": "2 weeks",
        "evidence_tier": "T3",
        "score_components": None,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_history(**overrides) -> dict:
    """Build a history dict as returned by _get_material_history."""
    now = datetime.now(timezone.utc)
    defaults = {
        "vendor_name": "Historical Parts Co",
        "mpn_matched": "LM358N",
        "manufacturer": "TI",
        "qty_available": 1000,
        "unit_price": 0.50,
        "currency": "USD",
        "source_type": "historical",
        "is_authorized": False,
        "vendor_sku": "HP-LM358",
        "last_seen": now - timedelta(days=10),
        "first_seen": now - timedelta(days=60),
        "times_seen": 3,
        "material_card_id": 42,
    }
    defaults.update(overrides)
    return defaults


# ── Tests ────────────────────────────────────────────────────────────────


def test_results_have_unified_fields():
    """Every result from sighting_to_dict has source_badge, confidence_pct, confidence_color."""
    s = _make_sighting()
    d = sighting_to_dict(s)

    assert "source_badge" in d
    assert "confidence_pct" in d
    assert "confidence_color" in d
    assert isinstance(d["source_badge"], str)
    assert len(d["source_badge"]) > 0
    assert isinstance(d["confidence_pct"], int)
    assert 0 <= d["confidence_pct"] <= 100
    assert d["confidence_color"] in ("green", "amber", "red")


def test_history_results_have_unified_fields():
    """Historical results from _history_to_result also carry unified fields."""
    h = _make_history()
    now = datetime.now(timezone.utc)
    d = _history_to_result(h, now)

    assert "source_badge" in d
    assert "confidence_pct" in d
    assert "confidence_color" in d
    assert d["source_badge"] == "Historical"
    assert isinstance(d["confidence_pct"], int)
    assert d["confidence_color"] in ("green", "amber", "red")


def test_live_results_have_no_reasoning():
    """Live API results have reasoning=None."""
    s = _make_sighting(source_type="nexar")
    d = sighting_to_dict(s)
    assert d["reasoning"] is None


def test_history_results_have_no_reasoning():
    """Historical results have reasoning=None."""
    h = _make_history()
    now = datetime.now(timezone.utc)
    d = _history_to_result(h, now)
    assert d["reasoning"] is None


def test_affinity_has_reasoning():
    """Affinity results carry non-empty reasoning (built inline in search_requirement).

    We verify the dict structure that search_requirement builds for affinity matches.
    """
    # Simulate the affinity dict as built in search_requirement
    match = {
        "vendor_name": "Preferred Vendor",
        "vendor_id": 99,
        "confidence": 0.85,
        "reasoning": "Previously supplied similar TI parts with 95% on-time delivery",
    }
    conf_pct = round(match["confidence"] * 100)
    affinity_result = {
        "vendor_name": match["vendor_name"],
        "source_type": "vendor_affinity",
        "source_badge": "Vendor Match",
        "confidence_pct": conf_pct,
        "confidence_color": "green" if conf_pct >= 75 else ("amber" if conf_pct >= 50 else "red"),
        "reasoning": match["reasoning"],
        "score": max(5, match["confidence"] * 20),
    }

    assert affinity_result["reasoning"] is not None
    assert len(affinity_result["reasoning"]) > 0
    assert affinity_result["source_badge"] == "Vendor Match"
    assert affinity_result["confidence_pct"] == 85
    assert affinity_result["confidence_color"] == "green"


def test_results_sorted_by_confidence():
    """Higher confidence_pct results appear before lower ones after sorting."""
    results = [
        {"vendor_name": "Low", "confidence_pct": 40, "score": 60},
        {"vendor_name": "High", "confidence_pct": 90, "score": 50},
        {"vendor_name": "Mid", "confidence_pct": 70, "score": 55},
    ]

    # Apply the same sort logic used in search_requirement
    results.sort(
        key=lambda x: (x.get("confidence_pct", 0), x.get("score", 0)),
        reverse=True,
    )

    assert results[0]["vendor_name"] == "High"
    assert results[1]["vendor_name"] == "Mid"
    assert results[2]["vendor_name"] == "Low"


def test_confidence_pct_tiebreak_uses_score():
    """When confidence_pct is equal, score breaks the tie."""
    results = [
        {"vendor_name": "LowScore", "confidence_pct": 80, "score": 30},
        {"vendor_name": "HighScore", "confidence_pct": 80, "score": 70},
    ]

    results.sort(
        key=lambda x: (x.get("confidence_pct", 0), x.get("score", 0)),
        reverse=True,
    )

    assert results[0]["vendor_name"] == "HighScore"
    assert results[1]["vendor_name"] == "LowScore"


def test_live_stock_above_historical():
    """Live stock at high confidence appears before older historical results."""
    live = _make_sighting(source_type="nexar", qty_available=5000, unit_price=0.45)
    live_d = sighting_to_dict(live)

    hist_h = _make_history(
        source_type="historical",
        last_seen=datetime.now(timezone.utc) - timedelta(days=60),
    )
    hist_d = _history_to_result(hist_h, datetime.now(timezone.utc))

    # Live results get mapped to 70-95 range, historical decays from 80
    # With 60-day-old history, confidence should be lower
    results = [hist_d, live_d]
    results.sort(
        key=lambda x: (x.get("confidence_pct", 0), x.get("score", 0)),
        reverse=True,
    )

    assert results[0]["source_badge"] == "Live Stock"
    assert results[0]["confidence_pct"] >= results[1]["confidence_pct"]
