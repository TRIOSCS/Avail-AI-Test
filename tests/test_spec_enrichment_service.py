"""Tests for the structured-spec enrichment service (second-pass extraction)."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.faceted_search import MaterialSpecFacet
from app.models.intelligence import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas


@pytest.fixture
def db(db_session):
    """Alias for db_session — ensures conftest cleanup handles row deletion."""
    return db_session


def _mc(
    db: Session, mpn: str, *, category: str | None = "microcontrollers", description="An MCU", specs_enriched_at=None
) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower().replace("-", ""),
        display_mpn=mpn,
        manufacturer="STMicroelectronics",
        description=description,
        category=category,
        search_count=5,
        specs_enriched_at=specs_enriched_at,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


@pytest.fixture()
def _schemas(db: Session):
    """Seed commodity_spec_schemas from the canonical JSON so record_spec validates."""
    seed_commodity_schemas(db)


def _payload(mpn: str):
    # has_usb above the 0.85 facet threshold (written); has_uart in the old 0.70–0.84 band,
    # now below threshold (skipped); has_can well below (skipped).
    return {
        "parts": [
            {
                "mpn": mpn,
                "has_usb": True,
                "has_usb_confidence": 0.95,
                "has_uart": True,
                "has_uart_confidence": 0.80,
                "has_can": True,
                "has_can_confidence": 0.40,
            }
        ]
    }


@pytest.mark.asyncio
async def test_writes_high_conf_facet_and_marks_card(db: Session, _schemas):
    from app.services.spec_enrichment_service import enrich_card_specs

    card = _mc(db, "STM32F103")
    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("STM32F103")):
        stats = await enrich_card_specs([card.id], db)

    db.refresh(card)
    assert stats["specs_written"] == 1
    assert card.specs_enriched_at is not None
    facets = db.query(MaterialSpecFacet).filter_by(material_card_id=card.id).all()
    keys = {f.spec_key: f.value_text for f in facets}
    assert keys.get("has_usb") == "true"
    assert "has_uart" not in keys  # 0.80 — below the 0.85 facet threshold
    assert "has_can" not in keys  # 0.40 — well below the 0.85 facet threshold


@pytest.mark.asyncio
async def test_skips_already_enriched_unless_forced(db: Session, _schemas):
    from datetime import datetime, timezone

    from app.services.spec_enrichment_service import enrich_card_specs

    card = _mc(db, "STM32F405", specs_enriched_at=datetime.now(timezone.utc))
    with patch(
        "app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("STM32F405")
    ) as m:
        stats = await enrich_card_specs([card.id], db)
    assert stats["cards_processed"] == 0
    m.assert_not_called()

    with patch(
        "app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("STM32F405")
    ) as m2:
        stats = await enrich_card_specs([card.id], db, force=True)
    assert stats["cards_processed"] == 1
    m2.assert_called_once()


@pytest.mark.asyncio
async def test_skips_card_without_description_or_schema(db: Session, _schemas):
    from app.services.spec_enrichment_service import enrich_card_specs

    no_desc = _mc(db, "NODESC", description=None)
    no_schema = _mc(db, "NOSCHEMA", category="not_a_real_commodity")  # absent from commodity_seeds
    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value={"parts": []}):
        stats = await enrich_card_specs([no_desc.id, no_schema.id], db)
    assert stats["skipped_no_schema"] == 1  # the schema-less card
    # no_desc is filtered out before grouping (description IS NULL) → not processed
    assert stats["cards_processed"] == 0


@pytest.mark.asyncio
async def test_claude_error_counts_and_continues(db: Session, _schemas):
    from app.services.spec_enrichment_service import enrich_card_specs

    card = _mc(db, "STM32F407")
    with patch(
        "app.utils.claude_client.claude_structured", new_callable=AsyncMock, side_effect=RuntimeError("timeout")
    ):
        stats = await enrich_card_specs([card.id], db)
    assert stats["errors"] >= 1
    db.refresh(card)
    assert card.specs_enriched_at is None  # not marked on failure


@pytest.mark.asyncio
async def test_pending_selects_unmarked_cards(db: Session, _schemas):
    from app.services.spec_enrichment_service import enrich_pending_specs

    _mc(db, "PENDING1")
    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("PENDING1")):
        stats = await enrich_pending_specs(db, limit=10)
    assert stats["cards_processed"] == 1


@pytest.mark.asyncio
async def test_enrich_button_triggers_spec_pass(client, test_material_card):
    with (
        patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock) as mcard,
        patch("app.services.spec_enrichment_service.enrich_card_specs", new_callable=AsyncMock) as mspec,
    ):
        resp = client.post(f"/v2/partials/materials/{test_material_card.id}/enrich")
    assert resp.status_code == 200
    mcard.assert_awaited_once()
    mspec.assert_awaited_once()
    # force=True so the just-clicked card re-extracts even if previously marked
    assert mspec.call_args.kwargs.get("force") is True


@pytest.mark.asyncio
async def test_enrich_button_survives_spec_failure(client, test_material_card):
    with (
        patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock),
        patch(
            "app.services.spec_enrichment_service.enrich_card_specs",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        resp = client.post(f"/v2/partials/materials/{test_material_card.id}/enrich")
    assert resp.status_code == 200  # card-level enrichment already succeeded; no 500
