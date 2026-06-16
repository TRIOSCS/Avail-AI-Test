"""tests/test_presence_service_coverage.py — Coverage tests for
app/services/presence_service.py.

Targets uncovered lines:
  - Lines 35-37: LRU eviction when cache is at _CACHE_MAX capacity
  - Lines 41-47: Exception handling (401/403 auth failure, general warning)

Already covered by test_integrations.py:
  - Happy path: get_presence returns status
  - Cache hit: second call skips API
  - presence_color: all branches
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock

import pytest

import app.services.presence_service as svc


@pytest.fixture()
def isolated_cache():
    """Snapshot _presence_cache (and _CACHE_MAX), clear it for the test, then
    restore."""
    original_cache = svc._presence_cache.copy()
    original_max = svc._CACHE_MAX
    svc._presence_cache.clear()
    try:
        yield svc
    finally:
        svc._CACHE_MAX = original_max
        svc._presence_cache.clear()
        svc._presence_cache.update(original_cache)


# ---------------------------------------------------------------------------
# LRU eviction — lines 35-37
# ---------------------------------------------------------------------------


async def test_get_presence_lru_eviction_triggers(isolated_cache):
    """When cache is at _CACHE_MAX, oldest 20% entries are evicted before inserting."""
    import time

    isolated_cache._CACHE_MAX = 5

    # Fill cache to exactly _CACHE_MAX entries with staggered timestamps
    base_time = time.monotonic() - 100
    for i in range(5):
        isolated_cache._presence_cache[f"user{i}@example.com"] = ("Available", base_time + i)

    assert len(isolated_cache._presence_cache) == 5

    # Now call get_presence for a new email — eviction should fire (5 >= 5)
    mock_gc = AsyncMock()
    mock_gc.get_json.return_value = {"availability": "Busy"}

    result = await isolated_cache.get_presence("new_user@example.com", mock_gc)

    # After eviction (removes oldest 20% = 1 entry) and insert, size should be ≤ 5
    assert result == "Busy"
    assert "new_user@example.com" in isolated_cache._presence_cache
    # One entry was evicted (user0, the oldest), so size should be 5 again
    assert len(isolated_cache._presence_cache) == 5
    assert "user0@example.com" not in isolated_cache._presence_cache


async def test_get_presence_lru_eviction_removes_oldest_twenty_percent(isolated_cache):
    """LRU eviction removes exactly _CACHE_MAX // 5 oldest entries."""
    import time

    isolated_cache._CACHE_MAX = 10

    base_time = time.monotonic() - 200
    for i in range(10):
        isolated_cache._presence_cache[f"evict{i}@example.com"] = ("Away", base_time + i)

    mock_gc = AsyncMock()
    mock_gc.get_json.return_value = {"availability": "DoNotDisturb"}

    result = await isolated_cache.get_presence("fresh@example.com", mock_gc)

    assert result == "DoNotDisturb"
    # 2 oldest (10 // 5 = 2) should be evicted, then 1 new entry added → 9 total
    assert len(isolated_cache._presence_cache) == 9
    assert "evict0@example.com" not in isolated_cache._presence_cache
    assert "evict1@example.com" not in isolated_cache._presence_cache


# ---------------------------------------------------------------------------
# Exception handling — auth failure (lines 41-43) and general warning (lines 44-46)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "error"),
    [
        # Errors containing 401/403 log an auth error and return None.
        pytest.param("authfail@example.com", Exception("401 Unauthorized — token expired"), id="auth_failure_401"),
        pytest.param("forbidden@example.com", Exception("403 Forbidden — insufficient scope"), id="auth_failure_403"),
        # Non-auth exceptions log a warning (not error) and return None.
        pytest.param("offline@example.com", ConnectionError("Network unreachable"), id="general_exception"),
        pytest.param("timeout@example.com", TimeoutError("Request timed out"), id="timeout_exception"),
    ],
)
async def test_get_presence_exception_returns_none(isolated_cache, email, error):
    """A get_json failure returns None and does not cache the email."""
    mock_gc = AsyncMock()
    mock_gc.get_json.side_effect = error

    result = await isolated_cache.get_presence(email, mock_gc)

    assert result is None
    assert email not in isolated_cache._presence_cache


# ---------------------------------------------------------------------------
# presence_color — all branches (belt-and-suspenders, some already covered)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param("BeRightBack", "bg-amber-400", id="be_right_back"),
        pytest.param("DoNotDisturb", "bg-rose-400", id="do_not_disturb"),
        pytest.param("Offline", "bg-gray-300", id="offline"),
        pytest.param("PresenceUnknown", "bg-gray-300", id="unknown_string"),
    ],
)
def test_presence_color(status, expected):
    from app.services.presence_service import presence_color

    assert presence_color(status) == expected
