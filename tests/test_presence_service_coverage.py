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

# ---------------------------------------------------------------------------
# LRU eviction — lines 35-37
# ---------------------------------------------------------------------------


async def test_get_presence_lru_eviction_triggers():
    """When cache is at _CACHE_MAX, oldest 20% entries are evicted before inserting."""
    import time

    import app.services.presence_service as svc

    # Save original values and patch _CACHE_MAX to a small number for test speed
    original_cache = svc._presence_cache.copy()
    original_max = svc._CACHE_MAX

    try:
        svc._CACHE_MAX = 5
        svc._presence_cache.clear()

        # Fill cache to exactly _CACHE_MAX entries with staggered timestamps
        base_time = time.monotonic() - 100
        for i in range(5):
            svc._presence_cache[f"user{i}@example.com"] = ("Available", base_time + i)

        assert len(svc._presence_cache) == 5

        # Now call get_presence for a new email — eviction should fire (5 >= 5)
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"availability": "Busy"}

        result = await svc.get_presence("new_user@example.com", mock_gc)

        # After eviction (removes oldest 20% = 1 entry) and insert, size should be ≤ 5
        assert result == "Busy"
        assert "new_user@example.com" in svc._presence_cache
        # One entry was evicted (user0, the oldest), so size should be 5 again
        assert len(svc._presence_cache) == 5
        assert "user0@example.com" not in svc._presence_cache

    finally:
        svc._CACHE_MAX = original_max
        svc._presence_cache.clear()
        svc._presence_cache.update(original_cache)


async def test_get_presence_lru_eviction_removes_oldest_twenty_percent():
    """LRU eviction removes exactly _CACHE_MAX // 5 oldest entries."""
    import time

    import app.services.presence_service as svc

    original_cache = svc._presence_cache.copy()
    original_max = svc._CACHE_MAX

    try:
        svc._CACHE_MAX = 10
        svc._presence_cache.clear()

        base_time = time.monotonic() - 200
        for i in range(10):
            svc._presence_cache[f"evict{i}@example.com"] = ("Away", base_time + i)

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"availability": "DoNotDisturb"}

        result = await svc.get_presence("fresh@example.com", mock_gc)

        assert result == "DoNotDisturb"
        # 2 oldest (10 // 5 = 2) should be evicted, then 1 new entry added → 9 total
        assert len(svc._presence_cache) == 9
        assert "evict0@example.com" not in svc._presence_cache
        assert "evict1@example.com" not in svc._presence_cache

    finally:
        svc._CACHE_MAX = original_max
        svc._presence_cache.clear()
        svc._presence_cache.update(original_cache)


# ---------------------------------------------------------------------------
# Exception handling — auth failure (lines 41-43)
# ---------------------------------------------------------------------------


async def test_get_presence_auth_failure_401_returns_none():
    """When graph client raises an error containing '401', logs auth error and returns
    None."""
    import app.services.presence_service as svc

    original_cache = svc._presence_cache.copy()
    svc._presence_cache.clear()

    try:
        mock_gc = AsyncMock()
        mock_gc.get_json.side_effect = Exception("401 Unauthorized — token expired")

        result = await svc.get_presence("authfail@example.com", mock_gc)

        assert result is None
        assert "authfail@example.com" not in svc._presence_cache

    finally:
        svc._presence_cache.clear()
        svc._presence_cache.update(original_cache)


async def test_get_presence_auth_failure_403_returns_none():
    """When graph client raises an error containing '403', logs auth error and returns
    None."""
    import app.services.presence_service as svc

    original_cache = svc._presence_cache.copy()
    svc._presence_cache.clear()

    try:
        mock_gc = AsyncMock()
        mock_gc.get_json.side_effect = Exception("403 Forbidden — insufficient scope")

        result = await svc.get_presence("forbidden@example.com", mock_gc)

        assert result is None

    finally:
        svc._presence_cache.clear()
        svc._presence_cache.update(original_cache)


# ---------------------------------------------------------------------------
# Exception handling — general warning (lines 44-46)
# ---------------------------------------------------------------------------


async def test_get_presence_general_exception_returns_none():
    """Non-auth exceptions log a warning (not error) and return None."""
    import app.services.presence_service as svc

    original_cache = svc._presence_cache.copy()
    svc._presence_cache.clear()

    try:
        mock_gc = AsyncMock()
        mock_gc.get_json.side_effect = ConnectionError("Network unreachable")

        result = await svc.get_presence("offline@example.com", mock_gc)

        assert result is None
        assert "offline@example.com" not in svc._presence_cache

    finally:
        svc._presence_cache.clear()
        svc._presence_cache.update(original_cache)


async def test_get_presence_timeout_exception_returns_none():
    """TimeoutError is a general exception — logs warning and returns None."""
    import app.services.presence_service as svc

    original_cache = svc._presence_cache.copy()
    svc._presence_cache.clear()

    try:
        mock_gc = AsyncMock()
        mock_gc.get_json.side_effect = TimeoutError("Request timed out")

        result = await svc.get_presence("timeout@example.com", mock_gc)

        assert result is None

    finally:
        svc._presence_cache.clear()
        svc._presence_cache.update(original_cache)


# ---------------------------------------------------------------------------
# presence_color — all branches (belt-and-suspenders, some already covered)
# ---------------------------------------------------------------------------


def test_presence_color_be_right_back():
    from app.services.presence_service import presence_color

    assert presence_color("BeRightBack") == "bg-amber-400"


def test_presence_color_do_not_disturb():
    from app.services.presence_service import presence_color

    assert presence_color("DoNotDisturb") == "bg-rose-400"


def test_presence_color_offline():
    from app.services.presence_service import presence_color

    assert presence_color("Offline") == "bg-gray-300"


def test_presence_color_unknown_string():
    from app.services.presence_service import presence_color

    assert presence_color("PresenceUnknown") == "bg-gray-300"
