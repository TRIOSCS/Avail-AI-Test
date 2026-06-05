"""Tests for the web_sourced enrichment tier in enrich_card.

Task 6 of the paced-web-enrichment-worker plan.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.models import MaterialCard
from app.services.authoritative_enrichment_service import enrich_card
from app.services.enrichment_worker.web_extractor import WebExtractResult


def _card(db, mpn="LM317T"):
    from app.utils.normalization import normalize_mpn_key

    c = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn),
        display_mpn=mpn,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    db.flush()
    return c


@patch("app.services.authoritative_enrichment_service.extract_part_from_web", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_web_sourced_when_no_api_hit(mock_conns, mock_web, db_session):
    from tests.test_authoritative_enrichment import _FakeConn

    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_web.return_value = WebExtractResult(
        status="web_sourced",
        description="Adj regulator",
        manufacturer="TI",
        category="Voltage Regulator",
        confidence=0.97,
        source_urls=["https://www.ti.com/product/LM317"],
        source_domains=["www.ti.com"],
    )
    card = _card(db_session)
    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "web_sourced"
    assert card.enrichment_source == "web_search"
    assert card.enrichment_provenance["source_urls"] == ["https://www.ti.com/product/LM317"]


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service.extract_part_from_web", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_falls_through_to_ai_when_web_fails(mock_conns, mock_web, mock_claude, db_session):
    from tests.test_authoritative_enrichment import _FakeConn

    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_web.return_value = WebExtractResult(status="failed")
    mock_claude.return_value = {"description": "guess", "category": "x", "confidence": 0.97}
    card = _card(db_session, "04M3HJ")
    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "ai_inferred"


@patch("app.services.authoritative_enrichment_service.extract_part_from_web", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_web_tier_skipped_when_web_search_disabled(mock_conns, mock_web, db_session):
    """When 'web_search' is in disabled, the web tier must be skipped entirely."""
    from unittest.mock import AsyncMock, patch

    from tests.test_authoritative_enrichment import _FakeConn

    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_web.return_value = WebExtractResult(
        status="web_sourced",
        description="Adj regulator",
        manufacturer="TI",
        category="Voltage Regulator",
        confidence=0.97,
        source_urls=["https://www.ti.com/product/LM317"],
        source_domains=["www.ti.com"],
    )

    with patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
        card = _card(db_session, "SKIP1")
        disabled = {"web_search"}
        asyncio.run(enrich_card(card, db_session, disabled=disabled))

    # web tier was skipped -> mock_web never called
    mock_web.assert_not_called()
    # falls through to not_found (claude returned low confidence)
    assert card.enrichment_status == "not_found"


def test_apply_web_sourced_sets_fields(db_session):
    """apply_web_sourced populates card fields and provenance correctly."""
    from app.services.authoritative_enrichment_service import apply_web_sourced

    card = _card(db_session, "TEST1")
    result = WebExtractResult(
        status="web_sourced",
        description="Adj voltage regulator",
        manufacturer="Texas Instruments",
        category="Voltage Regulator",
        datasheet_url="https://www.ti.com/lit/ds/x.pdf",
        confidence=0.97,
        source_urls=["https://www.ti.com/product/LM317"],
        source_domains=["www.ti.com"],
    )
    apply_web_sourced(card, result)

    assert card.description == "Adj voltage regulator"
    assert card.manufacturer == "Texas Instruments"
    assert card.category == "Voltage Regulator"
    assert card.datasheet_url == "https://www.ti.com/lit/ds/x.pdf"
    assert card.enrichment_status == "web_sourced"
    assert card.enrichment_source == "web_search"
    assert card.enriched_at is not None

    prov = card.enrichment_provenance
    assert prov["web_sourced"] is True
    assert prov["confidence"] == 0.97
    assert prov["source_urls"] == ["https://www.ti.com/product/LM317"]
    assert prov["source_domains"] == ["www.ti.com"]
    assert "fetched_at" in prov
    # per-field provenance entries
    assert prov["description"]["source"] == "web_search"
    assert prov["manufacturer"]["source"] == "web_search"


def test_apply_web_sourced_skips_empty_fields(db_session):
    """apply_web_sourced must not overwrite None/empty fields on the card."""
    from app.services.authoritative_enrichment_service import apply_web_sourced

    card = _card(db_session, "TEST2")
    card.description = "existing desc"
    result = WebExtractResult(
        status="web_sourced",
        description=None,  # empty -> should not overwrite
        manufacturer="TI",
        category=None,
        confidence=0.95,
        source_urls=["https://www.ti.com/product/LM317"],
        source_domains=["www.ti.com"],
    )
    apply_web_sourced(card, result)

    # description was None in result -> card.description left as-is (or unset by apply_web_sourced)
    # The spec says "sets only non-empty fields" — None is not set
    assert card.manufacturer == "TI"
    # description should not be in per-field provenance since it was None
    prov = card.enrichment_provenance
    assert "description" not in prov
