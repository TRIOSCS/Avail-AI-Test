"""Tests for search streaming, aggressive dedup, and shortlist features.

Called by: pytest
Depends on: app/search_service.py, app/connectors/sources.py
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.mouser import MouserConnector
from app.connectors.sources import NexarConnector


def test_base_connector_has_source_name():
    """Each connector exposes a source_name property matching its source_type."""
    nexar = NexarConnector.__new__(NexarConnector)
    assert hasattr(nexar, "source_name")
    assert isinstance(nexar.source_name, str)
    assert len(nexar.source_name) > 0


def test_build_connectors_all_skipped_when_no_creds(db_session):
    """_build_connectors skips all sources when no credentials are configured."""
    from app.search_service import _build_connectors

    with patch("app.search_service.get_credentials_batch", return_value={}):
        connectors, stats, disabled = _build_connectors(db_session)

    assert isinstance(connectors, list)
    assert isinstance(stats, dict)
    assert isinstance(disabled, set)
    assert len(connectors) == 0
    assert any(s["status"] in ("skipped", "disabled") for s in stats.values())


def test_build_connectors_instantiates_with_creds(db_session):
    """_build_connectors creates connector instances when credentials exist."""
    from app.search_service import _build_connectors

    fake_creds = {("mouser", "MOUSER_API_KEY"): "fake-mouser-key"}
    with patch("app.search_service.get_credentials_batch", return_value=fake_creds):
        connectors, stats, disabled = _build_connectors(db_session)

    assert len(connectors) == 1
    assert isinstance(connectors[0], MouserConnector)
    # Mouser should not appear in stats (it was instantiated, not skipped)
    assert "mouser" not in stats
    # Other sources should be skipped
    assert stats["nexar"]["status"] == "skipped"


# ── Aggressive dedup tests ──────────────────────────────────────────────


def test_aggressive_dedup_groups_by_vendor():
    """Same vendor with different prices should merge into one entry with sub_offers."""
    from app.search_service import _deduplicate_sightings_aggressive

    sightings = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 1000,
            "score": 80,
            "confidence": 0.8,
            "source_type": "nexar",
            "is_authorized": True,
            "moq": 1,
        },
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.48,
            "qty_available": 500,
            "score": 70,
            "confidence": 0.7,
            "source_type": "digikey",
            "is_authorized": True,
            "moq": 10,
        },
        {
            "vendor_name": "Mouser",
            "mpn_matched": "LM317T",
            "unit_price": 0.50,
            "qty_available": 2000,
            "score": 75,
            "confidence": 0.75,
            "source_type": "mouser",
            "is_authorized": True,
            "moq": 1,
        },
    ]
    result = _deduplicate_sightings_aggressive(sightings)

    # Should produce 2 entries: Arrow (merged) and Mouser
    assert len(result) == 2
    arrow = next(r for r in result if "arrow" in r["vendor_name"].lower())
    assert arrow["unit_price"] == 0.45  # best offer (highest score)
    assert arrow["qty_available"] == 1500  # summed
    assert len(arrow["sub_offers"]) == 1  # the other Arrow offer
    assert arrow["offer_count"] == 2
    assert "nexar" in arrow["sources_found"]
    assert "digikey" in arrow["sources_found"]


def test_aggressive_dedup_filters_zero_qty():
    """Sightings with qty_available=0 are excluded."""
    from app.search_service import _deduplicate_sightings_aggressive

    sightings = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 0,
            "score": 80,
            "confidence": 0.8,
            "source_type": "nexar",
            "is_authorized": True,
        },
    ]
    result = _deduplicate_sightings_aggressive(sightings)
    assert len(result) == 0


def test_incremental_dedup_new_vendor():
    """New vendor results in new_cards list."""
    from app.search_service import _incremental_dedup

    existing = []
    incoming = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 1000,
            "score": 80,
            "source_type": "nexar",
        },
    ]
    new_cards, updated_cards = _incremental_dedup(incoming, existing)
    assert len(new_cards) == 1
    assert len(updated_cards) == 0


def test_incremental_dedup_existing_vendor():
    """Existing vendor results in updated_cards list with merged sub_offers."""
    from app.search_service import _incremental_dedup

    existing = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 1000,
            "score": 80,
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": {"nexar"},
        },
    ]
    incoming = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.48,
            "qty_available": 500,
            "score": 70,
            "source_type": "digikey",
        },
    ]
    new_cards, updated_cards = _incremental_dedup(incoming, existing)
    assert len(new_cards) == 0
    assert len(updated_cards) == 1
    assert updated_cards[0]["offer_count"] == 2


# ── Streaming search tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_search_publishes_events(db_session):
    """stream_search_mpn publishes source-status and results events to the SSE
    broker."""
    from app.search_service import stream_search_mpn

    published_events = []

    async def mock_publish(channel, event, data=""):
        published_events.append({"channel": channel, "event": event, "data": data})

    # Mock broker and connectors
    with (
        patch("app.search_service.broker", create=True) as mock_broker,
        patch("app.search_service._build_connectors") as mock_build,
    ):
        mock_broker.publish = mock_publish

        # One fake connector that returns one result
        fake_connector = MagicMock()
        fake_connector.source_name = "nexar"
        fake_connector.search = AsyncMock(
            return_value=[
                {
                    "vendor_name": "Arrow",
                    "mpn_matched": "LM317T",
                    "unit_price": 0.45,
                    "qty_available": 1000,
                    "source_type": "nexar",
                    "is_authorized": True,
                }
            ]
        )
        mock_build.return_value = ([fake_connector], {}, set())

        await stream_search_mpn("test-search-id", "LM317T", db_session)

    # Should have published source-status + results + done events
    event_types = [e["event"] for e in published_events]
    assert "source-status" in event_types
    assert "results" in event_types
    assert "done" in event_types
    assert all(e["channel"] == "search:test-search-id" for e in published_events)

    # Verify done event stats have correct keys
    done_event = next(e for e in published_events if e["event"] == "done")
    done_data = json.loads(done_event["data"])
    assert "total_results" in done_data
    assert "elapsed_seconds" in done_data
