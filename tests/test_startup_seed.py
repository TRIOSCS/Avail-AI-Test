"""Tests for app.startup idempotent seeds.

Called by: pytest
Depends on: app.startup, app.models (ApiSource, IcsWorkerStatus)
"""

from sqlalchemy.orm import Session


def test_startup_flips_icsource_to_live(db_session: Session):
    from app.models import ApiSource
    from app.startup import seed_browser_worker_sources

    db_session.add(
        ApiSource(
            name="icsource",
            display_name="ICsource",
            category="search",
            source_type="broker",
            status="disabled",
            is_active=False,
        )
    )
    db_session.add(
        ApiSource(
            name="netcomponents",
            display_name="NetComponents",
            category="search",
            source_type="broker",
            status="pending",
            is_active=False,
        )
    )
    db_session.commit()

    seed_browser_worker_sources(db_session)
    db_session.commit()

    ics = db_session.query(ApiSource).filter_by(name="icsource").one()
    nc = db_session.query(ApiSource).filter_by(name="netcomponents").one()
    assert ics.status == "live"
    assert ics.is_active is True
    assert nc.status == "live"
    assert nc.is_active is True


def test_seed_is_idempotent(db_session: Session):
    """Running the seed twice produces the same state."""
    from app.models import ApiSource
    from app.startup import seed_browser_worker_sources

    db_session.add(
        ApiSource(
            name="icsource",
            display_name="ICsource",
            category="search",
            source_type="broker",
            status="disabled",
            is_active=False,
        )
    )
    db_session.commit()

    seed_browser_worker_sources(db_session)
    db_session.commit()
    seed_browser_worker_sources(db_session)
    db_session.commit()

    ics = db_session.query(ApiSource).filter_by(name="icsource").one()
    assert ics.status == "live"
    assert ics.is_active is True


def test_seed_skips_missing_rows(db_session: Session):
    """If the api_sources rows don't exist, the seed silently skips."""
    from app.startup import seed_browser_worker_sources

    # No setup — rows don't exist
    seed_browser_worker_sources(db_session)
    db_session.commit()  # Should not raise


def test_startup_seeds_ics_worker_status_singleton(db_session: Session):
    from app.models import IcsWorkerStatus
    from app.startup import seed_ics_worker_status_singleton

    db_session.query(IcsWorkerStatus).delete()
    db_session.commit()

    seed_ics_worker_status_singleton(db_session)
    db_session.commit()

    row = db_session.query(IcsWorkerStatus).filter_by(id=1).one()
    assert row.is_running is False

    # Idempotent — running again leaves the row alone
    seed_ics_worker_status_singleton(db_session)
    db_session.commit()
    count = db_session.query(IcsWorkerStatus).count()
    assert count == 1
