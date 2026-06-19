"""SP1 credit guard: circuit cooldown around paid-provider quota/rate-limit errors.

trip_circuit writes an intel-cache marker; circuit_open reads it. TTL is minutes→days.
Both intel-cache calls are patched at the credit-guard module (the import site here).
"""

import os

os.environ["TESTING"] = "1"

from app.services import enrichment_credit_guard as guard


def test_provider_quota_error_is_exception():
    assert issubclass(guard.ProviderQuotaError, Exception)


def test_circuit_open_false_when_no_marker(monkeypatch):
    monkeypatch.setattr(guard, "get_cached", lambda key: None)
    assert guard.circuit_open("lusha") is False


def test_circuit_open_true_when_marker_present(monkeypatch):
    monkeypatch.setattr(guard, "get_cached", lambda key: {"tripped": 1})
    assert guard.circuit_open("lusha") is True


def test_trip_circuit_writes_marker_with_minutes_ttl(monkeypatch):
    captured = {}

    def _fake_set(key, data, ttl_days):
        captured["key"] = key
        captured["data"] = data
        captured["ttl_days"] = ttl_days

    monkeypatch.setattr(guard, "set_cached", _fake_set)
    guard.trip_circuit("lusha", 15)
    assert captured["key"] == "enrich:circuit:lusha"
    assert captured["data"] == {"tripped": 1}
    assert captured["ttl_days"] == 15 / 1440
