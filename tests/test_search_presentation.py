"""Tests for search-results presentation — v2-persisted display reads (spec §9).

Verifies that sighting_to_dict derives confidence_pct/confidence_color from the
PERSISTED v2 score verbatim (screen == DB), that history rows carry the joined
persisted score (or render metadata-only, never a re-derived number), that
affinity rows are confidence-only, and that results sort None-safely by
confidence_pct descending.

Called by: pytest
Depends on: app.search_service (sighting_to_dict, _history_to_result,
            _affinity_match_to_result), app.scoring (confidence_color, source_badge)
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.search_service import _affinity_match_to_result, _history_to_result, sighting_to_dict

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
        "created_at": datetime.now(UTC) - timedelta(hours=2),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_history(**overrides) -> dict:
    """Build a history dict as returned by _get_material_history."""
    now = datetime.now(UTC)
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
        # Joined by _get_material_history from the newest matching sighting's
        # persisted columns (spec §9). None = no surviving scored sighting.
        "persisted_score": 55.0,
        "persisted_score_components": {"trust": 50.0},
    }
    defaults.update(overrides)
    return defaults


def _sort_by_confidence(results: list[dict]) -> None:
    """Sort results in place using the same (None-safe) key as search_requirement."""
    results.sort(
        key=lambda x: (x.get("confidence_pct") or 0, x.get("score") or 0),
        reverse=True,
    )


# ── Tests ────────────────────────────────────────────────────────────────


def test_results_have_unified_fields():
    """sighting_to_dict derives its display fields from the PERSISTED score verbatim
    (spec §9: screen == DB — no display-time re-derivation)."""
    s = _make_sighting(score=50.0)
    d = sighting_to_dict(s)

    assert d["score"] == 50.0
    assert d["confidence_pct"] == 50  # int(round(persisted score)), not a re-mapping
    assert d["confidence_color"] == "amber"
    assert d["source_badge"] == "Live Stock"


def test_sighting_confidence_tracks_persisted_score():
    """confidence_pct moves 1:1 with sightings.score — the 70-95 band remap is gone."""
    assert sighting_to_dict(_make_sighting(score=92.4))["confidence_pct"] == 92
    assert sighting_to_dict(_make_sighting(score=12.0))["confidence_pct"] == 12
    assert sighting_to_dict(_make_sighting(score=12.0))["confidence_color"] == "red"


def test_history_results_read_persisted_score():
    """History rows display the JOINED persisted v2 score of their newest matching
    sighting — never a re-derived age-band number."""
    h = _make_history(persisted_score=61.4)
    now = datetime.now(UTC)
    d = _history_to_result(h, now)

    assert d["source_badge"] == "Historical"
    assert d["score"] == 61.4
    assert d["confidence_pct"] == 61
    assert d["confidence_color"] == "amber"
    assert d["score_components"] == {"trust": 50.0}


def test_history_row_without_persisted_score_is_metadata_only():
    """No surviving scored sighting for the (card, vendor) pair → metadata-only row:

    score 0, no confidence chip — a number is never re-derived (spec §9).
    """
    h = _make_history(persisted_score=None, persisted_score_components=None)
    d = _history_to_result(h, datetime.now(UTC))

    assert d["score"] == 0
    assert d["confidence_pct"] is None
    assert d["confidence_color"] is None
    assert d["score_components"] is None
    assert d["source_badge"] == "Historical"
    # Metadata fields still render
    assert d["material_times_seen"] == 3
    assert d["is_material_history"] is True


def test_live_results_have_no_reasoning():
    """Live API results have reasoning=None."""
    s = _make_sighting(source_type="nexar")
    d = sighting_to_dict(s)
    assert d["reasoning"] is None


def test_history_results_have_no_reasoning():
    """Historical results have reasoning=None."""
    h = _make_history()
    now = datetime.now(UTC)
    d = _history_to_result(h, now)
    assert d["reasoning"] is None


def test_affinity_has_reasoning_and_no_derived_score():
    """Affinity rows carry reasoning + confidence_pct ONLY — the old confidence*20
    pseudo-score died in the §9 scoring cut."""
    match = {
        "vendor_name": "Preferred Vendor",
        "vendor_id": 99,
        "confidence": 0.85,
        "reasoning": "Previously supplied similar TI parts with 95% on-time delivery",
    }
    affinity_result = _affinity_match_to_result(match, "LM358N")

    assert affinity_result["reasoning"] == match["reasoning"]
    assert affinity_result["source_badge"] == "Vendor Match"
    assert affinity_result["confidence_pct"] == 85
    assert affinity_result["confidence_color"] == "green"
    assert affinity_result["score"] == 0
    assert affinity_result["is_affinity"] is True


def test_results_sorted_by_confidence():
    """Higher confidence_pct results appear before lower ones after sorting."""
    results = [
        {"vendor_name": "Low", "confidence_pct": 40, "score": 60},
        {"vendor_name": "High", "confidence_pct": 90, "score": 50},
        {"vendor_name": "Mid", "confidence_pct": 70, "score": 55},
    ]

    # Apply the same sort logic used in search_requirement
    _sort_by_confidence(results)

    assert results[0]["vendor_name"] == "High"
    assert results[1]["vendor_name"] == "Mid"
    assert results[2]["vendor_name"] == "Low"


def test_confidence_pct_tiebreak_uses_score():
    """When confidence_pct is equal, score breaks the tie."""
    results = [
        {"vendor_name": "LowScore", "confidence_pct": 80, "score": 30},
        {"vendor_name": "HighScore", "confidence_pct": 80, "score": 70},
    ]

    _sort_by_confidence(results)

    assert results[0]["vendor_name"] == "HighScore"
    assert results[1]["vendor_name"] == "LowScore"


def test_live_stock_above_historical():
    """A higher persisted score sorts first; a metadata-only history row (no confidence
    chip) sorts last via the None-safe key."""
    live = _make_sighting(source_type="nexar", score=72.0)
    live_d = sighting_to_dict(live)

    hist_h = _make_history(
        source_type="historical",
        last_seen=datetime.now(UTC) - timedelta(days=60),
        persisted_score=40.0,
    )
    hist_d = _history_to_result(hist_h, datetime.now(UTC))

    meta_h = _make_history(persisted_score=None, persisted_score_components=None)
    meta_d = _history_to_result(meta_h, datetime.now(UTC))

    results = [meta_d, hist_d, live_d]
    _sort_by_confidence(results)

    assert results[0]["source_badge"] == "Live Stock"
    assert results[0]["confidence_pct"] == 72
    assert results[1]["confidence_pct"] == 40
    assert results[2]["confidence_pct"] is None
