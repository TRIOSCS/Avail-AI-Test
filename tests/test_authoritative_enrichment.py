"""Tests for the verified-material-enrichment feature.

Covers: enrichment_status / enrichment_provenance model columns (Task 1),
and (future tasks) authoritative-enrichment service logic.

Called by: pytest
Depends on: app/models/intelligence.py, tests/conftest.py (db_session fixture)
"""

from datetime import datetime, timezone

from app.models import MaterialCard


def test_new_card_defaults_to_unenriched(db_session):
    card = MaterialCard(
        normalized_mpn="teststatusdefault",
        display_mpn="TEST-STATUS-DEFAULT",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()
    db_session.refresh(card)
    assert card.enrichment_status == "unenriched"
    assert card.enrichment_provenance is None


from app.services.authoritative_enrichment_service import (
    merge_authoritative,
)


def _hit(source, mpn="LM317T", **over):
    base = {
        "source_type": source,
        "mpn_matched": mpn,
        "manufacturer": "TI",
        "description": f"desc from {source}",
        "category": None,
        "lifecycle_status": None,
        "package_type": None,
        "pin_count": None,
        "rohs_status": None,
        "datasheet_url": None,
    }
    base.update(over)
    return base


def test_exact_match_guard_rejects_mismatch():
    # connector returned a DIFFERENT part — must be ignored
    results = {"digikey": [_hit("digikey", mpn="LM317MT")]}
    merged, prov, contributors = merge_authoritative("lm317t", results)
    assert merged == {}
    assert contributors == []


def test_first_non_null_by_priority():
    results = {
        "mouser": [_hit("mouser", description="mouser desc", category="Linear")],
        "digikey": [_hit("digikey", description="digikey desc", lifecycle_status="active")],
    }
    merged, prov, contributors = merge_authoritative("lm317t", results)
    # digikey has higher priority -> its description wins
    assert merged["description"] == "digikey desc"
    assert prov["description"]["source"] == "digikey"
    # category only present from mouser -> taken from mouser
    assert merged["category"] == "Linear"
    assert prov["category"]["source"] == "mouser"
    assert merged["lifecycle_status"] == "active"
    assert "digikey" in contributors and "mouser" in contributors
