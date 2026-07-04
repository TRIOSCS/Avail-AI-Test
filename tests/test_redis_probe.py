"""tests/test_redis_probe.py — RedisProbe lazy re-probe + downgrade observability.

Covers: sticky-disable on an intentional None, self-healing re-probe after a transient
failure, best-effort no-raise contract, backoff throttling, and the Prometheus
degraded gauge + downgrade counter.

Called by: pytest
Depends on: app.cache.redis_probe, app.prometheus_metrics (via REGISTRY samples)
"""

from prometheus_client import REGISTRY

import app.cache.redis_probe as rp
from app.cache.redis_probe import RedisProbe


def _degraded(subsystem: str):
    return REGISTRY.get_sample_value("redis_degraded", {"subsystem": subsystem})


def _downgrades(subsystem: str):
    return REGISTRY.get_sample_value("redis_downgrade_total", {"subsystem": subsystem})


class _Client:
    """Stand-in for a live redis client (identity is all the probe cares about)."""


def test_none_connect_disables_permanently():
    """Connect() returning None means intentionally off — no retries, no metric."""
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        return None

    probe = RedisProbe("unit_none", connect)
    assert probe.get() is None
    assert probe.get() is None
    assert calls["n"] == 1  # disabled after the first None — never re-probed


def test_live_client_cached():
    """A successful connect is cached and reused without re-calling connect."""
    client = _Client()
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        return client

    probe = RedisProbe("unit_live", connect)
    assert probe.get() is client
    assert probe.get() is client
    assert calls["n"] == 1


def test_transient_failure_then_reprobe_recovers(monkeypatch):
    """A raised connect degrades; a later re-probe recovers transparently."""
    monkeypatch.setattr(rp, "REPROBE_INTERVAL_S", 0.0)  # re-probe every call
    client = _Client()
    state = {"fail": True}

    def connect():
        if state["fail"]:
            raise ConnectionError("redis down")
        return client

    probe = RedisProbe("unit_recover", connect)

    # First call fails → degraded, returns None (does not raise).
    assert probe.get() is None
    assert probe._degraded is True
    assert _degraded("unit_recover") == 1
    assert _downgrades("unit_recover") == 1

    # Redis comes back; next call (interval elapsed) recovers.
    state["fail"] = False
    assert probe.get() is client
    assert probe._degraded is False
    assert _degraded("unit_recover") == 0


def test_downgrade_counter_counts_transitions_not_probes(monkeypatch):
    """The counter increments once per outage event, not once per failed re-probe."""
    monkeypatch.setattr(rp, "REPROBE_INTERVAL_S", 0.0)

    def connect():
        raise ConnectionError("still down")

    probe = RedisProbe("unit_counter", connect)
    probe.get()
    probe.get()
    probe.get()
    assert _downgrades("unit_counter") == 1  # one healthy->degraded transition only
    assert _degraded("unit_counter") == 1


def test_backoff_throttles_reprobe():
    """Within the re-probe interval the probe stays degraded without re-calling
    connect."""
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        raise ConnectionError("down")

    probe = RedisProbe("unit_backoff", connect)  # default 30s interval
    assert probe.get() is None
    assert probe.get() is None  # inside the window → no second connect attempt
    assert calls["n"] == 1


def test_get_never_raises(monkeypatch):
    """A probe failure (even a weird one) must never propagate to the caller."""
    monkeypatch.setattr(rp, "REPROBE_INTERVAL_S", 0.0)

    def connect():
        raise RuntimeError("boom")

    probe = RedisProbe("unit_noraise", connect)
    # Should return None, not raise.
    assert probe.get() is None


def test_reset_clears_state(monkeypatch):
    """Reset() forgets the cached client + degraded/disabled flags."""
    monkeypatch.setattr(rp, "REPROBE_INTERVAL_S", 0.0)
    client = _Client()

    def connect():
        return client

    probe = RedisProbe("unit_reset", connect)
    assert probe.get() is client
    probe.reset()
    assert probe._client is None
    assert probe._disabled is False
    assert probe.get() is client
