"""Tests for Nexar batch enrichment — mocked API responses.

Called by: pytest
Depends on: app.services.tagging_nexar, app.models
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging_nexar import run_nexar_backfill

# ── Helpers ────────────────────────────────────────────────────────────


def _make_card(db, mpn, manufacturer=None):
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _mock_nexar_response(mpn, manufacturer, category=None):
    """Build a mock Nexar GraphQL response dict."""
    return {
        "data": {
            "supSearchMpn": {
                "results": [
                    {
                        "part": {
                            "mpn": mpn,
                            "manufacturer": {"name": manufacturer} if manufacturer else None,
                            "category": {"name": category} if category else None,
                        }
                    }
                ]
            }
        }
    }


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nexar_backfill_creates_tags(db_session):
    _make_card(db_session, "CUSTOM123")

    async def mock_run_query(self, query, mpn):
        return _mock_nexar_response(mpn, "Texas Instruments", "Analog ICs")

    with (
        patch("app.services.credential_service.get_credential_cached", return_value="fake-key"),
        patch("app.connectors.sources.NexarConnector._run_query", mock_run_query),
    ):
        result = await run_nexar_backfill(db_session, batch_size=10, delay_seconds=0)

    assert result["total_processed"] == 1
    assert result["total_matched"] == 1
    assert db_session.query(MaterialTag).count() >= 1


@pytest.mark.asyncio
async def test_nexar_backfill_updates_material_card_fields(db_session):
    card = _make_card(db_session, "MYSTERY456")

    async def mock_run_query(self, query, mpn):
        return _mock_nexar_response(mpn, "Microchip Technology", "Microcontrollers")

    with (
        patch("app.services.credential_service.get_credential_cached", return_value="fake-key"),
        patch("app.connectors.sources.NexarConnector._run_query", mock_run_query),
    ):
        await run_nexar_backfill(db_session, batch_size=10, delay_seconds=0)

    db_session.refresh(card)
    assert card.manufacturer == "Microchip Technology"


@pytest.mark.asyncio
async def test_nexar_backfill_skips_already_tagged(db_session):
    card = _make_card(db_session, "TAGGED789")
    tag = Tag(name="Existing", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(tag)
    db_session.flush()
    db_session.add(MaterialTag(material_card_id=card.id, tag_id=tag.id, confidence=0.9, source="prefix_lookup"))
    db_session.commit()

    with patch("app.services.credential_service.get_credential_cached", return_value="fake-key"):
        result = await run_nexar_backfill(db_session, batch_size=10, delay_seconds=0)

    assert result["total_processed"] == 0


@pytest.mark.asyncio
async def test_nexar_backfill_no_credentials(db_session):
    _make_card(db_session, "NOCREDS123")

    with patch("app.services.credential_service.get_credential_cached", return_value=None):
        result = await run_nexar_backfill(db_session, batch_size=10, delay_seconds=0)

    # Should skip all since no credentials
    assert result["total_matched"] == 0


@pytest.mark.asyncio
async def test_nexar_backfill_untagged_no_creds(db_session):
    """nexar_backfill_untagged returns early when no credentials."""
    from app.services.enrichment import nexar_backfill_untagged

    result = await nexar_backfill_untagged(db_session, limit=10)
    assert result["total_checked"] == 0
