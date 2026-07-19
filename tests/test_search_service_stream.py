"""test_search_service_stream.py — Coverage tests for stream_search_mpn in
app/search_service.py.

Covers lines 1959-2110: streaming search via SSE.

Called by: pytest
Depends on: app/search_service.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.search_service import stream_search_mpn
from tests.conftest import engine  # noqa: F401


@pytest.fixture(autouse=True)
def _own_session(db_session):
    """stream_search_mpn opens its own SessionLocal(); point it at the test session so
    the worker does not touch the real database."""
    with patch("app.search_service.SessionLocal", lambda: db_session):
        yield


# ── Test 1: No connectors → early "done" event ───────────────────────────────


class TestStreamSearchMpnNoConnectors:
    async def test_no_connectors_publishes_done(self, db_session: Session):
        """When _build_connectors returns empty list, broker.publish is called once with
        'done'."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        with (
            patch("app.search_service._build_connectors", return_value=([], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            await stream_search_mpn("test-search-001", "LM317T")

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
            await stream_search_mpn("test-search-002", "LM317T")

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
            await stream_search_mpn("test-search-003", "LM317T")

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
            await stream_search_mpn("test-search-004", "LM317T")

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
            await stream_search_mpn("test-search-005", "LM317T")

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "results" in event_types

    async def test_updated_cards_triggers_card_update_event(self, db_session: Session):
        """When _incremental_dedup returns updated_cards, a 'card-update' event is
        published."""
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
            await stream_search_mpn("test-search-006", "LM317T")

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "card-update" in event_types


# ── Test 3: Connector raises exception → error status, continues ─────────────


class TestStreamSearchMpnConnectorException:
    async def test_connector_exception_publishes_error_source_status(self, db_session: Session):
        """When connector.search raises, source-status error is published and no
        exception propagates."""
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
            await stream_search_mpn("test-search-007", "LM317T")

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
            await stream_search_mpn("test-search-008", "LM317T")

        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "done" in event_types

    async def test_multiple_connectors_one_fails_other_succeeds(self, db_session: Session):
        """With two connectors, one failing and one succeeding, both source-status
        events are published."""
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
            await stream_search_mpn("test-search-009", "LM317T")

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
            await stream_search_mpn("test-search-010", "LM317T")

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
            await stream_search_mpn("test-search-011", "LM317T")

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
            await stream_search_mpn("test-search-012", "LM317T")

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
            await stream_search_mpn("test-search-013", "LM317T")

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
            await stream_search_mpn("my-unique-id-999", "LM317T")

        channel_used = mock_broker.publish.call_args[0][0]
        assert channel_used == "search:my-unique-id-999"


# ── Test: per-MPN :latest pointer key (Part Dossier market cache-hit contract) ──


class TestStreamSearchMpnLatestPointer:
    async def test_writes_per_mpn_latest_pointer_key(self, db_session: Session):
        """stream_search_mpn writes search:{normalized_mpn}:latest -> search_id (900s
        TTL) alongside the results cache, so the Part Dossier market cache-hit path can
        find the freshest run for an MPN.

        Without it /v2/partials/search/dossier/market never hits the cache and always
        re-fires the connector sweep.
        """
        from app.utils.normalization import normalize_mpn_key

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()
        # A connector is needed so the flow reaches the results-cache write (zero connectors
        # short-circuits to an early "done").
        mock_conn = MagicMock()
        mock_conn.source_name = "brokerbin"
        mock_conn.search = AsyncMock(
            return_value=[{"mpn_matched": "LM317T", "vendor_name": "Arrow", "qty_available": 100}]
        )
        mock_rc = MagicMock()

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
            patch("app.search_service._get_search_redis", return_value=mock_rc),
        ):
            await stream_search_mpn("sid-pointer-1", "LM317T")

        key = normalize_mpn_key("LM317T")
        setex_keys = {c.args[0]: c.args for c in mock_rc.setex.call_args_list}
        assert f"search:{key}:latest" in setex_keys, setex_keys
        assert setex_keys[f"search:{key}:latest"] == (f"search:{key}:latest", 900, "sid-pointer-1")
        assert "search:sid-pointer-1:results" in setex_keys


# ── Test: Relevance guard — off-target (different MPN) hits are excluded ──────
#
# Connectors that keyword-match (e.g. component distributors hit with a storage
# FRU) return rows under a DIFFERENT mpn_matched. Those are catalog noise, not
# offers for the searched part, so stream_search_mpn must drop them BEFORE
# scoring/dedup and report the dropped count via the "done" event's off_target.


class TestStreamSearchMpnRelevanceGuard:
    def _conn(self, hits):
        c = MagicMock()
        c.source_name = "mouser"
        c.search = AsyncMock(return_value=hits)
        return c

    async def test_off_target_hit_excluded_and_counted(self, db_session: Session):
        """Off-target hits (different MPN) are dropped pre-scoring; off_target counts
        them."""
        import json

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()
        captured: dict = {}

        def fake_dedup(scored, acc):
            captured["scored"] = list(scored)
            acc.extend(scored)
            return (list(scored), [])

        conn = self._conn(
            [
                {"mpn_matched": "17P9905", "vendor_name": "BrokerCo"},  # on-target
                {"mpn_matched": "1300940294", "vendor_name": "Mouser"},  # off-target
            ]
        )

        with (
            patch("app.search_service._build_connectors", return_value=([conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", side_effect=fake_dedup),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-relevance-001", "17P9905")

        assert len(captured["scored"]) == 1
        assert captured["scored"][0]["mpn_matched"] == "17P9905"

        done = [c for c in mock_broker.publish.call_args_list if c[0][1] == "done"][0]
        payload = json.loads(done[0][2])
        assert payload["off_target"] == 1
        assert payload["total_results"] == 1

    async def test_revision_suffix_kept(self, db_session: Session):
        """A close revision (17P9905 vs 17P9905-LF) is a match — NOT dropped."""
        import json

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()
        captured: dict = {}

        def fake_dedup(scored, acc):
            captured["scored"] = list(scored)
            return ([], [])

        conn = self._conn([{"mpn_matched": "17P9905-LF", "vendor_name": "BrokerCo"}])

        with (
            patch("app.search_service._build_connectors", return_value=([conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", side_effect=fake_dedup),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-relevance-002", "17P9905")

        assert len(captured["scored"]) == 1
        done = [c for c in mock_broker.publish.call_args_list if c[0][1] == "done"][0]
        assert json.loads(done[0][2])["off_target"] == 0

    async def test_missing_mpn_defaults_to_query_and_is_kept(self, db_session: Session):
        """A hit with no mpn_matched defaults to the searched MPN and is kept."""
        import json

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()
        captured: dict = {}

        def fake_dedup(scored, acc):
            captured["scored"] = list(scored)
            return ([], [])

        conn = self._conn([{"vendor_name": "BrokerCo", "qty_available": 5}])  # no mpn_matched

        with (
            patch("app.search_service._build_connectors", return_value=([conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", side_effect=fake_dedup),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-relevance-003", "17P9905")

        assert len(captured["scored"]) == 1
        assert captured["scored"][0]["mpn_matched"] == "17P9905"
        done = [c for c in mock_broker.publish.call_args_list if c[0][1] == "done"][0]
        assert json.loads(done[0][2])["off_target"] == 0

    async def test_multiple_connectors_off_target_accumulates(self, db_session: Session):
        """off_target sums across connectors; only on-target hits reach dedup."""
        import json

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()
        scored_seen: list = []

        def fake_dedup(scored, acc):
            scored_seen.extend(scored)
            return ([], [])

        c1 = MagicMock()
        c1.source_name = "brokerbin"
        c1.search = AsyncMock(
            return_value=[{"mpn_matched": "17P9905", "vendor_name": "A"}, {"mpn_matched": "WRONG1", "vendor_name": "B"}]
        )
        c2 = MagicMock()
        c2.source_name = "mouser"
        c2.search = AsyncMock(
            return_value=[{"mpn_matched": "17P9905", "vendor_name": "C"}, {"mpn_matched": "WRONG2", "vendor_name": "D"}]
        )

        with (
            patch("app.search_service._build_connectors", return_value=([c1, c2], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", side_effect=fake_dedup),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-relevance-multi", "17P9905")

        assert len(scored_seen) == 2  # one on-target hit from each connector
        assert {r["mpn_matched"] for r in scored_seen} == {"17P9905"}
        done = [c for c in mock_broker.publish.call_args_list if c[0][1] == "done"][0]
        assert json.loads(done[0][2])["off_target"] == 2  # one off-target from each connector

    async def test_off_target_not_counted_on_error_path(self, db_session: Session):
        """A crashed connector adds 0 to off_target (the guard runs only on success)."""
        import json

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()
        c = MagicMock()
        c.source_name = "brokerbin"
        c.search = AsyncMock(side_effect=RuntimeError("API down"))

        with (
            patch("app.search_service._build_connectors", return_value=([c], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            await stream_search_mpn("test-relevance-err", "17P9905")

        done = [c for c in mock_broker.publish.call_args_list if c[0][1] == "done"][0]
        assert json.loads(done[0][2])["off_target"] == 0


# ── Shared 15-min search-result cache (the same one _fetch_fresh reads/writes) ──


class TestStreamSearchMpnSharedCache:
    async def test_cache_hit_skips_live_fanout(self, db_session: Session):
        """A shared cache HIT serves results without touching any connector's search() —
        before this fix the streaming path never consulted the cache _fetch_fresh
        reads/writes, so an interactive search always re-hit live supplier APIs even
        moments after an identical requisition search."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.__class__.__name__ = "NexarConnector"
        mock_conn.source_name = "nexar"
        mock_conn.search = AsyncMock(return_value=[])

        cached_results = [
            {
                "vendor_name": "CachedVendor",
                "mpn_matched": "LM317T",
                "qty_available": 10,
                "unit_price": 1.0,
                "source_type": "nexar",
                "confidence": 3,
            }
        ]
        cached_stats = [{"source": "nexar", "results": 1, "ms": 50, "error": None, "status": "ok"}]

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch(
                "app.search_service._get_search_cache",
                return_value=(cached_results, cached_stats, "2026-01-01T00:00:00+00:00"),
            ),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
        ):
            await stream_search_mpn("test-cache-hit", "LM317T")

        mock_conn.search.assert_not_called()
        event_types = [call[0][1] for call in mock_broker.publish.call_args_list]
        assert "results" in event_types
        assert "done" in event_types

    async def test_cache_miss_writes_shared_cache(self, db_session: Session):
        """A cache MISS runs the live fan-out AND writes the shared cache in the same
        flat/unscored shape _fetch_fresh's cache-miss path writes, so a later
        requisition search (or another streaming search) of this MPN gets a cache
        HIT."""
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.__class__.__name__ = "NexarConnector"
        mock_conn.source_name = "nexar"
        mock_conn.search = AsyncMock(
            return_value=[{"mpn_matched": "LM317T", "vendor_name": "Arrow", "qty_available": 10, "unit_price": 1.0}]
        )

        set_calls = []

        def _capture_set(key, results, stats):
            set_calls.append((key, results, stats))

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._get_search_cache", return_value=None),
            patch("app.search_service._set_search_cache", side_effect=_capture_set),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await stream_search_mpn("test-cache-miss", "LM317T")

        mock_conn.search.assert_called_once()
        assert set_calls, "shared cache was not written on a cache MISS"
        _key, results, stats = set_calls[0]
        assert results and results[0]["vendor_name"] == "Arrow"
        assert stats and stats[0]["source"] == "nexar"
