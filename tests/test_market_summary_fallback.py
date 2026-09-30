"""Tests for the market-summary fallback endpoint (lost SSE "summary" event recovery).

stream_search_mpn publishes the SSE "summary" event just before "done", and the SSE broker
has no replay, so on the fast shared-cache-hit branch the whole stream can finish before
the browser subscribes and #market-summary stays empty even though the Redis results
cache (search:{search_id}:results) was written. GET /v2/partials/search/summary renders
the same KPI tile strip from that cache, and results_shell.html calls it on htmx:sseClose
when #market-summary is still empty.

Covers: cache hit renders the six tiles with whole-set counts (rows shaped like
_score_raw_hit output) and matches the SSE renderer's output; cache miss / empty cache /
Redis error render an empty body; missing search_id is a 422; the results shell carries
the fallback wiring (#market-summary, sseClose handler, endpoint URL, data-search-id).

Called by: pytest
Depends on: app/routers/htmx/search_views.py (search_summary), app/search_service.py
    (compute_market_summary, _render_market_summary_html),
    app/templates/htmx/partials/search/market_summary.html,
    app/templates/htmx/partials/search/results_shell.html, tests/conftest.py (client).
"""

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

SUMMARY_URL = "/v2/partials/search/summary"
SEARCH_ID = "sid-summary-fallback-1"
TILE_LABELS = ("Vendors", "Offers", "Authorized", "High confidence", "Best price", "Total stock")


def _rows() -> list[dict]:
    """Market rows shaped like production _score_raw_hit output (as cached in Redis).

    Expected whole-set summary: 3 vendors, 4 offers (A has 2 sub-offers, B and C one
    each), 1 authorized, 2 high confidence (A and C are green), best price $0.7900 (A's
    cheaper sub-offer, not the row head), total stock 1,250 (C has no known qty).
    """
    return [
        {
            "vendor_name": "Fallback Vendor A",
            "mpn_matched": "LM317T",
            "manufacturer": "Texas Instruments",
            "unit_price": 0.84,
            "qty_available": 1000,
            "is_authorized": True,
            "confidence": 0.91,
            "confidence_pct": 91,
            "confidence_color": "green",
            "source_type": "digikey",
            "sources_found": ["digikey"],
            "sub_offers": [
                {"unit_price": 0.79, "qty_available": 500},
                {"unit_price": 0.84, "qty_available": 500},
            ],
        },
        {
            "vendor_name": "Fallback Vendor B",
            "mpn_matched": "LM317T",
            "manufacturer": "Texas Instruments",
            "unit_price": 1.10,
            "qty_available": 250,
            "is_authorized": False,
            "confidence": 0.6,
            "confidence_pct": 60,
            "confidence_color": "amber",
            "source_type": "brokerbin",
            "sources_found": ["brokerbin"],
        },
        {
            "vendor_name": "Fallback Vendor C",
            "mpn_matched": "LM317T",
            "manufacturer": None,
            "unit_price": None,
            "qty_available": None,
            "is_authorized": False,
            "confidence": 0.8,
            "confidence_pct": 80,
            "confidence_color": "green",
            "source_type": "email",
            "sources_found": ["email"],
        },
    ]


def _redis_with(rows: list[dict] | None) -> MagicMock:
    """MagicMock Redis client whose ``search:{id}:results`` key holds ``rows`` as
    JSON."""
    rc = MagicMock()
    rc.get.side_effect = lambda k: json.dumps(rows) if (rows is not None and k.endswith(":results")) else None
    return rc


def _tiles(html: str) -> dict[str, str]:
    """Map tile label -> displayed value via the tiles' title attributes."""
    return dict(re.findall(r'title="([^":]+): ([^"]*)"', html))


# ── (a) cache hit ──────────────────────────────────────────────────────────


def test_cache_hit_renders_six_tiles_with_expected_counts(client):
    """Cached rows -> 200 and the six KPI tiles render with the whole-set counts."""
    rc = _redis_with(_rows())
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get(SUMMARY_URL, params={"search_id": SEARCH_ID})
    assert resp.status_code == 200
    body = resp.text
    for label in TILE_LABELS:
        assert label in body
    tiles = _tiles(body)
    assert tiles == {
        "Vendors": "3",
        "Offers": "4",
        "Authorized": "1",
        "High confidence": "2",
        "Best price": "$0.7900",
        "Total stock": "1,250",
    }
    rc.get.assert_called_with(f"search:{SEARCH_ID}:results")


def test_cache_hit_matches_sse_summary_event_body(client):
    """The fallback renders exactly what the lost SSE "summary" event would have
    carried."""
    from app.search_service import _render_market_summary_html

    rows = _rows()
    with patch("app.search_service._get_search_redis", return_value=_redis_with(rows)):
        resp = client.get(SUMMARY_URL, params={"search_id": SEARCH_ID})
    assert resp.status_code == 200
    assert resp.text.strip() == _render_market_summary_html(rows).strip()


# ── (b) cache miss ─────────────────────────────────────────────────────────


def test_cache_miss_no_redis_returns_empty_body(client):
    """TESTING -> no Redis client at all: 200 with an empty body, no tiles."""
    resp = client.get(SUMMARY_URL, params={"search_id": "unknown-search-id"})
    assert resp.status_code == 200
    assert resp.text.strip() == ""
    assert "Vendors" not in resp.text


def test_unknown_search_id_returns_empty_body(client):
    """Redis is up but has no entry for this search_id (expired/unknown): empty 200."""
    rc = _redis_with(None)
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get(SUMMARY_URL, params={"search_id": "expired-search-id"})
    assert resp.status_code == 200
    assert resp.text.strip() == ""
    assert "Vendors" not in resp.text
    rc.get.assert_called_with("search:expired-search-id:results")


def test_empty_cached_result_set_returns_empty_body(client):
    """A cached empty result list (zero vendors) renders nothing — the empty state owns
    it."""
    with patch("app.search_service._get_search_redis", return_value=_redis_with([])):
        resp = client.get(SUMMARY_URL, params={"search_id": SEARCH_ID})
    assert resp.status_code == 200
    assert resp.text.strip() == ""
    assert "Vendors" not in resp.text


def test_redis_error_returns_empty_body(client):
    """A Redis read failure is swallowed by the cache helper: empty 200, never a 500."""
    rc = MagicMock()
    rc.get.side_effect = RuntimeError("redis down")
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get(SUMMARY_URL, params={"search_id": SEARCH_ID})
    assert resp.status_code == 200
    assert resp.text.strip() == ""


def test_missing_search_id_is_rejected(client):
    """search_id is a required query param."""
    resp = client.get(SUMMARY_URL)
    assert resp.status_code == 422


# ── (c) results shell wiring ───────────────────────────────────────────────


def test_results_shell_carries_fallback_wiring(client):
    """POST search/run -> shell with #market-summary, data-search-id and an sseClose
    handler that calls the fallback endpoint for that search_id."""
    with patch("app.search_service.stream_search_mpn", new_callable=AsyncMock):
        resp = client.post(
            "/v2/partials/search/run",
            data={"mpn": "LM317T"},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    html = resp.text

    assert 'id="market-summary"' in html
    assert 'sse-swap="summary"' in html

    match = re.search(r'id="search-results-wrapper"\s+data-search-id="([^"]+)"', html)
    assert match, "wrapper div must carry data-search-id"
    assert match.group(1) in html.split("sse-connect=", 1)[1]  # same id the SSE stream uses

    close_at = html.index("htmx:sseClose")
    handler = html[close_at:]
    assert "/v2/partials/search/summary?search_id=" in handler
    assert "encodeURIComponent(wrapper.dataset.searchId)" in handler
    assert "getElementById('market-summary')" in handler
    assert "target: '#market-summary'" in handler
