"""Tests for multi-source connector enrichment service.

Tests enrichment waterfall, batch processing, live hook, admin endpoints,
and scheduled job.

Called by: pytest
Depends on: app.services.enrichment, app.services.tagging_ai, app.routers.tagging_admin
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.enrichment import (
    _CONNECTOR_CONFIGS,
    _IGNORED_MANUFACTURERS,
    _apply_enrichment_to_card,
    boost_confidence_internal,
    cross_validate_batch,
    enrich_batch,
    enrich_material_card,
    nexar_bulk_validate,
)
from app.services.tagging_ai import _apply_chunked_batch


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_card(db: Session, mpn: str, manufacturer: str | None = None) -> MaterialCard:
    """Create a MaterialCard for testing."""
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        manufacturer=manufacturer,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _make_brand_tag(db: Session, name: str) -> Tag:
    """Create a brand Tag."""
    tag = Tag(name=name, tag_type="brand")
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def _make_material_tag(db: Session, card_id: int, tag_id: int, source: str, confidence: float) -> MaterialTag:
    """Create a MaterialTag linking card to tag."""
    mt = MaterialTag(
        material_card_id=card_id,
        tag_id=tag_id,
        source=source,
        confidence=confidence,
    )
    db.add(mt)
    db.commit()
    db.refresh(mt)
    return mt


# ── enrich_material_card tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_digikey_manufacturer(db_session):
    """DigiKey returns manufacturer — should be returned as enrichment result."""
    mock_connector = AsyncMock()
    mock_connector.search.return_value = [
        {"manufacturer": "Texas Instruments", "category": "Analog ICs"}
    ]

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.services.enrichment.importlib") as mock_import:
            mock_module = MagicMock()
            mock_module.DigiKeyConnector.return_value = mock_connector
            mock_import.import_module.return_value = mock_module

            result = await enrich_material_card("LM358N", db_session)

    assert result is not None
    assert result["manufacturer"] == "Texas Instruments"
    assert result["source"] == "digikey"
    assert result["confidence"] == 0.95


@pytest.mark.asyncio
async def test_enrich_oemsecrets_manufacturer(db_session):
    """OEMSecrets returns manufacturer — should be returned."""

    async def _mock_search(mpn):
        return [{"manufacturer": "Microchip Technology"}]

    def _mock_cred(source, var):
        if source in ("digikey", "mouser", "element14"):
            return None
        return "test-key"

    mock_connector = AsyncMock()
    mock_connector.search = _mock_search

    with patch("app.services.enrichment.get_credential_cached", side_effect=_mock_cred):
        with patch("app.services.enrichment.importlib") as mock_import:
            mock_module = MagicMock()
            mock_module.OEMSecretsConnector.return_value = mock_connector
            mock_import.import_module.return_value = mock_module

            result = await enrich_material_card("PIC16F877A", db_session)

    assert result is not None
    assert result["manufacturer"] == "Microchip Technology"
    assert result["source"] == "oemsecrets"


@pytest.mark.asyncio
async def test_enrich_no_credentials_skips(db_session):
    """No API keys configured → all connectors skipped, returns None."""
    with patch("app.services.enrichment.get_credential_cached", return_value=None):
        result = await enrich_material_card("LM358N", db_session)

    assert result is None


@pytest.mark.asyncio
async def test_enrich_fallback_to_next(db_session):
    """First connector fails → falls back to next."""
    call_idx = {"n": 0}

    async def _side_effect(mpn):
        call_idx["n"] += 1
        if call_idx["n"] <= 2:
            raise ConnectionError("timeout")
        return [{"manufacturer": "STMicroelectronics"}]

    mock_connector = AsyncMock()
    mock_connector.search = _side_effect

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.services.enrichment.importlib") as mock_import:
            mock_module = MagicMock()
            for cfg in _CONNECTOR_CONFIGS:
                setattr(mock_module, cfg["class"], MagicMock(return_value=mock_connector))
            mock_import.import_module.return_value = mock_module

            result = await enrich_material_card("STM32F103", db_session)

    assert result is not None
    assert result["manufacturer"] == "STMicroelectronics"


@pytest.mark.asyncio
async def test_enrich_ignores_unknown_manufacturer(db_session):
    """Manufacturer = 'Unknown' should not be returned."""
    mock_connector = AsyncMock()
    mock_connector.search.return_value = [{"manufacturer": "Unknown"}]

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.services.enrichment.importlib") as mock_import:
            mock_module = MagicMock()
            for cfg in _CONNECTOR_CONFIGS:
                setattr(mock_module, cfg["class"], MagicMock(return_value=mock_connector))
            mock_import.import_module.return_value = mock_module

            result = await enrich_material_card("XYZ999", db_session)

    assert result is None


@pytest.mark.asyncio
async def test_enrich_ignores_na_manufacturer(db_session):
    """Manufacturer = 'N/A' should not be returned."""
    mock_connector = AsyncMock()
    mock_connector.search.return_value = [{"manufacturer": "N/A"}]

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.services.enrichment.importlib") as mock_import:
            mock_module = MagicMock()
            for cfg in _CONNECTOR_CONFIGS:
                setattr(mock_module, cfg["class"], MagicMock(return_value=mock_connector))
            mock_import.import_module.return_value = mock_module

            result = await enrich_material_card("ABC123", db_session)

    assert result is None


@pytest.mark.asyncio
async def test_enrich_empty_search_results(db_session):
    """Connector returns empty list → returns None."""
    mock_connector = AsyncMock()
    mock_connector.search.return_value = []

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.services.enrichment.importlib") as mock_import:
            mock_module = MagicMock()
            for cfg in _CONNECTOR_CONFIGS:
                setattr(mock_module, cfg["class"], MagicMock(return_value=mock_connector))
            mock_import.import_module.return_value = mock_module

            result = await enrich_material_card("EMPTY001", db_session)

    assert result is None


# ── enrich_batch tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_batch_processes_all(db_session):
    """Batch enrichment processes all MPNs and applies tags."""
    card1 = _make_card(db_session, "lm358n")
    card2 = _make_card(db_session, "pic16f877a")

    async def _mock_enrich(mpn, db):
        return {"manufacturer": "Texas Instruments", "source": "digikey", "confidence": 0.95, "category": None}

    with patch("app.services.enrichment.enrich_material_card", side_effect=_mock_enrich):
        result = await enrich_batch(["lm358n", "pic16f877a"], db_session, concurrency=2)

    assert result["total"] == 2
    assert result["matched"] == 2
    assert result["skipped"] == 0

    db_session.refresh(card1)
    db_session.refresh(card2)
    assert card1.manufacturer == "Texas Instruments"
    assert card2.manufacturer == "Texas Instruments"


@pytest.mark.asyncio
async def test_enrich_batch_skips_no_results(db_session):
    """Batch skips MPNs where no connector returns results."""
    _make_card(db_session, "nodata123")

    async def _mock_enrich(mpn, db):
        return None

    with patch("app.services.enrichment.enrich_material_card", side_effect=_mock_enrich):
        result = await enrich_batch(["nodata123"], db_session, concurrency=1)

    assert result["total"] == 1
    assert result["matched"] == 0
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_enrich_batch_missing_card(db_session):
    """Batch skips MPNs with no matching MaterialCard."""
    async def _mock_enrich(mpn, db):
        return {"manufacturer": "Acme", "source": "digikey", "confidence": 0.95, "category": None}

    with patch("app.services.enrichment.enrich_material_card", side_effect=_mock_enrich):
        result = await enrich_batch(["nonexistent_mpn"], db_session, concurrency=1)

    assert result["total"] == 1
    assert result["matched"] == 0
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_enrich_batch_tracks_sources(db_session):
    """Batch tracks which sources provided results."""
    _make_card(db_session, "mpn_a")
    _make_card(db_session, "mpn_b")

    call_count = {"n": 0}

    async def _mock_enrich(mpn, db):
        call_count["n"] += 1
        source = "digikey" if call_count["n"] == 1 else "mouser"
        return {"manufacturer": "Acme", "source": source, "confidence": 0.95, "category": None}

    with patch("app.services.enrichment.enrich_material_card", side_effect=_mock_enrich):
        result = await enrich_batch(["mpn_a", "mpn_b"], db_session, concurrency=1)

    assert result["sources"]["digikey"] == 1
    assert result["sources"]["mouser"] == 1


# ── _apply_enrichment_to_card tests ──────────────────────────────────────


def test_apply_enrichment_sets_manufacturer(db_session):
    """Enrichment should set card manufacturer and create tags."""
    card = _make_card(db_session, "stm32f103")

    enrichment = {"manufacturer": "STMicroelectronics", "source": "mouser", "confidence": 0.95, "category": None}
    _apply_enrichment_to_card(card, enrichment, db_session)
    db_session.commit()

    db_session.refresh(card)
    assert card.manufacturer == "STMicroelectronics"

    tags = db_session.query(MaterialTag).filter_by(material_card_id=card.id).all()
    assert len(tags) >= 1
    brand_tags = [t for t in tags if t.source == "connector_mouser"]
    assert len(brand_tags) >= 1
    assert brand_tags[0].confidence == 0.95


def test_apply_enrichment_preserves_existing_manufacturer(db_session):
    """If card already has a manufacturer, don't overwrite it."""
    card = _make_card(db_session, "lm358n", manufacturer="Existing Mfr")

    enrichment = {"manufacturer": "Texas Instruments", "source": "digikey", "confidence": 0.95, "category": None}
    _apply_enrichment_to_card(card, enrichment, db_session)
    db_session.commit()

    db_session.refresh(card)
    assert card.manufacturer == "Existing Mfr"


def test_apply_enrichment_sets_category(db_session):
    """Enrichment should set card category if not already set."""
    card = _make_card(db_session, "cap001")

    enrichment = {"manufacturer": "Murata", "source": "element14", "confidence": 0.95, "category": "Capacitors"}
    _apply_enrichment_to_card(card, enrichment, db_session)
    db_session.commit()

    db_session.refresh(card)
    assert card.category == "Capacitors"


# ── _apply_chunked_batch tests ───────────────────────────────────────────


def test_apply_chunked_batch_basic(db_session):
    """Chunked batch applier processes classifications correctly."""
    card = _make_card(db_session, "lm358n")

    classifications = [
        {"mpn": "LM358N", "manufacturer": "Texas Instruments", "category": "Op-Amps"},
    ]

    matched, unknown = _apply_chunked_batch(classifications, db_session)
    assert matched == 1
    assert unknown == 0

    db_session.refresh(card)
    assert card.manufacturer == "Texas Instruments"


def test_apply_chunked_batch_unknown(db_session):
    """Unknown manufacturer counts as unknown."""
    _make_card(db_session, "xyz999")

    classifications = [
        {"mpn": "XYZ999", "manufacturer": "Unknown", "category": "Miscellaneous"},
    ]

    matched, unknown = _apply_chunked_batch(classifications, db_session)
    assert matched == 0
    assert unknown == 1


def test_apply_chunked_batch_empty(db_session):
    """Empty classifications list returns zeros."""
    matched, unknown = _apply_chunked_batch([], db_session)
    assert matched == 0
    assert unknown == 0


def test_apply_chunked_batch_no_matching_card(db_session):
    """Classifications for non-existent cards are safely skipped."""
    classifications = [
        {"mpn": "DOESNOTEXIST", "manufacturer": "Acme", "category": "Widgets"},
    ]

    matched, unknown = _apply_chunked_batch(classifications, db_session)
    assert matched == 0
    assert unknown == 0


def test_apply_chunked_batch_multiple_cards(db_session):
    """Process multiple cards in a single batch."""
    _make_card(db_session, "mpn_a")
    _make_card(db_session, "mpn_b")
    _make_card(db_session, "mpn_c")

    classifications = [
        {"mpn": "mpn_a", "manufacturer": "Texas Instruments", "category": "MCU"},
        {"mpn": "mpn_b", "manufacturer": "Unknown", "category": "Misc"},
        {"mpn": "mpn_c", "manufacturer": "Analog Devices", "category": "ADC"},
    ]

    matched, unknown = _apply_chunked_batch(classifications, db_session)
    assert matched == 2
    assert unknown == 1


# ── Admin endpoint tests ────────────────────────────────────────────────


def test_admin_enrich_endpoint(client, db_session):
    """POST /api/admin/tagging/enrich returns 200."""
    card = _make_card(db_session, "lowconf001")
    tag = _make_brand_tag(db_session, "Unknown")
    _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.3)

    resp = client.post("/api/admin/tagging/enrich")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_admin_enrich_no_cards_needed(client, db_session):
    """POST /api/admin/tagging/enrich with no low-conf cards."""
    resp = client.post("/api/admin/tagging/enrich")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["count"] == 0


def test_admin_enrich_status_endpoint(client):
    """GET /api/admin/tagging/enrich/status returns current status."""
    resp = client.get("/api/admin/tagging/enrich/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "running" in data
    assert "result" in data


def test_admin_apply_batch_endpoint(client):
    """POST /api/admin/tagging/apply-batch returns 200."""
    resp = client.post("/api/admin/tagging/apply-batch?batch_id=test_batch_123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "test_batch_123" in data["message"]


def test_admin_tagging_status_endpoint(client, db_session):
    """GET /api/admin/tagging/status returns coverage stats."""
    _make_card(db_session, "card001")

    resp = client.get("/api/admin/tagging/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_material_cards" in data
    assert data["total_material_cards"] >= 1
    assert "coverage_percentage" in data


# ── Live enrichment hook tests ───────────────────────────────────────────


def test_schedule_background_enrichment_no_cards(db_session):
    """No cards → no enrichment scheduled."""
    from app.search_service import _schedule_background_enrichment

    _schedule_background_enrichment(set(), db_session)


def test_schedule_background_enrichment_with_manufacturer(db_session):
    """Cards with manufacturer → no enrichment needed."""
    from app.search_service import _schedule_background_enrichment

    card = _make_card(db_session, "has_mfr", manufacturer="Texas Instruments")
    _schedule_background_enrichment({card.id}, db_session)


def test_schedule_background_enrichment_fires_for_missing_mfr(db_session):
    """Cards without manufacturer → enrichment task created."""
    from app.search_service import _schedule_background_enrichment

    card = _make_card(db_session, "no_mfr")

    with patch("app.search_service.asyncio.create_task") as mock_task:
        _schedule_background_enrichment({card.id}, db_session)
        assert mock_task.called


# ── Validation tests ─────────────────────────────────────────────────────


def test_ignored_manufacturers_set():
    """Verify the ignored manufacturers set contains expected values."""
    assert "" in _IGNORED_MANUFACTURERS
    assert "unknown" in _IGNORED_MANUFACTURERS
    assert "n/a" in _IGNORED_MANUFACTURERS
    assert "various" in _IGNORED_MANUFACTURERS


def test_connector_configs_have_required_fields():
    """All connector configs have required fields."""
    for cfg in _CONNECTOR_CONFIGS:
        assert "name" in cfg
        assert "module" in cfg
        assert "class" in cfg
        assert "creds" in cfg
        assert "confidence" in cfg
        assert isinstance(cfg["creds"], list)
        assert cfg["confidence"] >= 0.8


def test_connector_configs_priority_order():
    """Connectors are in priority order: authoritative first."""
    names = [cfg["name"] for cfg in _CONNECTOR_CONFIGS]
    assert names.index("digikey") < names.index("brokerbin")
    assert names.index("mouser") < names.index("brokerbin")


# ── Cross-validation tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_validate_confirms_matching_tag(db_session):
    """AI tag with matching connector result → confidence upgraded."""
    card = _make_card(db_session, "lm358n", manufacturer="Texas Instruments")
    tag = _make_brand_tag(db_session, "Texas Instruments")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    async def _mock_enrich(mpn, db):
        return {"manufacturer": "Texas Instruments", "source": "digikey", "confidence": 0.95, "category": None}

    with patch("app.services.enrichment.enrich_material_card", side_effect=_mock_enrich):
        result = await cross_validate_batch(db_session, limit=10, concurrency=1)

    assert result["total"] == 1
    assert result["confirmed"] == 1

    db_session.refresh(mt)
    assert mt.confidence == 0.95
    assert "ai_confirmed" in mt.source


@pytest.mark.asyncio
async def test_cross_validate_changes_wrong_manufacturer(db_session):
    """AI tag with different connector result → new tag applied."""
    card = _make_card(db_session, "stm32f103", manufacturer="Wrong Corp")
    tag = _make_brand_tag(db_session, "Wrong Corp")
    _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    async def _mock_enrich(mpn, db):
        return {"manufacturer": "STMicroelectronics", "source": "mouser", "confidence": 0.95, "category": None}

    with patch("app.services.enrichment.enrich_material_card", side_effect=_mock_enrich):
        result = await cross_validate_batch(db_session, limit=10, concurrency=1)

    assert result["total"] == 1
    assert result["changed_manufacturer"] == 1


@pytest.mark.asyncio
async def test_cross_validate_no_result(db_session):
    """Connector returns nothing → tag left as-is."""
    card = _make_card(db_session, "nodata999")
    tag = _make_brand_tag(db_session, "Some Brand")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    async def _mock_enrich(mpn, db):
        return None

    with patch("app.services.enrichment.enrich_material_card", side_effect=_mock_enrich):
        result = await cross_validate_batch(db_session, limit=10, concurrency=1)

    assert result["total"] == 1
    assert result["no_result"] == 1

    db_session.refresh(mt)
    assert mt.confidence == 0.7  # Unchanged


@pytest.mark.asyncio
async def test_cross_validate_skips_unknown_tags(db_session):
    """Tags with confidence 0.3 (Unknown) are skipped."""
    card = _make_card(db_session, "unknown001")
    tag = _make_brand_tag(db_session, "Unknown")
    _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.3)

    result = await cross_validate_batch(db_session, limit=10, concurrency=1)
    assert result["total"] == 0  # Skipped because confidence <= 0.3


@pytest.mark.asyncio
async def test_cross_validate_empty(db_session):
    """No low-confidence tags → returns zeros."""
    result = await cross_validate_batch(db_session, limit=10, concurrency=1)
    assert result["total"] == 0
    assert result["confirmed"] == 0


def test_admin_cross_validate_endpoint(client, db_session):
    """POST /api/admin/tagging/cross-validate returns 200."""
    resp = client.post("/api/admin/tagging/cross-validate?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


# ── boost_confidence_internal tests ─────────────────────────────────────


def test_boost_confidence_upgrades_confirmed_tags(db_session):
    """Tags where card.manufacturer matches AI brand tag → boosted to 0.90."""
    card = _make_card(db_session, "lm358n", manufacturer="Texas Instruments")
    tag = _make_brand_tag(db_session, "Texas Instruments")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    result = boost_confidence_internal(db_session, batch_size=100)

    assert result["total_boosted"] == 1
    db_session.refresh(mt)
    assert mt.confidence == 0.90
    assert mt.source == "ai_confirmed_internal"


def test_boost_confidence_skips_unknown_tags(db_session):
    """Tags at 0.3 confidence (Unknown) are not boosted."""
    card = _make_card(db_session, "xyz999", manufacturer="Unknown")
    tag = _make_brand_tag(db_session, "Unknown")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.3)

    result = boost_confidence_internal(db_session, batch_size=100)

    assert result["total_boosted"] == 0
    db_session.refresh(mt)
    assert mt.confidence == 0.3


def test_boost_confidence_skips_no_manufacturer(db_session):
    """Cards with no manufacturer field → not boosted."""
    card = _make_card(db_session, "nomfr001")
    tag = _make_brand_tag(db_session, "Acme Corp")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    result = boost_confidence_internal(db_session, batch_size=100)

    assert result["total_boosted"] == 0
    db_session.refresh(mt)
    assert mt.confidence == 0.7


def test_boost_confidence_skips_mismatched_manufacturer(db_session):
    """Card manufacturer differs from AI tag → not boosted."""
    card = _make_card(db_session, "stm32f103", manufacturer="STMicroelectronics")
    tag = _make_brand_tag(db_session, "Texas Instruments")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    result = boost_confidence_internal(db_session, batch_size=100)

    assert result["total_boosted"] == 0
    db_session.refresh(mt)
    assert mt.confidence == 0.7


def test_boost_confidence_case_insensitive(db_session):
    """Match is case-insensitive: 'texas instruments' == 'Texas Instruments'."""
    card = _make_card(db_session, "lm741", manufacturer="texas instruments")
    tag = _make_brand_tag(db_session, "Texas Instruments")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    result = boost_confidence_internal(db_session, batch_size=100)

    assert result["total_boosted"] == 1
    db_session.refresh(mt)
    assert mt.confidence == 0.90


def test_boost_confidence_skips_already_high(db_session):
    """Tags already at 0.95 are not touched."""
    card = _make_card(db_session, "ad9361", manufacturer="Analog Devices")
    tag = _make_brand_tag(db_session, "Analog Devices")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.95)

    result = boost_confidence_internal(db_session, batch_size=100)

    assert result["total_boosted"] == 0


def test_boost_confidence_empty_db(db_session):
    """No AI tags at all → returns 0."""
    result = boost_confidence_internal(db_session, batch_size=100)
    assert result["total_boosted"] == 0


def test_boost_confidence_multiple_batches(db_session):
    """Processes multiple cards across batches."""
    tag = _make_brand_tag(db_session, "Murata")
    cards = []
    mts = []
    for i in range(5):
        card = _make_card(db_session, f"grm{i:03d}", manufacturer="Murata")
        mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)
        cards.append(card)
        mts.append(mt)

    result = boost_confidence_internal(db_session, batch_size=2)

    assert result["total_boosted"] == 5
    for mt in mts:
        db_session.refresh(mt)
        assert mt.confidence == 0.90


# ── nexar_bulk_validate tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_nexar_validate_no_low_conf_tags(db_session):
    """No 0.7-confidence AI tags → returns zeros."""
    result = await nexar_bulk_validate(db_session, limit=10)
    assert result["total_checked"] == 0
    assert result["confirmed"] == 0


@pytest.mark.asyncio
async def test_nexar_validate_no_credentials(db_session):
    """No Nexar credentials → skips with error."""
    card = _make_card(db_session, "test001")
    tag = _make_brand_tag(db_session, "Acme")
    _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    with patch("app.services.enrichment.get_credential_cached", return_value=None):
        result = await nexar_bulk_validate(db_session, limit=10)

    assert result["total_checked"] == 0
    assert result.get("error") == "no_nexar_creds"


@pytest.mark.asyncio
async def test_nexar_validate_confirms_matching(db_session):
    """Nexar confirms AI tag → confidence upgraded to 0.95."""
    card = _make_card(db_session, "lm358n")
    tag = _make_brand_tag(db_session, "Texas Instruments")
    mt = _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    mock_connector = AsyncMock()
    mock_connector.AGGREGATE_QUERY = "query { ... }"
    mock_connector._run_query = AsyncMock(return_value={
        "data": {"supSearchMpn": {"results": [
            {"part": {"manufacturer": {"name": "Texas Instruments"}}}
        ]}}
    })

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.connectors.sources.NexarConnector", return_value=mock_connector):
            result = await nexar_bulk_validate(db_session, limit=10)

    assert result["confirmed"] == 1
    assert result["changed"] == 0
    db_session.refresh(mt)
    assert mt.confidence == 0.95
    assert mt.source == "ai_confirmed_nexar"


@pytest.mark.asyncio
async def test_nexar_validate_changes_manufacturer(db_session):
    """Nexar disagrees → applies Nexar's manufacturer."""
    card = _make_card(db_session, "wrong001")
    tag = _make_brand_tag(db_session, "Wrong Brand")
    _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    mock_connector = AsyncMock()
    mock_connector.AGGREGATE_QUERY = "query { ... }"
    mock_connector._run_query = AsyncMock(return_value={
        "data": {"supSearchMpn": {"results": [
            {"part": {"manufacturer": {"name": "Correct Brand"}}}
        ]}}
    })

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.connectors.sources.NexarConnector", return_value=mock_connector):
            result = await nexar_bulk_validate(db_session, limit=10)

    assert result["changed"] == 1
    assert result["confirmed"] == 0


@pytest.mark.asyncio
async def test_nexar_validate_no_result(db_session):
    """Nexar returns no results → counted as no_result."""
    card = _make_card(db_session, "nodata001")
    tag = _make_brand_tag(db_session, "Some Brand")
    _make_material_tag(db_session, card.id, tag.id, "ai_classified", 0.7)

    mock_connector = AsyncMock()
    mock_connector.AGGREGATE_QUERY = "query { ... }"
    mock_connector._run_query = AsyncMock(return_value={
        "data": {"supSearchMpn": {"results": []}}
    })

    with patch("app.services.enrichment.get_credential_cached", return_value="test-key"):
        with patch("app.connectors.sources.NexarConnector", return_value=mock_connector):
            result = await nexar_bulk_validate(db_session, limit=10)

    assert result["no_result"] == 1


# ── Admin endpoint tests for boost/nexar ────────────────────────────────


def test_admin_boost_confidence_endpoint(client, db_session):
    """POST /api/admin/tagging/boost-confidence returns 200."""
    resp = client.post("/api/admin/tagging/boost-confidence")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "confidence" in data["message"].lower()


def test_admin_nexar_validate_endpoint(client, db_session):
    """POST /api/admin/tagging/nexar-validate returns 200."""
    resp = client.post("/api/admin/tagging/nexar-validate?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "nexar" in data["message"].lower()
