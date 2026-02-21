"""
tests/test_circuit_breaker.py — Tests for circuit breaker on connectors

Covers: breaker creation, state transitions (closed → open → half-open),
and that open breakers skip API calls gracefully.

Called by: pytest
Depends on: app.connectors.sources (BaseConnector, CircuitBreaker, get_breaker)
"""

import asyncio
import time

import pytest

from app.connectors.sources import BaseConnector, CircuitBreaker, get_breaker, _breakers


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


def test_breaker_half_open_after_timeout():
    """After reset_timeout elapses, breaker transitions to half_open."""
    b = CircuitBreaker(name="test", fail_max=1, reset_timeout=0.1)
    b.record_failure()
    assert b.current_state == "open"
    time.sleep(0.15)
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
    result = asyncio.get_event_loop().run_until_complete(conn.search("LM317T"))
    assert len(result) == 1
    assert conn._breaker.current_state == "closed"


def test_connector_opens_breaker_after_failures():
    """After 5 consecutive failures, connector breaker opens."""
    conn = _FakeConnector(fail=True)
    for _ in range(5):
        try:
            asyncio.get_event_loop().run_until_complete(conn.search("LM317T"))
        except ConnectionError:
            pass
    assert conn._breaker.current_state == "open"


def test_open_breaker_skips_calls():
    """When breaker is open, search returns [] without calling _do_search."""
    conn = _FakeConnector(fail=True)
    # Trip the breaker (fail_max=5, max_retries=0 → 5 search calls)
    for _ in range(5):
        try:
            asyncio.get_event_loop().run_until_complete(conn.search("LM317T"))
        except ConnectionError:
            pass
    assert conn._breaker.current_state == "open"

    # Reset call count and stop failing
    conn.call_count = 0
    conn._fail = False

    result = asyncio.get_event_loop().run_until_complete(conn.search("LM317T"))
    assert result == []
    assert conn.call_count == 0  # _do_search was never called
