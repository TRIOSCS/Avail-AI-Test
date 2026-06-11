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
    """apply_web_sourced populates card fields and provenance correctly.

    category/manufacturer route through the F1 ladder at web_search (tier 70) — the
    provenance columns are stamped, never raw-set.
    """
    from app.services.authoritative_enrichment_service import apply_web_sourced

    card = _card(db_session, "TEST1")
    result = WebExtractResult(
        status="web_sourced",
        description="3.84TB U.2 NVMe enterprise SSD",
        manufacturer="Samsung",
        category="ssd",
        datasheet_url="https://semiconductor.samsung.com/x.pdf",
        confidence=0.97,
        source_urls=["https://semiconductor.samsung.com/ssd/pm9a3"],
        source_domains=["semiconductor.samsung.com"],
    )
    apply_web_sourced(card, result)

    assert card.description == "3.84TB U.2 NVMe enterprise SSD"
    assert card.manufacturer == "Samsung"
    assert card.category == "ssd"
    assert card.datasheet_url == "https://semiconductor.samsung.com/x.pdf"
    assert card.enrichment_status == "web_sourced"
    assert card.enrichment_source == "web_search"
    assert card.enriched_at is not None
    # F1-ladder provenance stamped on both provenanced columns (web_search = tier 70).
    assert card.category_source == "web_search"
    assert card.category_tier == 70
    assert card.category_confidence == 0.97
    assert card.manufacturer_source == "web_search"
    assert card.manufacturer_tier == 70

    prov = card.enrichment_provenance
    assert prov["web_sourced"] is True
    assert prov["confidence"] == 0.97
    assert prov["source_urls"] == ["https://semiconductor.samsung.com/ssd/pm9a3"]
    assert prov["source_domains"] == ["semiconductor.samsung.com"]
    assert "fetched_at" in prov
    # per-field provenance entries
    assert prov["description"]["source"] == "web_search"
    assert prov["manufacturer"]["source"] == "web_search"
    assert prov["category"]["source"] == "web_search"


def test_apply_web_sourced_category_loses_to_decode_85(db_session):
    """web_search (70) can never overwrite a decode-85 category — the ladder keeps the
    decode value and the rejected write gets no per-field provenance entry."""
    from datetime import datetime, timezone

    from app.services.authoritative_enrichment_service import apply_web_sourced

    card = _card(db_session, "TEST3")
    card.category = "hdd"
    card.category_source = "mpn_decode"
    card.category_confidence = 0.95
    card.category_tier = 85
    card.category_updated_at = datetime.now(timezone.utc)
    db_session.flush()

    result = WebExtractResult(
        status="web_sourced",
        description="some web prose",
        manufacturer=None,
        category="ssd",
        confidence=0.97,
        source_urls=["https://example.com/p"],
        source_domains=["example.com"],
    )
    apply_web_sourced(card, result)

    assert card.category == "hdd"  # decode kept
    assert card.category_source == "mpn_decode"
    assert "category" not in card.enrichment_provenance


def test_apply_web_sourced_off_vocab_category_rejected(db_session):
    """An off-vocab web category is rejected by the ladder's normalizer — never
    persisted as junk, and absent from the per-field provenance."""
    from app.services.authoritative_enrichment_service import apply_web_sourced

    card = _card(db_session, "TEST4")
    result = WebExtractResult(
        status="web_sourced",
        description="Adj voltage regulator",
        manufacturer="Texas Instruments",
        category="Voltage Regulator",
        confidence=0.97,
        source_urls=["https://www.ti.com/product/LM317"],
        source_domains=["www.ti.com"],
    )
    apply_web_sourced(card, result)

    assert card.category is None
    assert "category" not in card.enrichment_provenance
    assert card.manufacturer == "Texas Instruments"  # maker still lands (empty card)
    assert card.enrichment_status == "web_sourced"


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
