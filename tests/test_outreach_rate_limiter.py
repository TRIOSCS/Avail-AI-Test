"""Tests for the shared per-user outreach rate limiter
(``app.rate_limit.check_rate_limit``).

The CDM outreach endpoints (click-to-call, click-to-contact, call-outcome) used to share
a per-process in-memory dict, so the limit was not enforced across worker processes or
across restarts. The limiter now rides the shared Redis substrate
(``app.cache.intel_cache``) — an atomic fixed-window counter — and degrades to a
per-process in-memory counter only when Redis is unavailable.

Covers: under-limit allows, over-limit blocks, state shared via the Redis substrate
(survives an in-memory reset → proxy for cross-process / restart), graceful in-memory
fallback when Redis is down or erroring, window rollover, and per-(user, bucket)
isolation.

Called by: pytest
Depends on: app.rate_limit
"""

import pytest

from app import rate_limit


class FakeRedis:
    """Minimal redis stand-in implementing the slice the limiter uses (incr/expire).

    A single instance models one shared Redis server: every ``check_rate_limit`` call
    that resolves to this instance shares ``store``, which is how the test simulates
    state being shared across processes.
    """

    def __init__(self, *, fail: bool = False):
        self.store: dict[str, int] = {}
        self.expirations: dict[str, int] = {}
        self.fail = fail

    def incr(self, key: str) -> int:
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key: str, ttl: int) -> bool:
        if self.fail:
            raise RuntimeError("redis down")
        self.expirations[key] = ttl
        return True


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Reset the in-memory fallback counter around every test."""
    rate_limit.reset_rate_limit_state()
    yield
    rate_limit.reset_rate_limit_state()


# ── Redis-backed (shared) path ─────────────────────────────────────────


def test_under_limit_allows(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: redis)

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True


def test_over_limit_blocks(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: redis)

    # Exactly ``limit`` calls are allowed...
    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True
    # ...the next one is blocked.
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is False


def test_redis_key_carries_a_ttl(monkeypatch):
    """The Redis counter key is given a bounded TTL so windows self-expire."""
    redis = FakeRedis()
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: redis)

    rate_limit.check_rate_limit(7, "outreach", limit=5, window_seconds=60)

    assert len(redis.store) == 1
    (key,) = redis.store
    assert redis.expirations[key] >= 60


def test_state_shared_via_redis_substrate(monkeypatch):
    """The count lives in the Redis substrate, not the per-process store.

    Clearing the in-memory fallback (the proxy for a fresh process / restart) must NOT
    reset the limit while Redis is up — that is what makes the limit hold across
    workers.
    """
    redis = FakeRedis()
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: redis)

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True

    # Simulate a different worker / a restart: wipe local state.
    rate_limit.reset_rate_limit_state()

    # The shared Redis counter still remembers — the next call is blocked.
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is False
    # And the counter genuinely lives in Redis.
    assert list(redis.store.values()) == [6]


# ── In-memory fallback (Redis down) ────────────────────────────────────


def test_redis_down_falls_back_to_in_memory(monkeypatch):
    """Redis unavailable (``_get_redis`` → None): enforcement still happens locally."""
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is False


def test_redis_error_falls_back_gracefully(monkeypatch):
    """A Redis client that raises must not surface — fall back to in-memory."""
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: FakeRedis(fail=True))

    # No exception escapes, and the limit is still enforced.
    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is False


def test_in_memory_reset_clears_fallback(monkeypatch):
    """When Redis is down, ``reset_rate_limit_state`` does free the bucket again."""
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is False

    rate_limit.reset_rate_limit_state()
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True


# ── Window + key isolation ─────────────────────────────────────────────


def test_window_rollover_resets_counter(monkeypatch):
    """Advancing the clock past the window starts a fresh budget (in-memory path)."""
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(rate_limit, "_now", lambda: clock["t"])

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5, window_seconds=60) is True
    assert rate_limit.check_rate_limit(1, "outreach", limit=5, window_seconds=60) is False

    # Jump into the next window.
    clock["t"] += 61
    assert rate_limit.check_rate_limit(1, "outreach", limit=5, window_seconds=60) is True


def test_window_rollover_resets_counter_redis(monkeypatch):
    """Same rollover guarantee on the Redis path: the key is window-indexed."""
    redis = FakeRedis()
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: redis)
    clock = {"t": 1000.0}
    monkeypatch.setattr(rate_limit, "_now", lambda: clock["t"])

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5, window_seconds=60) is True
    assert rate_limit.check_rate_limit(1, "outreach", limit=5, window_seconds=60) is False

    clock["t"] += 61
    assert rate_limit.check_rate_limit(1, "outreach", limit=5, window_seconds=60) is True


def test_buckets_are_independent(monkeypatch):
    """Exhausting one bucket must not touch another bucket's budget."""
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "call", limit=5) is True
    assert rate_limit.check_rate_limit(1, "call", limit=5) is False

    # Different bucket, same user — fresh budget.
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True


def test_users_are_independent(monkeypatch):
    """Two users do not share a budget."""
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)

    for _ in range(5):
        assert rate_limit.check_rate_limit(1, "outreach", limit=5) is True
    assert rate_limit.check_rate_limit(1, "outreach", limit=5) is False

    assert rate_limit.check_rate_limit(2, "outreach", limit=5) is True
