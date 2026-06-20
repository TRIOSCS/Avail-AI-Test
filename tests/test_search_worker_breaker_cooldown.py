"""Tests for the self-healing circuit-breaker cooldown in CircuitBreakerBase.

Once tripped, should_stop() must auto-reset after cooldown_seconds so a transient block
(captcha/rate-limit) doesn't wedge a worker until a process restart.
"""

import time

from app.services.ics_worker.circuit_breaker import CircuitBreaker
from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase


def test_not_open_returns_false():
    b = CircuitBreakerBase(cooldown_seconds=60)
    assert b.should_stop() is False


def test_trip_blocks_until_cooldown_then_auto_resets(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    b = CircuitBreakerBase(cooldown_seconds=60)
    b._trip("captcha detected")
    assert b.is_open is True
    assert b.should_stop() is True  # just tripped

    clock[0] += 59
    assert b.should_stop() is True  # still within cooldown

    clock[0] += 2  # now > 60s since trip
    assert b.should_stop() is False  # auto-reset
    assert b.is_open is False
    assert b.trip_reason == ""
    assert b.consecutive_failures == 0


def test_ics_subclass_inherits_cooldown(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    b = CircuitBreaker(cooldown_seconds=10)
    b._trip("rate limited")
    assert b.should_stop() is True
    clock[0] += 11
    assert b.should_stop() is False  # self-healed


def test_empty_results_trip_also_self_heals(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    b = CircuitBreakerBase(cooldown_seconds=30)
    for _ in range(10):  # 10 consecutive empties trips the breaker
        b.record_empty_results()
    assert b.is_open is True
    assert b.should_stop() is True
    clock[0] += 31
    assert b.should_stop() is False
