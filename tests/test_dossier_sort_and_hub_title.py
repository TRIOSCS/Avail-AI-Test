"""test_dossier_sort_and_hub_title.py — two small audit fixes.

Fix 1 (MEDIUM): the Search report's "Posting now" section carries a sort control wired to
GET /v2/partials/search/filter, so buyers can reorder the cached postings. Covers:
  - the live section renders the control (form → /v2/partials/search/filter, targeting
    #live-rows) seeded with the cached search id and the endpoint's REAL sort options
    (best|cheapest|stock);
  - hitting the endpoint with a sort param reorders the re-rendered rows;
  - a confidence / source filter param drops the non-matching rows;
  - several comma-separated search ids (one per part number) merge into one row set.

Fix 2 (LOW, post-retirement): the old personal Buy Plans hub URL now 308s to the
Approvals Workspace, so its retired "Buy Plans — AvailAI" <title> never renders — the
workspace's own title takes over after the redirect.

Called by: pytest
Depends on: conftest (client fixture, authed as test_user with BUY_PLANS access),
            app.routers.part_dossier.dossier_market, app.routers.htmx.search_views.search_filter,
            app.templates/htmx/partials/search/report_live.html,
            app.routers.htmx.buy_plans (retired-hub redirect).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

# ── Cached market rows: a pricey/high-confidence brokerbin offer and a cheap/
#    high-stock/amber nexar offer, chosen so best/cheapest/stock and the
#    confidence/source filters each produce a distinguishable result set. ──
_EXPENSIVE = {
    "vendor_name": "Expensive Co",
    "mpn_matched": "LM317T",
    "manufacturer": "TI",
    "unit_price": 5.00,
    "qty_available": 10,
    "confidence_color": "green",
    "confidence_pct": 95,
    "score": 10,
    "source_type": "brokerbin",
    "sources_found": ["brokerbin"],
}
_CHEAP = {
    "vendor_name": "Cheap Co",
    "mpn_matched": "LM317T",
    "manufacturer": "TI",
    "unit_price": 1.00,
    "qty_available": 500,
    "confidence_color": "amber",
    "confidence_pct": 60,
    "score": 1,
    "source_type": "nexar",
    "sources_found": ["nexar"],
}
_ROWS = [_EXPENSIVE, _CHEAP]
_SID = "sid-sortfilter-1"


def _redis_with_rows(rows: list[dict], sid: str = _SID) -> MagicMock:
    """MagicMock Redis whose :latest pointer → sid and :results → json(rows)."""
    rc = MagicMock()
    rc.get.side_effect = lambda k: (
        sid if k.endswith(":latest") else (json.dumps(rows) if k.endswith(":results") else None)
    )
    return rc


# ══════════════════════════════════════════════════════════════════════════
# Fix 1 — dossier market sort/filter bar + the (formerly orphaned) endpoint
# ══════════════════════════════════════════════════════════════════════════


def test_dossier_market_renders_sort_control(client: TestClient):
    """Cache-hit live section renders the sort control wired to
    /v2/partials/search/filter, targeting #live-rows, seeded with the cached search
    id."""
    rc = _redis_with_rows(_ROWS)
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    body = resp.text

    # The control is a form driving the filter endpoint into the rows container.
    assert 'hx-get="/v2/partials/search/filter"' in body
    assert 'hx-target="#live-rows"' in body
    assert 'name="search_id"' in body
    # The cached run's id seeds the section state the hidden input is bound to.
    assert f'liveSection(0, ["{_SID}"])' in body

    # Real sort options (best|cheapest|stock).
    assert 'name="sort"' in body
    assert 'value="best"' in body
    assert 'value="cheapest"' in body
    assert 'value="stock"' in body

    # Both cached rows render in the table.
    assert "Expensive Co" in body and "Cheap Co" in body


def test_filter_endpoint_merges_several_search_ids(client: TestClient):
    """One id per part number: the endpoint merges their cached rows (sorted together)."""
    rows_by_id = {"sid-a": [_EXPENSIVE], "sid-b": [_CHEAP]}

    def _lookup(sid):
        return rows_by_id.get(sid)

    with patch("app.routers.htmx.search_views._get_cached_search_results", side_effect=_lookup):
        resp = client.get("/v2/partials/search/filter", params={"search_id": "sid-a,sid-b", "sort": "cheapest"})
    assert resp.status_code == 200
    assert resp.text.index("Cheap Co") < resp.text.index("Expensive Co")
    # Each row keeps the id of the run it came from (its detail lookup needs it).
    assert 'id="live-sid-b-cheap-co"' in resp.text
    assert 'id="live-sid-a-expensive-co"' in resp.text


def test_filter_endpoint_sort_cheapest_reorders_cards(client: TestClient):
    """The default (best) order leads with the high-confidence offer; sort=cheapest
    flips it so the cheapest offer's card renders first."""
    rc = _redis_with_rows(_ROWS)
    with patch("app.search_service._get_search_redis", return_value=rc):
        best = client.get("/v2/partials/search/filter", params={"search_id": _SID, "sort": "best"})
        cheapest = client.get("/v2/partials/search/filter", params={"search_id": _SID, "sort": "cheapest"})

    assert best.status_code == 200 and cheapest.status_code == 200
    # best: higher confidence (95 vs 60) first.
    assert best.text.index("Expensive Co") < best.text.index("Cheap Co")
    # cheapest: lower unit_price ($1 vs $5) first — reordered.
    assert cheapest.text.index("Cheap Co") < cheapest.text.index("Expensive Co")


def test_filter_endpoint_sort_stock_orders_by_qty(client: TestClient):
    """Sort=stock leads with the highest-quantity offer."""
    rc = _redis_with_rows(_ROWS)
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get("/v2/partials/search/filter", params={"search_id": _SID, "sort": "stock"})
    assert resp.status_code == 200
    # Cheap Co has 500 available vs Expensive Co's 10.
    assert resp.text.index("Cheap Co") < resp.text.index("Expensive Co")


def test_filter_endpoint_confidence_filters_out_non_matching(client: TestClient):
    """Confidence=high keeps only green-band offers (drops the amber one)."""
    rc = _redis_with_rows(_ROWS)
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get("/v2/partials/search/filter", params={"search_id": _SID, "confidence": "high"})
    assert resp.status_code == 200
    assert "Expensive Co" in resp.text  # green
    assert "Cheap Co" not in resp.text  # amber → filtered out


def test_filter_endpoint_source_filters_by_source(client: TestClient):
    """Source=nexar keeps only offers whose sources_found includes nexar."""
    rc = _redis_with_rows(_ROWS)
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get("/v2/partials/search/filter", params={"search_id": _SID, "source": "nexar"})
    assert resp.status_code == 200
    assert "Cheap Co" in resp.text  # sources_found = [nexar]
    assert "Expensive Co" not in resp.text  # sources_found = [brokerbin]


# ══════════════════════════════════════════════════════════════════════════
# Fix 2 — the retired Buy Plans hub URL lands on the Approvals Workspace title
# ══════════════════════════════════════════════════════════════════════════


def test_buy_plans_hub_url_lands_on_approvals_title(client: TestClient):
    """The hub retired (spec §11.1): its partial 308s to the workspace shell, whose own
    <title> takes over (the hub's "Buy Plans — AvailAI" title is gone)."""
    resp = client.get("/v2/partials/buy-plans", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.text
    assert "<title" in body
    assert "Buy Plans — AvailAI" not in body
