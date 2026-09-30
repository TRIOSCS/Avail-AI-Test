"""Tests for search streaming, aggressive dedup, and shortlist features.

Called by: pytest
Depends on: app/search_service.py, app/connectors/sources.py
"""

import json
import re
from itertools import permutations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.mouser import MouserConnector
from app.connectors.sources import NexarConnector


def _render_template(name, **context):
    """Render a Jinja2 template from app/templates with the given context."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/templates"))
    return env.get_template(name).render(**context)


def _render_vendor_card(card, card_index, search_id):
    """Render the vendor_card.html partial for a single card."""
    return _render_template(
        "htmx/partials/search/vendor_card.html",
        card=card,
        card_index=card_index,
        search_id=search_id,
    )


def _make_event_collector():
    """Return (events_list, async_publish) for capturing broker.publish calls."""
    published_events = []

    async def mock_publish(channel, event, data=""):
        published_events.append({"channel": channel, "event": event, "data": data})

    return published_events, mock_publish


def _tile_values(html):
    """Map each market-summary KPI tile label to its rendered value.

    market_summary.html emits, per tile, a ``<span class="block ...">value</span>``
    immediately followed by a ``<span class="block ...">label</span>``.
    """
    pairs = re.findall(
        r'<span class="block[^"]*">\s*([^<]*?)\s*</span>\s*'
        r'<span class="block[^"]*">\s*(Vendors|Offers|Authorized|High confidence|Best price|Total stock)\s*</span>',
        html,
    )
    return {label: value for value, label in pairs}


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


def test_build_connectors_brokerbin_gates_on_bearer_token(db_session):
    """BrokerBin v2.x uses Bearer auth — only the API key (token) is required.

    The legacy ``BROKERBIN_API_SECRET`` slot is no longer load-bearing for v2.x
    keys; it remains in the schema for backward compatibility but is ignored at
    request time.
    """
    from app.connectors.sources import BrokerBinConnector
    from app.search_service import _build_connectors

    # Token only → connector built (Bearer doesn't need a username)
    only_key = {("brokerbin", "BROKERBIN_API_KEY"): "key-only"}
    with patch("app.search_service.get_credentials_batch", return_value=only_key):
        connectors, stats, _ = _build_connectors(db_session)
    assert any(isinstance(c, BrokerBinConnector) for c in connectors)
    assert "brokerbin" not in stats

    # No token → skipped (Bearer auth requires the token)
    only_secret = {("brokerbin", "BROKERBIN_API_SECRET"): "user-only"}
    with patch("app.search_service.get_credentials_batch", return_value=only_secret):
        connectors, stats, _ = _build_connectors(db_session)
    assert not any(isinstance(c, BrokerBinConnector) for c in connectors)
    assert stats["brokerbin"]["status"] == "skipped"


# ── _build_connectors config cache (60s TTL, no-op under TESTING) ────────


class TestConnectorConfigCache:
    def test_noop_under_testing(self, db_session):
        """Under TESTING=1 the connector-config cache never serves a stale value — every
        _build_connectors call re-queries credentials, so tests stay deterministic
        without needing the reset hook."""
        from app.search_service import _build_connectors, _reset_connector_config_cache

        _reset_connector_config_cache()
        call_count = {"n": 0}

        def _counting_batch(_db, _keys):
            call_count["n"] += 1
            return {}

        with patch("app.search_service.get_credentials_batch", side_effect=_counting_batch):
            _build_connectors(db_session)
            _build_connectors(db_session)

        assert call_count["n"] == 2  # no caching under TESTING

    def test_caches_across_calls_when_not_testing(self, db_session):
        """Outside TESTING, a second _build_connectors call within the 60s TTL is served
        from the in-process cache — no repeat DB round trip for the disabled/errored
        source sets + batched credentials."""
        import os

        from app.search_service import _build_connectors, _reset_connector_config_cache

        _reset_connector_config_cache()
        call_count = {"n": 0}

        def _counting_batch(_db, _keys):
            call_count["n"] += 1
            return {}

        original = os.environ.pop("TESTING", None)
        try:
            with patch("app.search_service.get_credentials_batch", side_effect=_counting_batch):
                _build_connectors(db_session)
                _build_connectors(db_session)
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            _reset_connector_config_cache()

        assert call_count["n"] == 1  # second call served from the 60s cache

    def test_reset_forces_a_fresh_lookup(self, db_session):
        """_reset_connector_config_cache() invalidates immediately, without waiting out
        the 60s TTL — the hook a settings/credential mutation point (or a test) uses to
        force fresh config."""
        import os

        from app.search_service import _build_connectors, _reset_connector_config_cache

        _reset_connector_config_cache()
        call_count = {"n": 0}

        def _counting_batch(_db, _keys):
            call_count["n"] += 1
            return {}

        original = os.environ.pop("TESTING", None)
        try:
            with patch("app.search_service.get_credentials_batch", side_effect=_counting_batch):
                _build_connectors(db_session)
                _reset_connector_config_cache()
                _build_connectors(db_session)
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            _reset_connector_config_cache()

        assert call_count["n"] == 2  # reset forced a fresh lookup


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
            "sources_found": ["nexar"],
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


def _dedup_offer(score, qty, price, source):
    """One scored offer for the same vendor + MPN, as _score_raw_hit would emit."""
    return {
        "vendor_name": "Arrow",
        "mpn_matched": "LM317T",
        "unit_price": price,
        "qty_available": qty,
        "score": score,
        "source_type": source,
    }


def test_incremental_dedup_three_offers_sum_each_qty_once():
    """Three offers for one vendor+MPN merged across successive calls (as connectors
    complete) sum each offer's qty exactly once: q0 + q1 + q2.

    The pre-fix re-sum re-added the previous running total on every merge past the
    second, yielding q0 + 2*q1 + q2 (1000 + 2*500 + 250 = 2250 instead of 1750).
    """
    from app.search_service import _incremental_dedup

    q0, q1, q2 = 1000, 500, 250
    existing: list[dict] = []

    new_cards, _ = _incremental_dedup([_dedup_offer(80, q0, 0.45, "nexar")], existing)
    assert len(new_cards) == 1
    assert existing[0]["qty_available"] == q0

    _, updated = _incremental_dedup([_dedup_offer(70, q1, 0.48, "digikey")], existing)
    assert updated == [existing[0]]
    assert existing[0]["qty_available"] == q0 + q1

    _, updated = _incremental_dedup([_dedup_offer(60, q2, 0.50, "mouser")], existing)
    assert updated == [existing[0]]

    assert len(existing) == 1
    card = existing[0]
    assert card["qty_available"] == q0 + q1 + q2
    assert card["offer_count"] == 3
    assert card["sources_found"] == ["digikey", "mouser", "nexar"]  # sorted list, JSON-safe for the Redis cache
    # No head swap: the first (best-scored) offer stays the head, each sub-offer keeps its own qty.
    assert card["unit_price"] == 0.45
    assert sorted(o["qty_available"] for o in card["sub_offers"]) == sorted([q1, q2])


def test_incremental_dedup_three_offers_head_swap_on_third_keeps_own_quantities():
    """When the THIRD offer has a better score it becomes the head; the old head is
    demoted to sub_offers with its OWN qty (not the running total) and the final qty is
    still q0 + q1 + q2."""
    from app.search_service import _incremental_dedup

    q0, q1, q2 = 1000, 500, 250
    existing: list[dict] = []

    _incremental_dedup([_dedup_offer(70, q0, 0.45, "nexar")], existing)
    _incremental_dedup([_dedup_offer(60, q1, 0.48, "digikey")], existing)
    assert existing[0]["qty_available"] == q0 + q1

    _, updated = _incremental_dedup([_dedup_offer(90, q2, 0.40, "mouser")], existing)
    assert updated == [existing[0]]

    card = existing[0]
    assert card["qty_available"] == q0 + q1 + q2  # pre-fix: 250 + 500 + 1500 = 2250
    assert card["offer_count"] == 3
    # Head is now the third (best-scored) offer ...
    assert card["score"] == 90
    assert card["unit_price"] == 0.40
    assert card["source_type"] == "mouser"
    # ... and the demoted old head carries its own qty, not the running total.
    assert sorted(o["qty_available"] for o in card["sub_offers"]) == sorted([q0, q1])
    assert {o["source_type"] for o in card["sub_offers"]} == {"nexar", "digikey"}
    # Head-only bookkeeping never leaks into the sub-offer dicts.
    for sub in card["sub_offers"]:
        assert "sub_offers" not in sub
        assert "offer_count" not in sub
        assert "sources_found" not in sub
        assert "own_qty_available" not in sub


def test_incremental_dedup_three_offers_head_swap_on_second_then_plain_merge():
    """Head swap on the 2nd offer followed by a non-swapping 3rd merge still sums each
    qty once."""
    from app.search_service import _incremental_dedup

    q0, q1, q2 = 1000, 500, 250
    existing: list[dict] = []

    _incremental_dedup([_dedup_offer(60, q0, 0.45, "nexar")], existing)
    _incremental_dedup([_dedup_offer(80, q1, 0.48, "digikey")], existing)
    card = existing[0]
    assert card["source_type"] == "digikey"  # swapped head
    assert card["qty_available"] == q0 + q1

    _incremental_dedup([_dedup_offer(70, q2, 0.50, "mouser")], existing)
    assert card["qty_available"] == q0 + q1 + q2  # pre-fix: 1500 + 1000 + 250 = 2750
    assert card["source_type"] == "digikey"
    assert sorted(o["qty_available"] for o in card["sub_offers"]) == sorted([q0, q2])


@pytest.mark.parametrize("scores", list(permutations([10, 20, 30])))
def test_incremental_dedup_three_offers_every_score_order_sums_quantities_once(scores):
    """Whatever order the scores arrive in (head swap on any merge, or none), every
    intermediate and final qty_available is the plain sum of the offers merged so
    far."""
    from app.search_service import _incremental_dedup

    qtys = [1000, 500, 250]
    existing: list[dict] = []

    for i, (score, qty) in enumerate(zip(scores, qtys, strict=True)):
        _incremental_dedup([_dedup_offer(score, qty, 0.40 + i / 100, f"src{i}")], existing)
        assert len(existing) == 1
        assert existing[0]["qty_available"] == sum(qtys[: i + 1])

    card = existing[0]
    assert card["offer_count"] == 3
    assert card["score"] == max(scores)  # best-scored offer is the head
    head_qty = qtys[scores.index(max(scores))]
    assert sorted(o["qty_available"] for o in card["sub_offers"]) == sorted(q for q in qtys if q != head_qty)


def test_incremental_dedup_unknown_quantities_are_skipped_in_the_sum():
    """None quantities contribute nothing; all-None stays None; a 0-qty offer is dropped
    outright."""
    from app.search_service import _incremental_dedup

    existing: list[dict] = []
    _incremental_dedup([_dedup_offer(80, None, 0.45, "nexar")], existing)
    _incremental_dedup([_dedup_offer(70, None, 0.48, "digikey")], existing)
    assert existing[0]["qty_available"] is None

    _incremental_dedup([_dedup_offer(60, 100, 0.50, "mouser")], existing)
    assert existing[0]["qty_available"] == 100

    # A better-scored offer with a known qty takes the head; 100 + 50, the Nones add nothing.
    _incremental_dedup([_dedup_offer(90, 50, 0.40, "octopart")], existing)
    assert existing[0]["qty_available"] == 150
    assert existing[0]["offer_count"] == 4

    new_cards, updated = _incremental_dedup([_dedup_offer(95, 0, 0.10, "brokerbin")], existing)
    assert (new_cards, updated) == ([], [])
    assert existing[0]["qty_available"] == 150
    assert existing[0]["offer_count"] == 4


# ── Streaming search tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_search_publishes_events(db_session):
    """stream_search_mpn publishes source-status and results events to the SSE
    broker."""
    from app.search_service import stream_search_mpn

    published_events, mock_publish = _make_event_collector()

    # Mock broker and connectors. The worker now opens its own SessionLocal(),
    # so we patch it to return the test's db_session (which is bound to the
    # in-memory test engine with tables created by conftest).
    with (
        patch("app.search_service.broker", create=True) as mock_broker,
        patch("app.search_service._build_connectors") as mock_build,
        patch("app.search_service.SessionLocal", lambda: db_session),
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

        await stream_search_mpn("test-search-id", "LM317T")

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

    # SSE sse-swap="results" expects HTML vendor cards, not raw JSON
    for e in published_events:
        if e["event"] == "results":
            assert "vendor-card" in e["data"]
            assert '"cards"' not in e["data"]
    for e in published_events:
        if e["event"] == "card-update" and e["data"]:
            assert "hx-swap-oob" in e["data"]


@pytest.mark.asyncio
async def test_stream_search_publishes_summary_before_done(db_session):
    """A live run publishes the market-summary KPI strip as a "summary" event AFTER the
    result cards and BEFORE the terminal "done" event.

    "done" closes the SSE connection (sse-close), so a summary published after it never
    reaches the browser. The payload is the rendered market_summary.html HTML, built
    from rows scored by the real _score_raw_hit (so its confidence_color drives the
    "High confidence" tile).
    """
    from app.search_service import stream_search_mpn

    published_events, mock_publish = _make_event_collector()

    with (
        patch("app.search_service.broker", create=True) as mock_broker,
        patch("app.search_service._build_connectors") as mock_build,
        patch("app.search_service.SessionLocal", lambda: db_session),
    ):
        mock_broker.publish = mock_publish

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
                    "confidence": 4,  # 4/5 -> 80% -> green
                },
                {
                    "vendor_name": "Shady Broker",
                    "mpn_matched": "LM317T",
                    "unit_price": 0.20,
                    "qty_available": 10,
                    "source_type": "nexar",
                    "is_authorized": False,
                    "confidence": 0.2,  # 20% -> red
                },
            ]
        )
        mock_build.return_value = ([fake_connector], {}, set())

        await stream_search_mpn("test-summary-id", "LM317T")

    event_types = [e["event"] for e in published_events]
    assert event_types.count("summary") == 1
    assert event_types.count("done") == 1
    assert event_types.index("results") < event_types.index("summary") < event_types.index("done")
    assert event_types[-1] == "done"  # nothing is published after the terminal event

    summary = next(e for e in published_events if e["event"] == "summary")
    assert summary["channel"] == "search:test-summary-id"
    assert "Vendors" in summary["data"]
    assert "High confidence" in summary["data"]
    tiles = _tile_values(summary["data"])
    assert tiles["Vendors"] == "2"
    assert tiles["Authorized"] == "1"
    assert tiles["High confidence"] == "1"  # only Arrow is green
    assert tiles["Best price"] == "$0.2000"
    assert tiles["Total stock"] == "1,010"


@pytest.mark.asyncio
async def test_stream_search_no_hits_publishes_no_summary(db_session):
    """No accumulated rows → no "summary" event (the empty state owns that case); "done"
    still fires."""
    from app.search_service import stream_search_mpn

    published_events, mock_publish = _make_event_collector()

    with (
        patch("app.search_service.broker", create=True) as mock_broker,
        patch("app.search_service._build_connectors") as mock_build,
        patch("app.search_service.SessionLocal", lambda: db_session),
    ):
        mock_broker.publish = mock_publish
        fake_connector = MagicMock()
        fake_connector.source_name = "nexar"
        fake_connector.search = AsyncMock(return_value=[])
        mock_build.return_value = ([fake_connector], {}, set())

        await stream_search_mpn("test-no-hits-id", "LM317T")

    event_types = [e["event"] for e in published_events]
    assert "summary" not in event_types
    assert event_types[-1] == "done"


@pytest.mark.asyncio
async def test_stream_search_summary_render_failure_never_blocks_done(db_session):
    """The summary strip is best-effort: a render failure is swallowed, no "summary" is
    published, and the terminal "done" event still fires."""
    from app.search_service import stream_search_mpn

    published_events, mock_publish = _make_event_collector()

    with (
        patch("app.search_service.broker", create=True) as mock_broker,
        patch("app.search_service._build_connectors") as mock_build,
        patch("app.search_service.SessionLocal", lambda: db_session),
        patch("app.search_service._render_market_summary_html", side_effect=RuntimeError("template boom")),
    ):
        mock_broker.publish = mock_publish
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

        await stream_search_mpn("test-summary-boom-id", "LM317T")

    event_types = [e["event"] for e in published_events]
    assert "results" in event_types
    assert "summary" not in event_types
    assert event_types[-1] == "done"


# ── Route tests ───────────────────────────────────────────────────────


def test_search_run_returns_shell_html(client, db_session):
    """POST /v2/partials/search/run should return results shell with SSE connection."""
    with patch("app.search_service.stream_search_mpn", new_callable=AsyncMock):
        resp = client.post(
            "/v2/partials/search/run",
            data={"mpn": "LM317T"},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    html = resp.text
    assert "sse-connect" in html
    assert "source-chip" in html or "source-progress" in html


def test_vendor_card_template_renders():
    """vendor_card.html renders without errors with sample data."""
    html = _render_vendor_card(
        card={
            "vendor_name": "Arrow Electronics",
            "mpn_matched": "LM317T",
            "manufacturer": "Texas Instruments",
            "unit_price": 0.45,
            "qty_available": 12450,
            "moq": 1,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_badge": "Live Stock",
            "is_authorized": True,
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "reason": "Authorized distributor with confirmed stock",
        },
        card_index=0,
        search_id="test-123",
    )
    assert "Arrow Electronics" in html
    assert "LM317T" in html
    assert "0.4500" in html
    assert "12,450" in html
    assert "AUTH" in html
    assert "85%" in html
    assert "nexar" in html
    assert "Texas Instruments" in html


def test_render_search_vendor_cards_html_for_streaming():
    """_render_search_vendor_cards_html produces HTMX-safe HTML for SSE (not JSON)."""
    from app.search_service import _render_search_vendor_cards_html

    card = {
        "vendor_name": "Arrow",
        "mpn_matched": "LM317T",
        "manufacturer": "TI",
        "unit_price": 0.45,
        "qty_available": 100,
        "confidence_color": "green",
        "confidence_pct": 80,
        "lead_quality": "strong",
        "is_authorized": True,
        "source_type": "nexar",
        "sub_offers": [],
        "offer_count": 1,
        "sources_found": ["nexar"],
        "reason": "ok",
    }
    html = _render_search_vendor_cards_html([card], search_id="sid-1", start_index=3, swap_oob=False)
    assert "vendor-card" in html
    assert "Arrow" in html
    assert "hx-swap-oob" not in html

    html_oob = _render_search_vendor_cards_html([card], search_id="sid-1", start_index=0, swap_oob=True)
    assert 'hx-swap-oob="true"' in html_oob


def test_vendor_card_template_renders_no_price():
    """vendor_card.html renders gracefully when unit_price is None."""
    html = _render_vendor_card(
        card={
            "vendor_name": "Unknown Vendor",
            "mpn_matched": "ABC123",
            "manufacturer": None,
            "unit_price": None,
            "qty_available": 0,
            "moq": None,
            "confidence_color": "red",
            "confidence_pct": 20,
            "lead_quality": "",
            "is_authorized": False,
            "source_type": "brokerbin",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["brokerbin"],
            "reason": "",
        },
        card_index=3,
        search_id="test-456",
    )
    assert "Unknown Vendor" in html
    # Re-skinned terminal row shows "RFQ" (not "No price") when unit_price is None.
    assert "RFQ" in html
    assert "AUTH" not in html


def test_vendor_card_template_renders_sub_offers():
    """vendor_card.html renders expandable sub-offers table."""
    html = _render_vendor_card(
        card={
            "vendor_name": "Mouser",
            "mpn_matched": "LM317T",
            "manufacturer": "TI",
            "unit_price": 0.50,
            "qty_available": 3000,
            "moq": 10,
            "confidence_color": "amber",
            "confidence_pct": 60,
            "lead_quality": "fair",
            "is_authorized": True,
            "source_type": "mouser",
            "sub_offers": [
                {"source_type": "digikey", "unit_price": 0.55, "qty_available": 1000},
                {"source_type": "nexar", "unit_price": 0.48, "qty_available": 2000},
            ],
            "offer_count": 3,
            "sources_found": ["mouser", "digikey", "nexar"],
            "reason": "Multiple sources",
        },
        card_index=1,
        search_id="test-789",
    )
    assert "3 offers" in html
    assert "digikey" in html
    assert "0.5500" in html
    assert "2,000" in html


def test_shortlist_bar_template_renders():
    """shortlist_bar.html renders with Alpine.js directives."""
    html = _render_template("htmx/partials/search/shortlist_bar.html")
    assert "$store.shortlist" in html
    assert "Add to Requisition" in html


def test_search_run_empty_mpn_returns_error(client):
    """POST /v2/partials/search/run with empty MPN returns error message."""
    resp = client.post(
        "/v2/partials/search/run",
        data={"mpn": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Please enter a part number" in resp.text


def _assert_no_market_summary_strip(html):
    """The KPI tile strip lives ABOVE #search-results-cards, outside the filter swap
    target, so /v2/partials/search/filter (which swaps INTO that container) must return
    vendor cards only — never the strip, or a filter change would nest a second strip
    inside the card list."""
    assert "High confidence" not in html
    assert "Market summary" not in html
    assert 'role="group"' not in html
    assert _tile_values(html) == {}


def test_search_filter_reads_from_cache(client, db_session):
    """GET /v2/partials/search/filter returns re-rendered cards from cached results."""
    cached_results = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "score": 80,
            "is_authorized": True,
        },
    ]

    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/filter?search_id=test-123&confidence=high",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text
    _assert_no_market_summary_strip(resp.text)


def test_lead_detail_matches_vendor_with_suffix(client, db_session):
    """SEARCH-DETAILS-VENDORKEY: the market-row Details → passes the RAW vendor name
    (url-encoded). The server normalizes BOTH sides, so a name carrying a corporate
    suffix the normalizer strips (', Inc.') still matches its cached result instead of
    falling through to 'Lead not found'."""
    cached_results = [
        {
            "vendor_name": "Mouser Electronics, Inc.",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "sub_offers": [],
            "sources_found": ["nexar"],
            "score": 80,
            "is_authorized": True,
        },
    ]
    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail",
            params={"search_id": "sfx-1", "vendor_key": "Mouser Electronics, Inc."},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Mouser Electronics" in resp.text
    assert "Lead not found" not in resp.text


def test_lead_detail_unknown_vendor_returns_not_found(client, db_session):
    """A vendor_key with no cached match still returns the 'Lead not found' message."""
    cached_results = [{"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T"}]
    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail",
            params={"search_id": "sfx-2", "vendor_key": "Nobody Corp"},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Lead not found" in resp.text


def test_search_filter_expired_returns_message(client, db_session):
    """GET /v2/partials/search/filter with no cached data returns expiry message."""
    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=None,
    ):
        resp = client.get(
            "/v2/partials/search/filter?search_id=expired-123",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "expired" in resp.text.lower() or "search again" in resp.text.lower()


def test_search_filter_confidence_filters(client, db_session):
    """GET /v2/partials/search/filter with confidence=high filters out non-green
    results."""
    cached_results = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "score": 80,
            "is_authorized": True,
        },
        {
            "vendor_name": "Shady Broker",
            "mpn_matched": "LM317T",
            "unit_price": 0.20,
            "confidence_color": "red",
            "confidence_pct": 20,
            "lead_quality": "",
            "source_type": "brokerbin",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["brokerbin"],
            "score": 30,
            "is_authorized": False,
        },
    ]

    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/filter?search_id=test-123&confidence=high",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text
    assert "Shady Broker" not in resp.text
    _assert_no_market_summary_strip(resp.text)


# ── Add to Requisition tests ────────────────────────────────────────────


def test_add_to_requisition_creates_sightings(client, db_session):
    """POST /v2/partials/search/add-to-requisition creates Requirement + Sighting
    rows."""
    from app.models.sourcing import Requirement, Requisition, Sighting

    req = Requisition(name="Test Req", customer_name="Test Co")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={
            "requisition_id": req.id,
            "mpn": "LM317T",
            "items": [
                {
                    "vendor_name": "Arrow",
                    "mpn_matched": "LM317T",
                    "unit_price": 0.45,
                    "qty_available": 1000,
                    "source_type": "nexar",
                    "is_authorized": True,
                    "confidence": 0.8,
                    "score": 80,
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert "Added 1 result" in resp.text

    requirement = db_session.query(Requirement).filter_by(requisition_id=req.id, primary_mpn="LM317T").first()
    assert requirement is not None
    # Canonical key form (lowercase, separators stripped) — matches update_requirement
    # so part-history / material-card joins line up. Was the broken .upper() display form.
    assert requirement.normalized_mpn == "lm317t"

    sighting = db_session.query(Sighting).filter_by(requirement_id=requirement.id).first()
    assert sighting is not None
    assert sighting.vendor_name == "Arrow"
    assert float(sighting.unit_price) == 0.45


def test_add_to_requisition_reuses_existing_requirement(client, db_session):
    """Adding to a requisition with an existing Requirement reuses it."""
    from app.models.sourcing import Requirement, Requisition, Sighting

    req = Requisition(name="Existing Req", customer_name="Acme")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    # Pre-create a Requirement
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        normalized_mpn="LM317T",
        sourcing_status="open",
    )
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(requirement)

    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={
            "requisition_id": req.id,
            "mpn": "LM317T",
            "items": [{"vendor_name": "Mouser", "source_type": "mouser", "score": 70}],
        },
    )
    assert resp.status_code == 200

    # Should still be exactly one Requirement
    count = db_session.query(Requirement).filter_by(requisition_id=req.id, primary_mpn="LM317T").count()
    assert count == 1

    sighting = db_session.query(Sighting).filter_by(requirement_id=requirement.id).first()
    assert sighting is not None
    assert sighting.vendor_name == "Mouser"


def test_add_to_requisition_missing_fields(client):
    """POST with missing fields returns 400."""
    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={"requisition_id": None, "mpn": "", "items": []},
    )
    assert resp.status_code == 400
    assert "Missing required fields" in resp.text


def test_add_to_requisition_not_found(client):
    """POST with nonexistent requisition returns 404."""
    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={"requisition_id": 999999, "mpn": "LM317T", "items": [{"vendor_name": "X"}]},
    )
    assert resp.status_code == 404
    assert "Requisition not found" in resp.text


def test_requisition_picker_renders(client, db_session):
    """GET /v2/partials/search/requisition-picker returns the modal HTML."""
    from app.models.sourcing import Requisition

    req = Requisition(name="Pick Me", customer_name="TestCo")
    db_session.add(req)
    db_session.commit()

    resp = client.get(
        "/v2/partials/search/requisition-picker?mpn=LM317T&items=[]",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Pick Me" in resp.text
    assert "Add to Requisition" in resp.text


def test_lead_detail_reads_from_cache(client, db_session):
    """Lead detail route reads vendor data from Redis cache."""
    cached_results = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "reason": "Authorized distributor",
            "sub_offers": [
                {"unit_price": 0.48, "source_type": "digikey", "qty_available": 500},
            ],
            "offer_count": 2,
            "sources_found": ["nexar", "digikey"],
            "is_authorized": True,
            "qty_available": 1000,
        },
    ]

    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail?search_id=test-123&vendor_key=arrow",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text


def test_lead_detail_cache_miss_returns_not_found(client, db_session):
    """Lead detail route returns friendly message when cache is empty."""
    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=None,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail?search_id=test-123&vendor_key=arrow",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "not found" in resp.text.lower() or "search again" in resp.text.lower()


def test_lead_detail_cache_vendor_not_matched(client, db_session):
    """Lead detail returns not-found when vendor_key doesn't match any cached result."""
    cached_results = [
        {"vendor_name": "Mouser", "mpn_matched": "LM317T", "unit_price": 0.50},
    ]

    with patch(
        "app.routers.htmx.search_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail?search_id=test-123&vendor_key=nonexistent",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "not found" in resp.text.lower() or "search again" in resp.text.lower()


# ── Integration smoke test ────────────────────────────────────────────


def test_full_search_flow_smoke(client, db_session):
    """Smoke test: search form → shell → filter → add-to-req."""
    # 1. Submit search (returns shell with SSE connection)
    with patch("app.search_service.stream_search_mpn", new_callable=AsyncMock):
        resp = client.post(
            "/v2/partials/search/run",
            data={"mpn": "LM317T"},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "sse-connect" in resp.text

    # 2. Filter with cached results
    cached = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "score": 80,
            "is_authorized": True,
            "qty_available": 1000,
        },
    ]
    with patch("app.routers.htmx.search_views._get_cached_search_results", return_value=cached):
        resp = client.get(
            "/v2/partials/search/filter?search_id=test-123",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text


# ── Errored / non-ok source surfacing ──────────────────────────────────


class TestBuildConnectorsErroredBranch:
    """_build_connectors must exclude sources where ApiSource.status='error' (set by
    health_monitor) and surface them as 'error_skipped' in source_stats_map.

    Operator sees a distinct chip with actionable message.
    """

    def test_errored_source_excluded_with_error_skipped_status(self, db_session):
        """A source with status='error' is not instantiated; source_stats_map gets
        'error_skipped' with the operator-actionable message."""
        from app.models.config import ApiSource
        from app.search_service import _build_connectors

        # Insert an OEMSecrets source flipped to 'error' by health_monitor
        # (simulating the prior-error state). Pre-populate its credentials
        # so the only reason for exclusion is the error status.
        src = ApiSource(
            name="oemsecrets",
            display_name="OEMSecrets",
            category="api",
            source_type="search",
            status="error",
            is_active=True,
            credentials={"OEMSECRETS_API_KEY": "test-key"},
        )
        db_session.add(src)
        db_session.commit()

        # Stub credentials_batch to return a key for oemsecrets so the
        # exclusion is unambiguously due to the 'error' branch (not "no
        # creds").
        with patch(
            "app.search_service.get_credentials_batch",
            return_value={("oemsecrets", "OEMSECRETS_API_KEY"): "test-key"},
        ):
            connectors, source_stats_map, _ = _build_connectors(db_session)

        # OEMSecrets connector excluded
        from app.connectors.oemsecrets import OEMSecretsConnector

        assert not any(isinstance(c, OEMSecretsConnector) for c in connectors)
        # source_stats_map carries the error_skipped chip with operator
        # message
        stat = source_stats_map.get("oemsecrets", {})
        assert stat.get("status") == "error_skipped"
        msg = (stat.get("error") or "").lower()
        assert "rotate" in msg or "credentials" in msg or "auto-recover" in msg


class TestStreamSearchMpnNonOkChips:
    """stream_search_mpn must publish source-status SSE events for every non-ok entry in
    source_stats_map at search start.

    Without this, the operator never sees chips for excluded sources (error_skipped,
    disabled, skipped) — only sources that actually run emit events.
    """

    @pytest.mark.asyncio
    async def test_error_skipped_publishes_source_status_event(self, db_session):
        """An error_skipped source emits a source-status event so the chip renders.

        Verifies the contract's UI hop end-to-end.
        """
        from app.search_service import stream_search_mpn

        # source_stats_map populated by _build_connectors with an
        # error_skipped entry for oemsecrets — simulating health_monitor's
        # prior-error state.
        seeded_stats = {
            "oemsecrets": {
                "source": "oemsecrets",
                "status": "error_skipped",
                "error": "Skipped due to prior error — auto-recovers when next ping returns 200; rotate credentials if persistent",
                "results": 0,
                "ms": 0,
            },
        }

        published_events, mock_publish = _make_event_collector()

        # Need at least one connector to keep the function from short-circuiting
        # — otherwise it emits 'done' and returns before reaching the
        # full event flow. But the non-ok publish loop runs BEFORE the
        # short-circuit, so this also tests the no-connector case.
        fake_connector = MagicMock()
        fake_connector.source_name = "nexar"
        fake_connector.search = AsyncMock(return_value=[])

        with (
            patch("app.search_service.broker", create=True) as mock_broker,
            patch(
                "app.search_service._build_connectors",
                return_value=([fake_connector], seeded_stats, set()),
            ),
            patch("app.search_service.SessionLocal", lambda: db_session),
        ):
            mock_broker.publish = mock_publish
            await stream_search_mpn("test-search-id", "LM317T")

        # Find the source-status event for oemsecrets (the non-ok one)
        oem_status_events = [e for e in published_events if e["event"] == "source-status" and "oemsecrets" in e["data"]]
        assert len(oem_status_events) >= 1, (
            f"Expected oemsecrets source-status event, got: {[e['event'] for e in published_events]}"
        )
        oem_payload = json.loads(oem_status_events[0]["data"])
        assert oem_payload["status"] == "error_skipped"
        assert oem_payload["source"] == "oemsecrets"
        # Operator-actionable error message must be carried through to
        # the chip
        assert oem_payload.get("error")

    @pytest.mark.asyncio
    async def test_no_connectors_still_publishes_non_ok_chips(self, db_session):
        """Even when no connectors run (all skipped/disabled/errored), the non-ok chips
        must still publish before the 'done' event."""
        from app.search_service import stream_search_mpn

        seeded_stats = {
            "nexar": {
                "source": "nexar",
                "status": "skipped",
                "error": "No API key configured",
                "results": 0,
                "ms": 0,
            },
            "oemsecrets": {
                "source": "oemsecrets",
                "status": "error_skipped",
                "error": "Skipped due to prior error",
                "results": 0,
                "ms": 0,
            },
        }

        published_events, mock_publish = _make_event_collector()

        with (
            patch("app.search_service.broker", create=True) as mock_broker,
            patch(
                "app.search_service._build_connectors",
                return_value=([], seeded_stats, set()),
            ),
            patch("app.search_service.SessionLocal", lambda: db_session),
        ):
            mock_broker.publish = mock_publish
            await stream_search_mpn("test-search-id", "LM317T")

        status_events = [e for e in published_events if e["event"] == "source-status"]
        assert len(status_events) == 2
        sources_published = {json.loads(e["data"])["source"] for e in status_events}
        assert sources_published == {"nexar", "oemsecrets"}
        # 'done' should still fire after the non-ok chips
        assert any(e["event"] == "done" for e in published_events)

    @pytest.mark.asyncio
    async def test_ok_status_in_seeded_map_does_not_double_publish(self, db_session):
        """If source_stats_map already has an 'ok' entry (shouldn't happen in practice
        but defensive), the non-ok publish loop must skip it — that source's event will
        come from the actual run later."""
        from app.search_service import stream_search_mpn

        seeded_stats = {
            "nexar": {"source": "nexar", "status": "ok", "results": 0, "ms": 0, "error": None},
        }

        published_events, mock_publish = _make_event_collector()

        with (
            patch("app.search_service.broker", create=True) as mock_broker,
            patch(
                "app.search_service._build_connectors",
                return_value=([], seeded_stats, set()),
            ),
            patch("app.search_service.SessionLocal", lambda: db_session),
        ):
            mock_broker.publish = mock_publish
            await stream_search_mpn("test-search-id", "LM317T")

        # No source-status events expected because the only seeded entry
        # is 'ok' (skipped by the non-ok publish loop) and there are no
        # connectors to run.
        status_events = [e for e in published_events if e["event"] == "source-status"]
        assert len(status_events) == 0
