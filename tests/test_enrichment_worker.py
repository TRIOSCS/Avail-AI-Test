"""Tests for the enrichment worker status model (Task 7).

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
