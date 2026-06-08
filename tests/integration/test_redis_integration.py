"""Redis integration tests — exercise the REAL redis-py client end-to-end.

What it does: connects to a live Redis (via the REDIS_URL env var) and drives
exactly the client operations the app relies on — ``from_url(...)`` with the
app's kwargs, ``ping``, ``get``/``setex``, ``scan``/``delete``, and
``decode_responses`` — plus the app's own connection code paths
(``intel_cache._get_redis`` and ``rate_limit._resolve_storage``).

Why it exists: the normal suite runs under ``TESTING=1``, where every Redis path
short-circuits to ``None`` / in-memory, so a redis-py-breaking change (e.g. a
future major bump, like redis-py 8.0 in PR #227) would sail through CI green.
These tests are the missing coverage. They run ONLY when ``INTEGRATION_REDIS_URL``
is set — the dedicated CI job that spins up a ``redis`` service — and skip
everywhere else (local dev, the main test run), so they never flake without a
real server. A dedicated var (not ``REDIS_URL``) is used because
``tests/conftest.py`` deliberately blanks ``REDIS_URL`` for test isolation.

Called by: ``.github/workflows/ci.yml`` "Redis integration tests" step.
Depends on: a reachable Redis server, ``app.cache.intel_cache``,
``app.rate_limit``, ``app.config.settings``.
"""

import os

import pytest

# Dedicated var: conftest.py blanks REDIS_URL for isolation, so the integration
# job passes the live server URL via INTEGRATION_REDIS_URL instead. Default to ""
# (falsy -> skip) so the type stays `str`, not `str | None`.
REDIS_URL = os.environ.get("INTEGRATION_REDIS_URL") or ""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not REDIS_URL,
        reason="INTEGRATION_REDIS_URL not set — redis integration tests skipped",
    ),
]

_KEY_PREFIX = "intel:itest:"


@pytest.fixture
def redis_client():
    """A real redis-py client, or skip if the server is unreachable.

    Cleans up this test namespace before and after so reruns are deterministic.
    """
    import redis

    client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3)
    try:
        client.ping()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")

    def _clear():
        for key in client.scan_iter(match=f"{_KEY_PREFIX}*"):
            client.delete(key)

    _clear()
    yield client
    _clear()


def test_client_ops_app_relies_on(redis_client):
    """from_url + setex + get + scan + delete with the app's exact usage.

    Mirrors intel_cache.set_cached/get_cached (setex+get) and
    decorators.invalidate_prefix (scan+delete by pattern).
    """
    r = redis_client

    # setex with a TTL, then get — decode_responses=True returns a str, not bytes.
    r.setex(f"{_KEY_PREFIX}k1", 60, '{"v": 1}')
    assert r.get(f"{_KEY_PREFIX}k1") == '{"v": 1}'
    assert r.ttl(f"{_KEY_PREFIX}k1") > 0

    # scan + delete by pattern (the invalidate_prefix cursor loop).
    r.setex(f"{_KEY_PREFIX}p:a", 60, "1")
    r.setex(f"{_KEY_PREFIX}p:b", 60, "2")
    found: list[str] = []
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{_KEY_PREFIX}p:*", count=100)
        found.extend(keys)
        if cursor == 0:
            break
    assert set(found) == {f"{_KEY_PREFIX}p:a", f"{_KEY_PREFIX}p:b"}
    assert r.delete(*found) == 2
    assert r.get(f"{_KEY_PREFIX}p:a") is None


def test_intel_cache_get_redis_connects(monkeypatch):
    """app.cache.intel_cache._get_redis() connects to a real Redis and round-trips."""
    from app.cache import intel_cache
    from app.config import settings

    # Defeat the TESTING short-circuit and point at the live server.
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(settings, "cache_backend", "redis", raising=False)
    monkeypatch.setattr(settings, "redis_url", REDIS_URL, raising=False)
    # Reset the lazy-init module globals so it reconnects with our settings.
    monkeypatch.setattr(intel_cache, "_redis_init_attempted", False, raising=False)
    monkeypatch.setattr(intel_cache, "_redis_client", None, raising=False)

    client = intel_cache._get_redis()
    assert client is not None, "intel_cache could not connect to Redis"
    assert client.ping() is True

    client.setex(f"{_KEY_PREFIX}gr", 30, "ok")
    assert client.get(f"{_KEY_PREFIX}gr") == "ok"
    client.delete(f"{_KEY_PREFIX}gr")


def test_rate_limiter_picks_redis_storage(monkeypatch):
    """rate_limit._resolve_storage() returns the Redis URL, not the in-memory
    fallback."""
    from app import rate_limit
    from app.config import settings

    monkeypatch.setattr(settings, "cache_backend", "redis", raising=False)
    monkeypatch.setattr(settings, "redis_url", REDIS_URL, raising=False)

    assert rate_limit._resolve_storage() == REDIS_URL
