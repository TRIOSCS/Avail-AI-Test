"""
tests/test_enrichment_coverage.py — Coverage tests for enrichment.py gaps

Covers: enrich_batch commit boundaries, commodity tag None path,
sighting-confirmed boost, nexar_backfill_untagged, nexar validate edge cases,
cross-validate progress commit.

Called by: pytest
Depends on: app.services.enrichment, conftest fixtures
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard

# ── Helper: create tag infrastructure in SQLite ──────────────────────


def _ensure_tag_tables(db):
    """Import tag models so they exist in the test DB."""
    from app.models.tags import MaterialTag, Tag  # noqa: F401

    return Tag, MaterialTag


def _create_brand_tag(db, name="Texas Instruments"):
    """Create a brand tag and return it."""
    Tag, _ = _ensure_tag_tables(db)
    tag = Tag(name=name, tag_type="brand")
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def _create_material_tag(db, card_id, tag_id, source="ai_classified", confidence=0.7):
    """Create a MaterialTag linking a card to a tag."""
    _, MaterialTag = _ensure_tag_tables(db)
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


def _create_card(db, mpn="lm317t", manufacturer=None):
    """Create a MaterialCard."""
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        manufacturer=manufacturer,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


# ── enrich_batch: commit at 100-item boundary ───────────────────────


@pytest.mark.asyncio
async def test_enrich_batch_commits_at_100_boundary(db_session: Session):
    """enrich_batch should commit every 100 items for progress."""
    from app.services.enrichment import enrich_batch

    # Create 101 cards so we hit the 100-boundary commit
    mpns = []
    for i in range(101):
        mpn = f"part{i:04d}"
        _create_card(db_session, mpn=mpn)
        mpns.append(mpn)

    with patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock, return_value=None):
        result = await enrich_batch(mpns, db_session)

    assert result["total"] == 101
    assert result["skipped"] == 101  # All return None


# ── enrich_batch: card not found after enrich ────────────────────────


@pytest.mark.asyncio
async def test_enrich_batch_skips_missing_card(db_session: Session):
    """enrich_batch should skip if card not in DB after enrichment."""
    from app.services.enrichment import enrich_batch

    fake_result = {"manufacturer": "TI", "category": "IC", "source": "digikey", "confidence": 0.95}

    with patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock, return_value=fake_result):
        result = await enrich_batch(["nonexistent_mpn"], db_session)

    assert result["skipped"] == 1
    assert result["matched"] == 0


# ── _apply_enrichment_to_card: commodity tag is None ─────────────────


def test_apply_enrichment_no_commodity_tag(db_session: Session):
    """When classify returns commodity but get_or_create_commodity_tag returns None, skip it."""
    from app.services.enrichment import _apply_enrichment_to_card

    card = _create_card(db_session, mpn="test_nocommodity", manufacturer=None)

    classify_result = {
        "brand": {"name": "Texas Instruments"},
        "commodity": {"name": "Unknown Commodity"},
    }

    with (
        patch("app.services.enrichment.classify_material_card", return_value=classify_result),
        patch("app.services.enrichment.get_or_create_brand_tag") as mock_brand,
        patch("app.services.enrichment.get_or_create_commodity_tag", return_value=None),
        patch("app.services.enrichment.tag_material_card") as mock_tag,
    ):
        mock_brand_tag = MagicMock()
        mock_brand_tag.id = 99
        mock_brand.return_value = mock_brand_tag

        _apply_enrichment_to_card(
            card,
            {"manufacturer": "Texas Instruments", "source": "digikey", "confidence": 0.95, "category": "IC"},
            db_session,
        )

        # Only brand tag should be applied, not commodity
        mock_tag.assert_called_once()
        tags_applied = mock_tag.call_args[0][1]
        assert len(tags_applied) == 1
        assert tags_applied[0]["tag_id"] == 99


# ── boost_confidence_internal: sighting-confirmed boost ──────────────


def test_boost_confidence_sighting_confirmed(db_session: Session):
    """Sighting-confirmed phase should boost tags where sighting manufacturer matches tag name."""
    from app.models.sourcing import Requirement, Requisition, Sighting

    # Use different manufacturer on card vs tag so Phase 1 (internal) doesn't match,
    # but the sighting manufacturer matches the tag name (Phase 4).
    card = _create_card(db_session, mpn="sighting_test", manufacturer=None)
    tag = _create_brand_tag(db_session, "Texas Instruments")
    mt = _create_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)

    # Sighting requires a requirement_id — create requisition + requirement first
    from app.models import User

    user = User(email="sighting_test@test.com", name="Test", role="buyer")
    db_session.add(user)
    db_session.flush()
    req = Requisition(name="REQ-SIGHTING", customer_name="Test Co", status="open", created_by=user.id)
    db_session.add(req)
    db_session.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn="SIGHTING_TEST", target_qty=100)
    db_session.add(requirement)
    db_session.flush()

    # Create a sighting that confirms the tag
    sighting = Sighting(
        requirement_id=requirement.id,
        material_card_id=card.id,
        vendor_name="Arrow",
        manufacturer="Texas Instruments",
        source_type="brokerbin",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.commit()

    from app.services.enrichment import boost_confidence_internal

    result = boost_confidence_internal(db_session)

    assert result["sighting_boosted"] >= 1

    # Verify the tag confidence was upgraded (may be 0.95 if multi-source also ran)
    from app.models.tags import MaterialTag

    updated_mt = db_session.get(MaterialTag, mt.id)
    assert updated_mt.confidence >= 0.90


def test_boost_confidence_sighting_zero(db_session: Session):
    """When no sighting matches, sighting_boosted should be 0 (no log line)."""
    from app.services.enrichment import boost_confidence_internal

    result = boost_confidence_internal(db_session)
    assert result["sighting_boosted"] == 0


# ── nexar_backfill_untagged ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_nexar_backfill_untagged_no_cards(db_session: Session):
    """When all cards are tagged, returns early with zeros."""
    from app.services.enrichment import nexar_backfill_untagged

    # Ensure tag tables exist
    _ensure_tag_tables(db_session)

    result = await nexar_backfill_untagged(db_session)
    assert result["total_checked"] == 0
    assert result["tagged"] == 0


@pytest.mark.asyncio
async def test_nexar_backfill_untagged_no_creds(db_session: Session):
    """Without Nexar credentials, returns error."""
    from app.services.enrichment import nexar_backfill_untagged

    _ensure_tag_tables(db_session)
    _create_card(db_session, mpn="untagged_part")

    with patch("app.services.enrichment.get_credential_cached", return_value=None):
        result = await nexar_backfill_untagged(db_session)

    assert result["error"] == "no_nexar_creds"
    assert result["total_checked"] == 0


@pytest.mark.asyncio
async def test_nexar_backfill_untagged_success(db_session: Session):
    """Nexar backfill tags untagged cards with manufacturer data."""
    from app.services.enrichment import nexar_backfill_untagged

    _ensure_tag_tables(db_session)
    card = _create_card(db_session, mpn="backfill_part")

    nexar_response = {"data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "Murata"}}}]}}}

    mock_connector = MagicMock()
    mock_connector._run_query = AsyncMock(return_value=nexar_response)
    mock_connector.AGGREGATE_QUERY = "query { ... }"

    with (
        patch("app.services.enrichment.get_credential_cached", return_value="fake_cred"),
        patch("app.services.enrichment.NexarConnector", return_value=mock_connector)
        if False
        else patch("app.connectors.sources.NexarConnector", return_value=mock_connector),
        patch("app.services.enrichment._apply_enrichment_to_card") as mock_apply,
    ):
        # Need to patch the import inside the function
        with patch.dict("sys.modules", {}):
            pass
        # Simpler: patch at the point of use inside nexar_backfill_untagged
        import app.connectors.sources as sources_mod

        original_class = getattr(sources_mod, "NexarConnector", None)
        sources_mod.NexarConnector = lambda *a: mock_connector

        try:
            result = await nexar_backfill_untagged(db_session)
        finally:
            if original_class:
                sources_mod.NexarConnector = original_class

    assert result["tagged"] == 1
    assert result["total_checked"] == 1


@pytest.mark.asyncio
async def test_nexar_backfill_untagged_no_results(db_session: Session):
    """Nexar returns empty results — card stays untagged."""
    from app.services.enrichment import nexar_backfill_untagged

    _ensure_tag_tables(db_session)
    _create_card(db_session, mpn="empty_nexar")

    mock_connector = MagicMock()
    mock_connector._run_query = AsyncMock(return_value={"data": {"supSearchMpn": {"results": []}}})
    mock_connector.AGGREGATE_QUERY = "query { ... }"

    import app.connectors.sources as sources_mod

    original_class = getattr(sources_mod, "NexarConnector", None)
    sources_mod.NexarConnector = lambda *a: mock_connector

    try:
        with patch("app.services.enrichment.get_credential_cached", return_value="fake"):
            result = await nexar_backfill_untagged(db_session)
    finally:
        if original_class:
            sources_mod.NexarConnector = original_class

    assert result["no_result"] == 1
    assert result["tagged"] == 0


@pytest.mark.asyncio
async def test_nexar_backfill_untagged_ignored_manufacturer(db_session: Session):
    """Nexar returns ignored manufacturer (unknown) — counted as no_result."""
    from app.services.enrichment import nexar_backfill_untagged

    _ensure_tag_tables(db_session)
    _create_card(db_session, mpn="ignored_mfr")

    mock_connector = MagicMock()
    mock_connector._run_query = AsyncMock(
        return_value={"data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "Unknown"}}}]}}}
    )
    mock_connector.AGGREGATE_QUERY = "query { ... }"

    import app.connectors.sources as sources_mod

    original_class = getattr(sources_mod, "NexarConnector", None)
    sources_mod.NexarConnector = lambda *a: mock_connector

    try:
        with patch("app.services.enrichment.get_credential_cached", return_value="fake"):
            result = await nexar_backfill_untagged(db_session)
    finally:
        if original_class:
            sources_mod.NexarConnector = original_class

    assert result["no_result"] == 1


@pytest.mark.asyncio
async def test_nexar_backfill_untagged_exception(db_session: Session):
    """Nexar query raises exception — counted as no_result."""
    from app.services.enrichment import nexar_backfill_untagged

    _ensure_tag_tables(db_session)
    _create_card(db_session, mpn="error_part")

    mock_connector = MagicMock()
    mock_connector._run_query = AsyncMock(side_effect=Exception("Nexar API timeout"))
    mock_connector.AGGREGATE_QUERY = "query { ... }"

    import app.connectors.sources as sources_mod

    original_class = getattr(sources_mod, "NexarConnector", None)
    sources_mod.NexarConnector = lambda *a: mock_connector

    try:
        with patch("app.services.enrichment.get_credential_cached", return_value="fake"):
            result = await nexar_backfill_untagged(db_session)
    finally:
        if original_class:
            sources_mod.NexarConnector = original_class

    assert result["no_result"] == 1
    assert result["tagged"] == 0


# ── nexar_bulk_validate edge cases ───────────────────────────────────


@pytest.mark.asyncio
async def test_nexar_validate_ignored_manufacturer(db_session: Session):
    """Nexar returns 'unknown' manufacturer — counted as no_result."""
    from app.services.enrichment import nexar_bulk_validate

    tag = _create_brand_tag(db_session, "Texas Instruments")
    card = _create_card(db_session, mpn="validate_ignored")
    _create_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)

    mock_connector = MagicMock()
    mock_connector._run_query = AsyncMock(
        return_value={"data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "unknown"}}}]}}}
    )
    mock_connector.AGGREGATE_QUERY = "query { ... }"

    import app.connectors.sources as sources_mod

    original_class = getattr(sources_mod, "NexarConnector", None)
    sources_mod.NexarConnector = lambda *a: mock_connector

    try:
        with patch("app.services.enrichment.get_credential_cached", return_value="fake"):
            result = await nexar_bulk_validate(db_session)
    finally:
        if original_class:
            sources_mod.NexarConnector = original_class

    assert result["no_result"] == 1


@pytest.mark.asyncio
async def test_nexar_validate_exception_handling(db_session: Session):
    """Nexar query exception during validate — counted as no_result."""
    from app.services.enrichment import nexar_bulk_validate

    tag = _create_brand_tag(db_session, "Murata")
    card = _create_card(db_session, mpn="validate_error")
    _create_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)

    mock_connector = MagicMock()
    mock_connector._run_query = AsyncMock(side_effect=RuntimeError("API down"))
    mock_connector.AGGREGATE_QUERY = "query { ... }"

    import app.connectors.sources as sources_mod

    original_class = getattr(sources_mod, "NexarConnector", None)
    sources_mod.NexarConnector = lambda *a: mock_connector

    try:
        with patch("app.services.enrichment.get_credential_cached", return_value="fake"):
            result = await nexar_bulk_validate(db_session)
    finally:
        if original_class:
            sources_mod.NexarConnector = original_class

    assert result["no_result"] == 1
    assert result["confirmed"] == 0


@pytest.mark.asyncio
async def test_nexar_validate_mt_deleted(db_session: Session):
    """If MaterialTag is deleted between query and update, skip gracefully."""
    from app.services.enrichment import nexar_bulk_validate

    tag = _create_brand_tag(db_session, "Analog Devices")
    card = _create_card(db_session, mpn="validate_deleted_mt")
    mt = _create_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)

    # Nexar confirms the tag
    mock_connector = MagicMock()
    mock_connector._run_query = AsyncMock(
        return_value={"data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "Analog Devices"}}}]}}}
    )
    mock_connector.AGGREGATE_QUERY = "query { ... }"

    # Delete the MaterialTag before the validate function can update it
    mt_id = mt.id
    db_session.delete(mt)
    db_session.commit()

    import app.connectors.sources as sources_mod

    original_class = getattr(sources_mod, "NexarConnector", None)
    sources_mod.NexarConnector = lambda *a: mock_connector

    try:
        with patch("app.services.enrichment.get_credential_cached", return_value="fake"):
            result = await nexar_bulk_validate(db_session)
    finally:
        if original_class:
            sources_mod.NexarConnector = original_class

    # Should not crash — mt was None so it skipped
    assert result["confirmed"] == 0


# ── cross_validate_batch: progress commit at 100 boundary ────────────


@pytest.mark.asyncio
async def test_cross_validate_progress_commit(db_session: Session):
    """cross_validate_batch commits and logs progress at 100-item boundary."""
    from app.services.enrichment import cross_validate_batch

    _ensure_tag_tables(db_session)
    tag = _create_brand_tag(db_session, "STMicroelectronics")

    # Create 101 cards with low-confidence AI tags
    for i in range(101):
        card = _create_card(db_session, mpn=f"xval{i:04d}")
        _create_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.5)

    # enrich_material_card returns None for all — exercises the no_result + commit path
    with patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock, return_value=None):
        result = await cross_validate_batch(db_session, limit=101)

    assert result["total"] == 101
    assert result["no_result"] == 101
