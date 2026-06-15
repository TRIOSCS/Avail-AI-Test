"""Tests for the structured-spec enrichment service (second-pass extraction)."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import MaterialEnrichmentStatus
from app.models.faceted_search import MaterialSpecFacet
from app.models.intelligence import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas


@pytest.fixture
def db(db_session):
    """Alias for db_session — ensures conftest cleanup handles row deletion."""
    return db_session


def _mc(
    db: Session,
    mpn: str,
    *,
    category: str | None = "microcontrollers",
    description="An MCU",
    specs_enriched_at=None,
    # SP1 (2026-06-09): only trustworthy/source-attributed cards seed specs. Default the
    # helper to 'verified' so existing happy-path tests still exercise the spec reader.
    enrichment_status=MaterialEnrichmentStatus.VERIFIED,
) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower().replace("-", ""),
        display_mpn=mpn,
        manufacturer="STMicroelectronics",
        description=description,
        category=category,
        search_count=5,
        specs_enriched_at=specs_enriched_at,
        enrichment_status=enrichment_status,
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
async def test_ladder_arbitration_and_provenance_through_writer(db: Session, _schemas):
    """End-to-end F1 ladder pin for THIS writer (the other four record_spec writers have
    one): a higher-tier mpn_decode prior survives a conflicting AI extraction that self-
    reports EQUAL confidence, while a fresh key persists with the writer's registered
    provenance (source="spec_extraction", tier=60).

    Pins (a) the writer's source literal staying registered in spec_tiers.SOURCE_TIER —
    an unregistered literal would rank EVERY AI spec write at tier 0 (silently
    clobberable by ai_guess 40) with no other test failing — and (b) the absence of any
    per-writer pre-gate (arbitration belongs to record_spec alone, so run order is not
    load-bearing).
    """
    from app.services.spec_enrichment_service import enrich_card_specs
    from app.services.spec_tiers import SOURCE_TIER
    from app.services.spec_write_service import record_spec

    card = _mc(db, "STM32G474")
    # Higher-tier deterministic prior: the decode says the part has NO USB.
    assert record_spec(db, card.id, "has_usb", False, source="mpn_decode", confidence=0.95) is True
    db.commit()

    payload = {
        "parts": [
            {
                "mpn": "STM32G474",
                "has_usb": True,  # conflicts with the tier-85 decode prior
                "has_usb_confidence": 0.95,  # equal confidence — TIER must decide, not confidence
                "has_uart": True,  # fresh key — must land at spec_extraction/60
                "has_uart_confidence": 0.95,
            }
        ]
    }
    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=payload):
        stats = await enrich_card_specs([card.id], db)

    db.refresh(card)
    # The decode prior survives untouched — facet AND JSONB provenance.
    usb = db.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="has_usb").one()
    assert usb.value_text == "false"
    assert usb.source == "mpn_decode"
    assert usb.tier == SOURCE_TIER["mpn_decode"]
    assert card.specs_structured["has_usb"]["source"] == "mpn_decode"
    assert card.specs_structured["has_usb"]["tier"] == SOURCE_TIER["mpn_decode"]
    # The fresh key lands with the writer's registered provenance (literal pinned to 60).
    uart = db.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="has_uart").one()
    assert uart.value_text == "true"
    assert uart.source == "spec_extraction"
    assert uart.tier == SOURCE_TIER["spec_extraction"] == 60
    assert uart.confidence == 0.95
    assert card.specs_structured["has_uart"]["source"] == "spec_extraction"
    assert card.specs_structured["has_uart"]["tier"] == 60
    assert stats["specs_written"] == 1  # only the fresh key — the conflict was rejected


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
    no_schema = _mc(db, "NOSCHEMA", category="ics_other")  # canonical coarse bucket — no spec schema
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
@pytest.mark.parametrize("coarse", ["ics_other", "oem_assemblies"])
async def test_coarse_bucket_cards_skip_spec_pass_unstamped(db: Session, _schemas, coarse):
    """The canonical coarse buckets (not just arbitrary unknown strings) hit the
    skipped_no_schema path when addressed directly (e.g. the enrich button), and stay
    unstamped so a future schema addition picks them up without force=True."""
    from app.services.spec_enrichment_service import enrich_card_specs

    card = _mc(db, f"COARSE-{coarse}", category=coarse)
    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value={"parts": []}) as m:
        stats = await enrich_card_specs([card.id], db)
    assert stats["skipped_no_schema"] == 1
    assert stats["cards_processed"] == 0
    m.assert_not_called()
    db.refresh(card)
    assert card.specs_enriched_at is None


@pytest.mark.asyncio
async def test_pending_selects_unmarked_cards(db: Session, _schemas):
    from app.services.spec_enrichment_service import enrich_pending_specs

    _mc(db, "PENDING1")
    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("PENDING1")):
        stats = await enrich_pending_specs(db, limit=10)
    assert stats["cards_processed"] == 1


@pytest.mark.asyncio
async def test_pending_excludes_coarse_buckets_from_window(db: Session, _schemas):
    """Coarse-bucket cards never enter the scheduled selection window: they have no
    schema BY DESIGN, so selecting them would recycle the same unstamped cards through
    every run and (once the population outgrows the limit) starve seeded commodities."""
    from app.services.spec_enrichment_service import enrich_pending_specs

    coarse = _mc(db, "COARSE-PENDING", category="ics_other")
    seeded = _mc(db, "SEEDED-PENDING")  # microcontrollers — seeded, must still be picked
    with patch(
        "app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("SEEDED-PENDING")
    ):
        stats = await enrich_pending_specs(db, limit=10)
    assert stats["cards_processed"] == 1  # only the seeded-commodity card
    assert stats["skipped_no_schema"] == 0  # the coarse card was excluded, not skipped
    db.refresh(coarse)
    db.refresh(seeded)
    assert coarse.specs_enriched_at is None  # stays eligible if a schema ever ships
    assert seeded.specs_enriched_at is not None


@pytest.mark.asyncio
async def test_enrich_button_triggers_spec_pass(client, test_material_card):
    # SP1 (2026-06-09): the button routes to the authoritative ladder (enrich_cards), then
    # the status-gated spec pass — never the removed Haiku path (enrich_material_cards).
    with (
        patch(
            "app.services.authoritative_enrichment_service.enrich_cards",
            new_callable=AsyncMock,
            return_value={"verified": 1},
        ) as mauth,
        patch("app.services.spec_enrichment_service.enrich_card_specs", new_callable=AsyncMock) as mspec,
        patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock) as mhaiku,
    ):
        resp = client.post(f"/v2/partials/materials/{test_material_card.id}/enrich")
    assert resp.status_code == 200
    mauth.assert_awaited_once()
    # refresh=True so even a terminal card re-enters the ladder
    assert mauth.call_args.kwargs.get("refresh") is True
    mhaiku.assert_not_called()
    mspec.assert_awaited_once()
    # force=True so the just-clicked card re-extracts even if previously marked
    assert mspec.call_args.kwargs.get("force") is True


def test_build_spec_prompt_includes_graded_ladder_note():
    """Seed-level extraction notes reach the AI prompt: hdd/encryption is a graded
    highest-tier-wins ladder (a FIPS 140-2 drive is also an SED with ISE), and the
    facet holds ONE value per card, so the writer must pick deterministically."""
    from app.services.spec_enrichment_service import build_spec_prompt

    prompt = build_spec_prompt(
        "hdd", [{"display_mpn": "ST4000NM000A", "manufacturer": "Seagate", "description": "4TB 7.2K SAS HDD"}]
    )
    assert "highest tier wins" in prompt


@pytest.mark.asyncio
async def test_enrich_button_survives_spec_failure(client, test_material_card):
    with (
        patch(
            "app.services.authoritative_enrichment_service.enrich_cards",
            new_callable=AsyncMock,
            return_value={"verified": 1},
        ),
        patch(
            "app.services.spec_enrichment_service.enrich_card_specs",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        resp = client.post(f"/v2/partials/materials/{test_material_card.id}/enrich")
    assert resp.status_code == 200  # card-level enrichment already succeeded; no 500
