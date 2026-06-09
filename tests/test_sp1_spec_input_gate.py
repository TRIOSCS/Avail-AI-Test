"""SP1 trustworthy-status input gate for the structured-spec reader.

What: Asserts enrich_card_specs / enrich_pending_specs only ever seed facets from cards
      whose enrichment_status is trustworthy/source-attributed (verified/web_sourced/
      oem_sourced). Guess/orphan descriptions (e.g. not_found cards with a hallucinated
      "likely a ..." description) must NEVER produce facets, and force=True does NOT bypass
      the gate (it only bypasses the specs_enriched_at re-process filter).
Called by: pytest.
Depends on: app.services.spec_enrichment_service, app.constants.MaterialEnrichmentStatus,
            app.models (MaterialCard, MaterialSpecFacet), commodity schema seed fixture.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import MaterialEnrichmentStatus
from app.models.faceted_search import MaterialSpecFacet
from app.models.intelligence import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas

_TRUSTWORTHY = {
    MaterialEnrichmentStatus.VERIFIED,
    MaterialEnrichmentStatus.WEB_SOURCED,
    MaterialEnrichmentStatus.OEM_SOURCED,
}


@pytest.fixture
def db(db_session):
    return db_session


@pytest.fixture()
def _schemas(db: Session):
    """Seed commodity_spec_schemas so record_spec validates."""
    seed_commodity_schemas(db)


def _card(
    db: Session,
    mpn: str,
    status: str,
    *,
    category: str = "microcontrollers",
    description: str = "An MCU",
    enrichment_source: str | None = "verified-distributor",
) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower().replace("-", ""),
        display_mpn=mpn,
        manufacturer="STMicroelectronics",
        description=description,
        category=category,
        search_count=5,
        enrichment_status=status,
        enrichment_source=enrichment_source,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _payload(mpn: str) -> dict:
    return {"parts": [{"mpn": mpn, "has_usb": True, "has_usb_confidence": 0.95}]}


# ── The status gate: selection IFF trustworthy ─────────────────────────


@pytest.mark.parametrize("status", list(MaterialEnrichmentStatus))
@pytest.mark.asyncio
async def test_enrich_card_specs_selects_only_trustworthy(db: Session, _schemas, status):
    """A complete card is processed by enrich_card_specs IFF its status is
    trustworthy."""
    from app.services.spec_enrichment_service import enrich_card_specs

    card = _card(db, f"GATE{status.replace('_', '')}", status)
    with patch(
        "app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload(card.display_mpn)
    ) as mclaude:
        stats = await enrich_card_specs([card.id], db)

    if status in _TRUSTWORTHY:
        assert stats["cards_processed"] == 1
        mclaude.assert_awaited()
    else:
        assert stats["cards_processed"] == 0
        mclaude.assert_not_awaited()


@pytest.mark.parametrize("status", list(MaterialEnrichmentStatus))
@pytest.mark.asyncio
async def test_enrich_pending_specs_selects_only_trustworthy(db: Session, _schemas, status):
    """A complete card is swept by enrich_pending_specs IFF its status is
    trustworthy."""
    from app.services.spec_enrichment_service import enrich_pending_specs

    card = _card(db, f"PEND{status.replace('_', '')}", status)
    with patch(
        "app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload(card.display_mpn)
    ) as mclaude:
        stats = await enrich_pending_specs(db, limit=10)

    if status in _TRUSTWORTHY:
        assert stats["cards_processed"] == 1
        mclaude.assert_awaited()
    else:
        assert stats["cards_processed"] == 0
        mclaude.assert_not_awaited()


# ── Regression: the guess -> spec leak ─────────────────────────────────


@pytest.mark.asyncio
async def test_not_found_guess_description_never_seeds_specs(db: Session, _schemas):
    """The original leak: a not_found card with a hallucinated description + a real category
    must produce ZERO facets and never reach Claude."""
    from app.services import spec_enrichment_service
    from app.services.spec_enrichment_service import enrich_pending_specs

    card = _card(
        db,
        "GUESS123",
        MaterialEnrichmentStatus.NOT_FOUND,
        category="hdd",
        description="Likely a proprietary hard drive; could be a SAS unit.",
        enrichment_source=None,
    )

    with (
        patch(
            "app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("GUESS123")
        ) as mclaude,
        patch.object(spec_enrichment_service, "record_spec", autospec=True) as mrecord,
    ):
        stats = await enrich_pending_specs(db, limit=10)

    assert stats["cards_processed"] == 0
    mclaude.assert_not_awaited()
    mrecord.assert_not_called()  # record_spec never reached
    facets = db.query(MaterialSpecFacet).filter_by(material_card_id=card.id).all()
    assert facets == []


# ── force=True still honors the status gate ────────────────────────────


@pytest.mark.asyncio
async def test_force_does_not_bypass_status_gate(db: Session, _schemas):
    """Force=True bypasses the specs_enriched_at re-process filter, NOT the status
    gate."""
    from app.services.spec_enrichment_service import enrich_card_specs

    card = _card(db, "FORCED1", MaterialEnrichmentStatus.NOT_FOUND, enrichment_source=None)
    with patch(
        "app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=_payload("FORCED1")
    ) as mclaude:
        stats = await enrich_card_specs([card.id], db, force=True)

    assert stats["cards_processed"] == 0
    mclaude.assert_not_awaited()
