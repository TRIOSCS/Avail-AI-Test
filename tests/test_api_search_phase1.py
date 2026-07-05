"""Phase 1 API-search core-hardening regression suite (audit 2026-07-03, "Phase 1").

One consolidated file guarding the five small/high-confidence fixes that landed
together, each asserted end-to-end at the connector / search-path seam:

  1. Price-break ``min()`` must survive a PRESENT-but-null break quantity. A supplier
     may return ``{"Quantity": null}``; ``dict.get(k, default)`` returns the default
     only on a MISSING key, so an explicit null yields ``None`` and ``min(..., key=...)``
     then does ``None < int`` → ``TypeError`` → the WHOLE part number errors. The
     parsers coalesce the null quantity to a large sentinel so a null row sorts last and
     the PN still resolves (digikey, mouser, + Nexar GraphQL/REST v4 in sources.py).
  2. Hunter.io ``r.json()`` on a 200 with a non-JSON body must degrade to the empty
     shape, not raise ``ValueError`` into the enrichment caller (as every sibling does).
  3. The DigiKey/eBay/Nexar OAuth bearer is cached PROCESS-WIDE (keyed by
     ``(class, client_id)`` + expiry, single-flight ``asyncio.Lock``), so sequential
     searches reuse one token, an expired entry forces a re-mint, and a concurrent burst
     collapses to a SINGLE token POST instead of stampeding the auth endpoint.
  4. The sync Redis GET/SETEX on the async search path is dispatched off the event loop
     via ``asyncio.to_thread`` so a slow/unreachable Redis cannot stall the single loop.
  5. Element14 fires exactly ONE call per PN — the keyword-search fallback on an
     exact-match miss was dropped (it doubled 403-inducing call volume for catalog noise
     the relevance guard discards anyway).

Deeper per-fix coverage lives in the dedicated files (test_connector_price_breaks.py,
test_hunter_connector.py, test_connectors.py::TestOAuthTokenCache,
test_element14_connector.py); this suite is the consolidated Phase-1 contract guard and
the sole home of the Redis-off-the-loop assertion. Conftest's autouse
``_clear_connector_token_cache`` empties the OAuth cache around each test.

Called by: pytest
Depends on: app.connectors.{digikey,mouser,hunter,element14,sources}, app.search_service
"""

import asyncio
import inspect
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ───────────────────────────────────────────────────────────────────────────
#  Fix 1 — price-break min() survives a present-but-null break quantity
# ───────────────────────────────────────────────────────────────────────────


class TestPriceBreakNullQuantity:
    def test_digikey_null_break_quantity_resolves(self):
        """A DigiKey StandardPricing row with an explicit null BreakQuantity no longer
        crashes the parse — the valid break still wins and the PN resolves."""
        from app.connectors.digikey import DigiKeyConnector

        c = DigiKeyConnector(client_id="id", client_secret="secret")
        data = {
            "Products": [
                {
                    "ManufacturerPartNumber": "LM317T",
                    "Manufacturer": {"Name": "TI"},
                    "DigiKeyPartNumber": "DK1",
                    "QuantityAvailable": 100,
                    "StandardPricing": [
                        {"BreakQuantity": None, "UnitPrice": 9.99},  # the crash trigger
                        {"BreakQuantity": 1, "UnitPrice": 0.75},
                    ],
                    "ProductUrl": "",
                    "Description": {"DetailedDescription": "x"},
                }
            ]
        }
        results = c._parse(data, "LM317T")  # must not raise TypeError
        assert len(results) == 1
        assert results[0]["unit_price"] == 0.75  # the real lowest-qty break still wins

    def test_mouser_null_break_quantity_resolves(self):
        """Mirror on Mouser's PriceBreaks shape — a null Quantity sorts last, not
        crash."""
        from app.connectors.mouser import MouserConnector

        c = MouserConnector(api_key="key")
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "LM317T",
                        "Manufacturer": "TI",
                        "MouserPartNumber": "M-1",
                        "Availability": "100 In Stock",
                        "PriceBreaks": [
                            {"Quantity": None, "Price": "$9.99"},
                            {"Quantity": 1, "Price": "$0.89"},
                        ],
                        "ProductDetailUrl": "",
                        "Description": "",
                    }
                ]
            }
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        assert results[0]["unit_price"] == 0.89


# ───────────────────────────────────────────────────────────────────────────
#  Fix 2 — Hunter.io non-JSON 200 degrades instead of raising
# ───────────────────────────────────────────────────────────────────────────


class TestHunterNonJsonDegrades:
    @pytest.mark.asyncio
    async def test_domain_search_non_json_200_returns_empty(self):
        """A 200 whose body isn't JSON (r.json() → ValueError) must degrade to [] and
        not escape into the enrichment caller."""
        from app.connectors.hunter import HunterConnector

        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("No JSON object could be decoded")
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            result = await HunterConnector("key").domain_search("example.com")

        assert result == []


# ───────────────────────────────────────────────────────────────────────────
#  Fix 3 — OAuth token cache is alive ACROSS searches (module-level + Lock)
# ───────────────────────────────────────────────────────────────────────────


def _digikey_token_response(token: str = "tok", expires_in: int = 600) -> MagicMock:
    """A stand-in DigiKey token-endpoint response with a working
    json()/raise_for_status()."""
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"access_token": token, "expires_in": expires_in}
    return r


class TestOAuthTokenCacheAcrossSearches:
    @pytest.mark.asyncio
    async def test_sequential_get_token_reuses_cached_bearer(self):
        """Two sequential _get_token() calls (a fresh connector per search rebuilds the
        instance, but the cache is process-wide) mint the bearer exactly ONCE."""
        from app.connectors.digikey import DigiKeyConnector

        c = DigiKeyConnector(client_id="dk-id", client_secret="secret")
        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(return_value=_digikey_token_response("tok-A"))
            first = await c._get_token()
            second = await c._get_token()

        assert first == second == "tok-A"
        assert mock_http.post.call_count == 1  # second call served from the module cache

    @pytest.mark.asyncio
    async def test_expiry_forces_reauth(self):
        """An expired cache entry (expires_at in the past) forces a fresh mint — the
        stale bearer is never served."""
        from app.connectors.digikey import DigiKeyConnector
        from app.connectors.sources import _token_cache

        c = DigiKeyConnector(client_id="dk-id", client_secret="secret")
        _token_cache[c._token_cache_key()] = ("stale-bearer", time.monotonic() - 1.0)
        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(return_value=_digikey_token_response("tok-fresh"))
            token = await c._get_token()

        assert token == "tok-fresh"  # re-minted, not the stale entry
        assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_get_token_single_flight(self):
        """A concurrent burst of _get_token() collapses to a SINGLE mint POST — the per-
        key asyncio.Lock kills the intra-search auth herd."""
        from app.connectors.digikey import DigiKeyConnector

        c = DigiKeyConnector(client_id="dk-id", client_secret="secret")

        async def _slow_post(*args, **kwargs):
            await asyncio.sleep(0.02)  # widen the race window so the burst truly overlaps
            return _digikey_token_response("tok-burst")

        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(side_effect=_slow_post)
            tokens = await asyncio.gather(*(c._get_token() for _ in range(20)))

        assert set(tokens) == {"tok-burst"}
        assert mock_http.post.call_count == 1  # the lock collapsed the herd into one POST


# ───────────────────────────────────────────────────────────────────────────
#  Fix 4 — sync Redis GET/SETEX runs off the event loop (asyncio.to_thread)
# ───────────────────────────────────────────────────────────────────────────


class TestRedisOffEventLoop:
    def test_search_cache_helpers_are_sync(self):
        """The Redis helpers are plain SYNC functions — so the async path MUST offload
        them (a bare sync call on the loop is exactly the blocking bug the fix
        removed)."""
        from app import search_service

        assert not inspect.iscoroutinefunction(search_service._get_search_cache)
        assert not inspect.iscoroutinefunction(search_service._set_search_cache)

    def test_fetch_fresh_offloads_both_redis_calls_to_thread(self):
        """_fetch_fresh dispatches BOTH the Redis GET and SETEX via asyncio.to_thread —
        structural guard against reverting to a loop-blocking bare call."""
        from app import search_service

        src = inspect.getsource(search_service._fetch_fresh)
        assert "asyncio.to_thread(_get_search_cache" in src
        assert "asyncio.to_thread(_set_search_cache" in src

    @pytest.mark.asyncio
    async def test_blocking_redis_does_not_stall_the_loop(self, monkeypatch):
        """Behavioral proof: a slow sync Redis GET dispatched via asyncio.to_thread keeps
        the loop free — a concurrent ticker keeps advancing while the GET blocks."""
        from app import search_service

        class _BlockingRedis:
            def get(self, key):
                time.sleep(0.15)  # simulate a slow / hung Redis GET
                return None

        monkeypatch.setattr(search_service, "_get_search_redis", lambda: _BlockingRedis())

        ticks = 0

        async def _ticker():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.01)
                ticks += 1

        # Offload the blocking sync call exactly as _fetch_fresh does, alongside the ticker.
        result, _ = await asyncio.gather(
            asyncio.to_thread(search_service._get_search_cache, "k"),
            _ticker(),
        )

        assert result is None  # the blocking GET returned (miss), no exception escaped
        assert ticks == 10  # the loop kept running the ticker while the GET was in flight


# ───────────────────────────────────────────────────────────────────────────
#  Fix 5 — Element14 fires ONE exact-match call per PN (no keyword fallback)
# ───────────────────────────────────────────────────────────────────────────


class TestElement14SingleCall:
    @pytest.mark.asyncio
    async def test_exact_miss_fires_single_exact_call_no_keyword_fallback(self, monkeypatch):
        """A 0-result exact-MPN miss on a full MPN fires exactly ONE call, and that call
        uses the exact ``manuPartNum:`` term — the keyword-search fallback was
        dropped."""
        from app.connectors.element14 import Element14Connector

        c = Element14Connector(api_key="key")
        terms: list[str] = []
        fake_request = httpx.Request("GET", Element14Connector.SEARCH_URL)

        async def _mock_get(*args, **kwargs):
            terms.append(kwargs["params"].get("term"))
            return httpx.Response(
                200,
                request=fake_request,
                json={"manufacturerPartNumberSearchReturn": {"products": []}},
            )

        monkeypatch.setattr(
            "app.connectors.element14.http",
            type("FakeHTTP", (), {"get": _mock_get})(),
        )
        results = await c._do_search("TPS65217CRSLR")

        assert results == []
        assert len(terms) == 1  # no second (keyword-fallback) call
        assert terms[0] == "manuPartNum:TPS65217CRSLR"  # only the exact-match path ran
