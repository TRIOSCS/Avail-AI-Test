"""tests/test_circuit_breaker.py — Tests for circuit breaker on connectors.

Covers: breaker creation, state transitions (closed → open → half-open),
and that open breakers skip API calls gracefully.

Called by: pytest
Depends on: app.connectors.sources (BaseConnector, CircuitBreaker, get_breaker)
"""

import asyncio

import pytest

from app.connectors.sources import BaseConnector, CircuitBreaker, _breakers, get_breaker


class _FakeConnector(BaseConnector):
    """Minimal connector for testing breaker behavior."""

    def __init__(self, fail=False):
        # Clear any stale breaker for this class
        _breakers.pop("_FakeConnector", None)
        super().__init__(timeout=1.0, max_retries=0)
        self._fail = fail
        self.call_count = 0

    async def _do_search(self, part_number: str) -> list[dict]:
        self.call_count += 1
        if self._fail:
            raise ConnectionError("API down")
        return [{"vendor_name": "Test", "mpn_matched": part_number}]


@pytest.fixture(autouse=True)
def _clean_breakers():
    """Reset breaker state between tests."""
    _breakers.pop("_FakeConnector", None)
    yield
    _breakers.pop("_FakeConnector", None)


def _trip_breaker(conn: "_FakeConnector", times: int = 5) -> None:
    """Drive `times` failing searches so the connector's breaker opens."""
    for _ in range(times):
        try:
            asyncio.run(conn.search("LM317T"))
        except ConnectionError:
            pass


# ── CircuitBreaker unit tests ─────────────────────────────────────────


def test_breaker_initial_state():
    """New breaker starts closed."""
    b = CircuitBreaker(name="test")
    assert b.current_state == "closed"


def test_breaker_stays_closed_on_success():
    """record_success keeps state closed."""
    b = CircuitBreaker(name="test")
    b.record_success()
    assert b.current_state == "closed"


def test_breaker_opens_after_enough_failures():
    """After fail_max failures, breaker opens."""
    b = CircuitBreaker(name="test", fail_max=3)
    for _ in range(3):
        b.record_failure()
    assert b.current_state == "open"


def test_breaker_half_open_after_timeout(monkeypatch: pytest.MonkeyPatch):
    """After reset_timeout elapses, breaker transitions to half_open.

    Uses a fake ``time.monotonic`` clock (advanced explicitly, no real ``sleep``) so
    the boundary is exact and deterministic under xdist parallelism — the previous
    version's real `sleep(0.15)` against a 0.1s timeout left only a 50ms margin,
    which xdist scheduling jitter could blow through and flake the test.
    """
    fake_now = 1_000.0

    def _fake_monotonic() -> float:
        return fake_now

    monkeypatch.setattr("app.connectors.sources.time.monotonic", _fake_monotonic)

    b = CircuitBreaker(name="test", fail_max=1, reset_timeout=0.1)
    b.record_failure()
    assert b.current_state == "open"

    # Just under the timeout: still open (boundary condition, not just "eventually").
    fake_now += 0.05
    assert b.current_state == "open"

    # Past the timeout: half_open.
    fake_now += 0.06  # total elapsed 0.11s > reset_timeout=0.1s
    assert b.current_state == "half_open"


def test_breaker_resets_on_success():
    """record_success resets fail count and closes breaker."""
    b = CircuitBreaker(name="test", fail_max=5)
    for _ in range(3):
        b.record_failure()
    assert b.current_state == "closed"  # still below threshold
    b.record_success()
    assert b._fail_count == 0


def test_get_breaker_creates_singleton():
    """get_breaker returns the same instance for the same name."""
    _breakers.pop("TestConn", None)
    b1 = get_breaker("TestConn")
    b2 = get_breaker("TestConn")
    assert b1 is b2
    _breakers.pop("TestConn", None)


def test_breaker_defaults():
    """Breaker is configured with 5 failures and 60s reset."""
    b = CircuitBreaker(name="defaults")
    assert b.fail_max == 5
    assert b.reset_timeout == 60


# ── Integration with BaseConnector ────────────────────────────────────


def test_connector_success_keeps_breaker_closed():
    """Successful connector call keeps breaker closed."""
    conn = _FakeConnector(fail=False)
    result = asyncio.run(conn.search("LM317T"))
    assert len(result) == 1
    assert conn._breaker.current_state == "closed"


def test_connector_opens_breaker_after_failures():
    """After 5 consecutive failures, connector breaker opens."""
    conn = _FakeConnector(fail=True)
    _trip_breaker(conn)
    assert conn._breaker.current_state == "open"


def test_open_breaker_raises_without_calling():
    """When breaker is open, search raises ConnectorError without calling _do_search.

    Was: returned [] (silent failure that masked the contract —
    health_monitor saw success and flipped status back to 'live'). See
    docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
    """
    from app.connectors.errors import ConnectorError

    conn = _FakeConnector(fail=True)
    # Trip the breaker (fail_max=5, max_retries=0 → 5 search calls)
    _trip_breaker(conn)
    assert conn._breaker.current_state == "open"

    # Reset call count and stop failing
    conn.call_count = 0
    conn._fail = False

    with pytest.raises(ConnectorError, match="circuit breaker open"):
        asyncio.run(conn.search("LM317T"))
    assert conn.call_count == 0  # _do_search was never called


# ── Health-probe breaker bypass ───────────────────────────────────────
# A breaker that tripped during a user search is transient; a health/Test probe must
# measure GENUINE upstream health, not the in-process breaker state — otherwise one
# flaky search flips api_sources.status to a 15-min ERROR exclusion.


def test_health_probe_bypasses_open_breaker_and_clears_it():
    """health_probe runs the real upstream even with the breaker open; a success clears
    the transient trip."""
    conn = _FakeConnector(fail=True)
    _trip_breaker(conn)
    assert conn._breaker.current_state == "open"

    # Upstream is actually healthy now — the probe must hit it (bypassing the breaker)
    # and the success must reset the breaker.
    conn._fail = False
    conn.call_count = 0
    result = asyncio.run(conn.health_probe("LM317T"))
    assert len(result) == 1
    assert conn.call_count == 1  # _do_search actually ran (short-circuit bypassed)
    assert conn._breaker.current_state == "closed"  # transient trip cleared


def test_health_probe_still_raises_on_genuine_failure():
    """A truly-down upstream still fails through health_probe and keeps the breaker open
    — real error exclusion is preserved."""
    conn = _FakeConnector(fail=True)
    _trip_breaker(conn)
    assert conn._breaker.current_state == "open"

    conn.call_count = 0  # upstream still failing
    with pytest.raises(ConnectionError):
        asyncio.run(conn.health_probe("LM317T"))
    assert conn.call_count == 1  # the real upstream WAS attempted
    assert conn._breaker.current_state == "open"  # record_failure re-opens


def test_run_health_probe_bypasses_breaker_for_baseconnector():
    """The run_health_probe seam routes BaseConnectors through health_probe (bypass)."""
    from app.connectors.sources import run_health_probe

    conn = _FakeConnector(fail=True)
    _trip_breaker(conn)
    assert conn._breaker.current_state == "open"

    conn._fail = False
    conn.call_count = 0
    result = asyncio.run(run_health_probe(conn, "LM317T"))
    assert len(result) == 1
    assert conn.call_count == 1
    assert conn._breaker.current_state == "closed"


def test_run_health_probe_falls_back_to_search_for_non_baseconnector():
    """Keyless test connectors (no breaker) fall back to plain search."""
    from app.connectors.sources import run_health_probe

    class _Keyless:
        def __init__(self):
            self.searched = None

        async def search(self, mpn):
            self.searched = mpn
            return [{"vendor_name": "X"}]

    k = _Keyless()
    result = asyncio.run(run_health_probe(k, "LM317T"))
    assert k.searched == "LM317T"
    assert result == [{"vendor_name": "X"}]
