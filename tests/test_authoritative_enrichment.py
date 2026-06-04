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


from unittest.mock import AsyncMock, patch

from app.services.authoritative_enrichment_service import enrich_card


def _card(db_session, mpn="LM317T"):
    from app.utils.normalization import normalize_mpn_key

    c = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn),
        display_mpn=mpn,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    db_session.flush()
    return c


class _FakeConn:
    def __init__(self, source_name, hits):
        self.source_name = source_name
        self._hits = hits

    async def search(self, pn):
        return self._hits


@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_verified(mock_conns, db_session):
    card = _card(db_session)
    mock_conns.return_value = [
        _FakeConn(
            "digikey",
            [
                {
                    "source_type": "digikey",
                    "mpn_matched": "LM317T",
                    "manufacturer": "TI",
                    "description": "Adjustable regulator",
                    "category": "Voltage Regulator",
                    "lifecycle_status": "active",
                }
            ],
        )
    ]
    import asyncio

    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "verified"
    assert card.manufacturer == "TI"
    assert card.enrichment_provenance["description"]["source"] == "digikey"


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_ai_inferred_when_no_authoritative(mock_conns, mock_claude, db_session):
    card = _card(db_session, "04M3HJ")
    mock_conns.return_value = [_FakeConn("digikey", [])]  # no hits anywhere
    mock_claude.return_value = {"description": "Dell laptop bezel", "category": "Mechanical", "confidence": 0.7}
    import asyncio

    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "ai_inferred"
    assert card.description == "Dell laptop bezel"
    assert card.lifecycle_status is None  # never guessed


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_not_found(mock_conns, mock_claude, db_session):
    card = _card(db_session, "ZZ9PLURAL")
    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
    import asyncio

    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "not_found"
    assert card.description is None


def test_quota_error_disables_source_for_run():
    """A ConnectorQuotaError disables that source for the rest of the run."""
    import asyncio

    from app.connectors.errors import ConnectorQuotaError
    from app.services.authoritative_enrichment_service import fetch_authoritative

    calls = {"n": 0}

    class _QuotaConn:
        source_name = "digikey"

        async def search(self, pn):
            calls["n"] += 1
            raise ConnectorQuotaError("quota exceeded")

    disabled: set[str] = set()
    conn = _QuotaConn()
    asyncio.run(fetch_authoritative("X1", "x1", [conn], disabled))
    assert "digikey" in disabled
    assert calls["n"] == 1
    # Second MPN: source already disabled -> not retried.
    asyncio.run(fetch_authoritative("X2", "x2", [conn], disabled))
    assert calls["n"] == 1
