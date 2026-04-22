"""Phase 1 smoke gate — proves _fetch_fresh lacks an outer timeout wrapper.

What it does: stubs the connector list with one fast connector and one that
hangs for 45s, then asserts _fetch_fresh(...) returns within a 15s budget.

What calls it: pytest regression suite (`TESTING=1 pytest tests/test_fetch_fresh_timeout.py -v`).

What it depends on: app.search_service._fetch_fresh, tests/conftest.py db_session fixture.

Status: xfail(strict=True) until the async gather at app/search_service.py:931 is
wrapped with an outer asyncio.wait_for (or equivalent per-task deadline). Once
wrapped, this test will pass and the xfail marker must be removed.
"""

import asyncio

import pytest

from app import search_service


class _FastFakeConnector:
    """Stub connector that returns instantly with one synthetic hit."""

    async def search(self, pn: str) -> list[dict]:
        return [
            {
                "mpn": pn,
                "vendor": "fast-fake",
                "qty": 1,
                "price": 1.00,
                "source": "fast_fake",
            }
        ]


class _SlowFakeConnector:
    """Stub connector that simulates an upstream hang (45s)."""

    async def search(self, pn: str) -> list[dict]:
        await asyncio.sleep(45)
        return []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known: app/search_service.py:931 uses bare asyncio.gather with no outer "
        "wait_for. Remove this xfail once the gather is bounded by a deadline "
        "(e.g. asyncio.wait_for(gather, timeout=settings.search_total_timeout_s))."
    ),
)
async def test_fetch_fresh_returns_within_budget_when_one_connector_hangs(monkeypatch, db_session):
    """A single hung connector must not block the whole orchestrator.

    Budget: 15s total wall time for _fetch_fresh. On fixed HEAD the fast
    connector's result returns and the slow connector is recorded as
    error/timeout in source_stats.
    """
    # Register synthetic connectors in the source-name map so the stats-write
    # path at line 933+ has something to look up (and no-op it against an empty
    # api_sources table in the SQLite test DB).
    monkeypatch.setitem(search_service._CONNECTOR_SOURCE_MAP, "_FastFakeConnector", "fast_fake")
    monkeypatch.setitem(search_service._CONNECTOR_SOURCE_MAP, "_SlowFakeConnector", "slow_fake")

    def _fake_build(_db):
        return ([_FastFakeConnector(), _SlowFakeConnector()], {}, set())

    monkeypatch.setattr(search_service, "_build_connectors", _fake_build)
    monkeypatch.setattr(search_service, "_get_search_cache", lambda _k: None)
    monkeypatch.setattr(search_service, "_set_search_cache", lambda *_a, **_kw: None)

    loop = asyncio.get_event_loop()
    start = loop.time()
    try:
        results, stats = await asyncio.wait_for(
            search_service._fetch_fresh(["FAKE-MPN-1"], db_session),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        pytest.fail(
            "_fetch_fresh did not return within 15s — the async gather at "
            "app/search_service.py:931 has no outer timeout wrapper, so one "
            "hung connector blocks the whole orchestrator until Caddy's 30s "
            "lb_try_duration cuts the HTTP connection."
        )

    elapsed = loop.time() - start
    assert elapsed < 15.0, f"_fetch_fresh took {elapsed:.2f}s (budget 15s)"

    # Fast connector's result must be present — graceful degradation works.
    assert len(results) >= 1, "no results from fast connector despite it returning"
    assert any(r.get("vendor") == "fast-fake" for r in results)

    # Slow connector must be recorded as error/timeout in source_stats, not silently dropped.
    slow_stat = next((s for s in stats if s.get("source") == "slow_fake"), None)
    assert slow_stat is not None, "slow connector missing from source_stats"
    assert slow_stat.get("status") in {"error", "timeout"} or slow_stat.get("error"), (
        f"slow connector not marked as failed: {slow_stat!r}"
    )
