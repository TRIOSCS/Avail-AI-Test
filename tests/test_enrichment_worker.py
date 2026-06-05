"""Tests for the enrichment worker status model (Task 7) and worker config + circuit
breaker (Task 8).

The singleton row is seeded by the Alembic migration in Postgres. In SQLite tests,
create_all builds the table but does not run migrations, so the row may be absent — the
test tolerates None (row is None or id==1).
"""


def test_worker_status_singleton(db_session):
    from app.models.enrichment_worker_status import EnrichmentWorkerStatus

    row = db_session.query(EnrichmentWorkerStatus).get(1)
    # Migration seeds id=1 in Postgres; in SQLite tests the row is absent (None).
    assert row is None or row.id == 1


def test_worker_status_model_importable():
    """Smoke test: the model and helper are importable and have expected columns."""
    from app.models.enrichment_worker_status import (
        EnrichmentWorkerStatus,
        update_enrichment_worker_status,
    )

    cols = {c.key for c in EnrichmentWorkerStatus.__table__.columns}
    assert "id" in cols
    assert "is_running" in cols
    assert "last_heartbeat" in cols
    assert "last_enriched_at" in cols
    assert "enriched_today" in cols
    assert "web_sourced_today" in cols
    assert "ai_inferred_today" in cols
    assert "not_found_today" in cols
    assert "circuit_breaker_open" in cols
    assert "circuit_breaker_reason" in cols
    assert "daily_stats_json" in cols
    assert "updated_at" in cols
    assert callable(update_enrichment_worker_status)


def test_worker_status_singleton_constraint():
    """The CheckConstraint name is correct."""
    from app.models.enrichment_worker_status import EnrichmentWorkerStatus

    constraints = {c.name for c in EnrichmentWorkerStatus.__table__.constraints}
    assert "ck_enrichment_worker_status_singleton" in constraints


def test_update_helper_noop_when_no_row(db_session):
    """update_enrichment_worker_status is a no-op when the singleton row is absent."""
    from app.models.enrichment_worker_status import update_enrichment_worker_status

    # Should not raise even if row doesn't exist
    update_enrichment_worker_status(db_session, is_running=True, enriched_today=5)


def test_update_helper_sets_fields(db_session):
    """update_enrichment_worker_status sets columns on the singleton row."""

    from app.models.enrichment_worker_status import (
        EnrichmentWorkerStatus,
        update_enrichment_worker_status,
    )

    # Seed the singleton row (mimicking the migration)
    row = EnrichmentWorkerStatus(id=1)
    db_session.add(row)
    db_session.commit()

    update_enrichment_worker_status(
        db_session,
        is_running=True,
        enriched_today=10,
        web_sourced_today=3,
        ai_inferred_today=4,
        not_found_today=3,
    )

    db_session.expire_all()
    refreshed = db_session.query(EnrichmentWorkerStatus).get(1)
    assert refreshed is not None
    assert refreshed.is_running is True
    assert refreshed.enriched_today == 10
    assert refreshed.web_sourced_today == 3
    assert refreshed.ai_inferred_today == 4
    assert refreshed.not_found_today == 3
    assert refreshed.updated_at is not None


# ---------------------------------------------------------------------------
# Task 8: EnrichmentWorkerConfig
# ---------------------------------------------------------------------------


def test_config_defaults():
    """EnrichmentWorkerConfig has the spec §5.5 defaults."""
    import os

    # Remove any stale overrides before testing defaults
    for key in [
        "ENRICHMENT_BATCH_SIZE",
        "ENRICHMENT_DAILY_CAP",
        "ENRICHMENT_WEB_DAILY_CAP",
        "ENRICHMENT_LOOP_SLEEP_SECONDS",
        "ENRICHMENT_IDLE_SLEEP_SECONDS",
        "ENRICHMENT_NOT_FOUND_RETRY_HOURS",
        "ENRICHMENT_CIRCUIT_BREAKER_ERRORS",
    ]:
        os.environ.pop(key, None)

    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig.from_env()
    assert cfg.batch_size == 5
    assert cfg.daily_cap == 200
    assert cfg.web_daily_cap == 80
    assert cfg.loop_sleep_seconds == 30
    assert cfg.idle_sleep_seconds == 300
    assert cfg.not_found_retry_hours == 22
    assert cfg.circuit_breaker_errors == 5


def test_config_env_override(monkeypatch):
    """An env-var override is reflected in EnrichmentWorkerConfig.from_env()."""
    monkeypatch.setenv("ENRICHMENT_BATCH_SIZE", "10")
    monkeypatch.setenv("ENRICHMENT_DAILY_CAP", "500")

    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig.from_env()
    assert cfg.batch_size == 10
    assert cfg.daily_cap == 500
    # Unchanged fields stay at default
    assert cfg.web_daily_cap == 80


def test_config_direct_kwargs():
    """EnrichmentWorkerConfig can be constructed with direct kwargs (for tests/Task
    9)."""
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(batch_size=10, not_found_retry_hours=22)
    assert cfg.batch_size == 10
    assert cfg.not_found_retry_hours == 22
    # Other fields stay at defaults
    assert cfg.daily_cap == 200
    assert cfg.web_daily_cap == 80


# ---------------------------------------------------------------------------
# Task 8: EnrichmentCircuitBreaker
# ---------------------------------------------------------------------------


def test_breaker_does_not_trip_below_threshold():
    """Fewer than N consecutive errors leaves the breaker closed."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    breaker.record_claude_error()
    assert not breaker.should_stop()


def test_breaker_trips_after_n_consecutive_errors():
    """N consecutive Claude errors trips the breaker (should_stop() → True)."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)
    for _ in range(3):
        breaker.record_claude_error()
    assert breaker.should_stop()
    assert breaker.is_open


def test_breaker_success_resets_counter():
    """A success resets the consecutive-error counter so the breaker stays closed."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    breaker.record_claude_error()
    breaker.record_claude_success()  # resets counter
    breaker.record_claude_error()
    breaker.record_claude_error()
    # Only 2 consecutive errors after the reset — not tripped
    assert not breaker.should_stop()


def test_breaker_cooldown_resets_after_1h():
    """After a 1h cooldown, should_stop() returns False even if the breaker was open."""
    import time
    from unittest.mock import patch

    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=2)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    breaker.record_claude_error()
    assert breaker.should_stop()

    # Simulate 1h + 1s elapsing since the breaker was tripped
    future = time.monotonic() + 3601
    with patch("time.monotonic", return_value=future):
        assert not breaker.should_stop()
        # is_open should also be cleared
        assert not breaker.is_open


def test_breaker_get_trip_info_includes_custom_fields():
    """get_trip_info() includes the base fields and trip_reason after trip."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=1)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    info = breaker.get_trip_info()
    assert info["is_open"] is True
    assert "consecutive_failures" in info
    assert info["trip_reason"] != ""
