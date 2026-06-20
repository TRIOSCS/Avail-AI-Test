"""Tests for the outbound resilience wrapper (app/connectors/resilience.py).

Covers: pass-through success, retry on 429/5xx then success, Retry-After,
breaker-open short-circuit, timeout exhaustion → ProviderUnavailable, and
auth-failure passthrough. Backoff sleeps are 0 under TESTING (TESTING=1 in conftest).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.connectors import resilience
from app.connectors.resilience import ProviderUnavailable, resilient_call


def _resp(status, headers=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.headers = headers or {}
    return r


@pytest.fixture(autouse=True)
def _clean():
    resilience.reset_state()
    from app.connectors.sources import _breakers
    _breakers.clear()
    yield


def test_success_passthrough():
    factory = AsyncMock(return_value=_resp(200))
    resp = asyncio.run(resilient_call("lusha", factory))
    assert resp.status_code == 200
    assert factory.await_count == 1


def test_retries_on_500_then_succeeds():
    factory = AsyncMock(side_effect=[_resp(500), _resp(200)])
    resp = asyncio.run(resilient_call("apollo", factory, max_retries=2))
    assert resp.status_code == 200
    assert factory.await_count == 2


def test_retryable_exhausted_returns_last_response():
    factory = AsyncMock(side_effect=[_resp(503), _resp(503), _resp(503)])
    resp = asyncio.run(resilient_call("explorium", factory, max_retries=2))
    assert resp.status_code == 503
    assert factory.await_count == 3


def test_respects_retry_after_header():
    factory = AsyncMock(side_effect=[_resp(429, {"Retry-After": "0"}), _resp(200)])
    resp = asyncio.run(resilient_call("clay", factory, max_retries=1))
    assert resp.status_code == 200
    assert factory.await_count == 2


def test_auth_failure_passes_response_through():
    factory = AsyncMock(return_value=_resp(401))
    resp = asyncio.run(resilient_call("lusha", factory))
    assert resp.status_code == 401
    # Not retried — auth errors won't fix themselves
    assert factory.await_count == 1


def test_timeout_exhaustion_raises_provider_unavailable():
    factory = AsyncMock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(resilient_call("apollo", factory, max_retries=1))
    assert factory.await_count == 2


def test_open_breaker_short_circuits():
    from app.connectors.sources import get_breaker
    breaker = get_breaker("provider:lusha")
    for _ in range(breaker.fail_max):
        breaker.record_failure()
    assert breaker.current_state == "open"
    factory = AsyncMock(return_value=_resp(200))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(resilient_call("lusha", factory))
    factory.assert_not_awaited()


def test_non_retryable_4xx_returns_immediately():
    factory = AsyncMock(return_value=_resp(404))
    resp = asyncio.run(resilient_call("explorium", factory, max_retries=2))
    assert resp.status_code == 404
    assert factory.await_count == 1


def test_record_provider_result_noop_under_testing():
    # Under TESTING, no DB/alert side effects — should not raise.
    resilience.record_provider_result("lusha", ok=True)
    resilience.record_provider_result("lusha", ok=False, error="x", auth_failure=True)
