"""test_search_service_stream.py — Coverage tests for stream_search_mpn in app/search_service.py.

Covers lines 1959-2110: streaming search via SSE.

Called by: pytest
Depends on: app/search_service.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.search_service import stream_search_mpn
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used

# ── Test 1: No connectors → early "done" event ───────────────────────────────


class TestStreamSearchMpnNoConnectors:
    async def test_no_connectors_publishes_done(self, db_session: Session):
        """When _build_connectors returns empty list, broker.publish is called once with 'done'."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        with (
            patch("app.search_service._build_connectors", return_value=([], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            await stream_search_mpn("test-search-001", "LM317T", db_session)

        assert mock_broker.publish.call_count == 1
        call_args = mock_broker.publish.call_args
        # Second positional arg is event_type
        assert call_args[0][1] == "done"

    async def test_no_connectors_done_payload_has_zero_results(self, db_session: Session):
        """The 'done' event payload reports zero results when no connectors run."""
        import json

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        with (
            patch("app.search_service._build_connectors", return_value=([], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            await stream_search_mpn("test-search-002", "LM317T", db_session)

        payload = json.loads(mock_broker.publish.call_args[0][2])
        assert payload["total_results"] == 0
        assert payload["sources"] == 0


# ── Test 2: Single connector returns results → results published ─────────────


class TestStreamSearchMpnWithResults:
    async def test_connector_results_publishes_source_status_and_done(self, db_session: Session):
        """Connector returning hits triggers source-status and done events."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "brokerbin"
        mock_conn.search = AsyncMock(
            return_value=[{"mpn_matched": "LM317T", "vendor_name": "Arrow", "qty_available": 100}]
        )

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-search-003", "LM317T", db_session)

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "source-status" in event_types
        assert "done" in event_types

    async def test_connector_results_no_exception_raised(self, db_session: Session):
        """Stream completes without raising even when connector returns results."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "digikey"
        mock_conn.search = AsyncMock(
            return_value=[{"mpn_matched": "LM317T", "vendor_name": "DigiKey", "qty_available": 50}]
        )

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value=""),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            # Should not raise
            await stream_search_mpn("test-search-004", "LM317T", db_session)

        assert mock_broker.publish.called

    async def test_new_cards_triggers_results_event(self, db_session: Session):
        """When _incremental_dedup returns new_cards, a 'results' event is published."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "mouser"
        mock_conn.search = AsyncMock(
            return_value=[{"mpn_matched": "LM317T", "vendor_name": "Mouser", "qty_available": 200}]
        )

        fake_card = {"mpn_matched": "LM317T", "vendor_name": "Mouser", "qty_available": 200}

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div>card</div>"),
            patch("app.search_service._incremental_dedup", return_value=([fake_card], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-search-005", "LM317T", db_session)

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "results" in event_types

    async def test_updated_cards_triggers_card_update_event(self, db_session: Session):
        """When _incremental_dedup returns updated_cards, a 'card-update' event is published."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "nexar"
        mock_conn.search = AsyncMock(
            return_value=[{"mpn_matched": "LM317T", "vendor_name": "Nexar", "qty_available": 300}]
        )

        fake_updated = {"mpn_matched": "LM317T", "vendor_name": "Nexar", "qty_available": 300}

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div>updated</div>"),
            patch("app.search_service._incremental_dedup", return_value=([], [fake_updated])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-search-006", "LM317T", db_session)

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "card-update" in event_types


# ── Test 3: Connector raises exception → error status, continues ─────────────


class TestStreamSearchMpnConnectorException:
    async def test_connector_exception_publishes_error_source_status(self, db_session: Session):
        """When connector.search raises, source-status error is published and no exception propagates."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "brokerbin"
        mock_conn.search = AsyncMock(side_effect=RuntimeError("API down"))

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            # Must not raise
            await stream_search_mpn("test-search-007", "LM317T", db_session)

        import json

        source_status_calls = [call for call in mock_broker.publish.call_args_list if call[0][1] == "source-status"]
        assert len(source_status_calls) == 1
        payload = json.loads(source_status_calls[0][0][2])
        assert payload["status"] == "error"
        assert "API down" in payload["error"]

    async def test_connector_exception_still_publishes_done(self, db_session: Session):
        """Even after a connector exception, the 'done' event is published."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "nexar"
        mock_conn.search = AsyncMock(side_effect=ValueError("Timeout"))

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            await stream_search_mpn("test-search-008", "LM317T", db_session)

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "done" in event_types

    async def test_multiple_connectors_one_fails_other_succeeds(self, db_session: Session):
        """With two connectors, one failing and one succeeding, both source-status events are published."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn_fail = MagicMock()
        mock_conn_fail.source_name = "brokerbin"
        mock_conn_fail.search = AsyncMock(side_effect=RuntimeError("fail"))

        mock_conn_ok = MagicMock()
        mock_conn_ok.source_name = "digikey"
        mock_conn_ok.search = AsyncMock(return_value=[])

        with (
            patch(
                "app.search_service._build_connectors",
                return_value=([mock_conn_fail, mock_conn_ok], {}, set()),
            ),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value=""),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-search-009", "LM317T", db_session)

        import json

        source_status_calls = [call for call in mock_broker.publish.call_args_list if call[0][1] == "source-status"]
        assert len(source_status_calls) == 2
        statuses = {json.loads(c[0][2])["source"]: json.loads(c[0][2])["status"] for c in source_status_calls}
        assert statuses["brokerbin"] == "error"
        assert statuses["digikey"] == "ok"


# ── Test 4: Redis cache attempt with no real Redis → no exception ─────────────


class TestStreamSearchMpnRedisCacheFailure:
    async def test_redis_unavailable_no_exception(self, db_session: Session):
        """Function completes gracefully when Redis cache attempt fails."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        # _get_search_redis raises so the except branch (line 2098) is hit
        with (
            patch("app.search_service._build_connectors", return_value=([], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._get_search_redis", side_effect=Exception("no redis")),
        ):
            await stream_search_mpn("test-search-010", "LM317T", db_session)

        # Still publishes done
        assert mock_broker.publish.called

    async def test_redis_setex_failure_no_exception(self, db_session: Session):
        """When Redis setex fails, no exception propagates."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "brokerbin"
        mock_conn.search = AsyncMock(return_value=[])

        mock_redis = MagicMock()
        mock_redis.setex = MagicMock(side_effect=Exception("Redis write failed"))

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._get_search_redis", return_value=mock_redis),
            patch("app.search_service._render_search_vendor_cards_html", return_value=""),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-search-011", "LM317T", db_session)

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "done" in event_types

    async def test_redis_none_no_exception(self, db_session: Session):
        """When _get_search_redis returns None, cache block is skipped cleanly."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        with (
            patch("app.search_service._build_connectors", return_value=([], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._get_search_redis", return_value=None),
        ):
            await stream_search_mpn("test-search-012", "LM317T", db_session)

        assert mock_broker.publish.called


# ── Test 5: Done event payload correctness ───────────────────────────────────


class TestStreamSearchMpnDonePayload:
    async def test_done_payload_has_correct_keys(self, db_session: Session):
        """The 'done' event payload contains total_results, sources, elapsed_seconds."""
        import json

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.source_name = "mouser"
        mock_conn.search = AsyncMock(return_value=[])

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value=""),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
            patch("app.search_service._get_search_redis", return_value=None),
        ):
            await stream_search_mpn("test-search-013", "LM317T", db_session)

        done_calls = [call for call in mock_broker.publish.call_args_list if call[0][1] == "done"]
        assert len(done_calls) == 1
        payload = json.loads(done_calls[0][0][2])
        assert "total_results" in payload
        assert "sources" in payload
        assert "elapsed_seconds" in payload
        assert payload["sources"] == 1

    async def test_done_channel_matches_search_id(self, db_session: Session):
        """The 'done' event is published to the correct SSE channel."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        with (
            patch("app.search_service._build_connectors", return_value=([], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            await stream_search_mpn("my-unique-id-999", "LM317T", db_session)

        channel_used = mock_broker.publish.call_args[0][0]
        assert channel_used == "search:my-unique-id-999"
