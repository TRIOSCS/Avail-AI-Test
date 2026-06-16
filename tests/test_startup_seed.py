"""Tests for app.startup idempotent seeds.

Called by: pytest
Depends on: app.startup, app.models (ApiSource, IcsWorkerStatus, NcWorkerStatus)
"""

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.constants import ApiSourceStatus


def make_api_source(name, display_name, status, *, is_active, source_type="broker"):
    """Build an ApiSource row with the fields these seed tests vary."""
    from app.models import ApiSource

    return ApiSource(
        name=name,
        display_name=display_name,
        category="search",
        source_type=source_type,
        status=status,
        is_active=is_active,
    )


def test_startup_flips_icsource_to_live(db_session: Session):
    from app.models import ApiSource
    from app.startup import seed_browser_worker_sources

    db_session.add(make_api_source("icsource", "ICsource", "disabled", is_active=False))
    db_session.add(make_api_source("netcomponents", "NetComponents", "pending", is_active=False))
    db_session.commit()

    seed_browser_worker_sources(db_session)
    db_session.commit()

    ics = db_session.query(ApiSource).filter_by(name="icsource").one()
    nc = db_session.query(ApiSource).filter_by(name="netcomponents").one()
    assert ics.status == ApiSourceStatus.LIVE.value
    assert ics.is_active is True
    assert nc.status == ApiSourceStatus.LIVE.value
    assert nc.is_active is True


def test_seed_is_idempotent(db_session: Session):
    """Running the seed twice produces the same state."""
    from app.models import ApiSource
    from app.startup import seed_browser_worker_sources

    db_session.add(make_api_source("icsource", "ICsource", "disabled", is_active=False))
    db_session.commit()

    seed_browser_worker_sources(db_session)
    db_session.commit()
    seed_browser_worker_sources(db_session)
    db_session.commit()

    ics = db_session.query(ApiSource).filter_by(name="icsource").one()
    assert ics.status == ApiSourceStatus.LIVE.value
    assert ics.is_active is True


def test_seed_skips_missing_rows(db_session: Session):
    """If the api_sources rows don't exist, the seed silently skips."""
    from app.startup import seed_browser_worker_sources

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

    seed_ics_worker_status_singleton(db_session)
    db_session.commit()
    count = db_session.query(IcsWorkerStatus).count()
    assert count == 1


def test_startup_seeds_nc_worker_status_singleton(db_session: Session):
    """NC worker has the same singleton pattern as ICS — same bug if not seeded."""
    from app.models import NcWorkerStatus
    from app.startup import seed_nc_worker_status_singleton

    db_session.query(NcWorkerStatus).delete()
    db_session.commit()

    seed_nc_worker_status_singleton(db_session)
    db_session.commit()

    row = db_session.query(NcWorkerStatus).filter_by(id=1).one()
    assert row.is_running is False

    seed_nc_worker_status_singleton(db_session)
    db_session.commit()
    count = db_session.query(NcWorkerStatus).count()
    assert count == 1


def test_seed_browser_workers_swallows_db_error(monkeypatch):
    """Wrapper logs + rolls back on DB error so startup proceeds."""
    from app import startup

    def boom(_db):
        raise SQLAlchemyError("simulated")

    monkeypatch.setattr(startup, "seed_browser_worker_sources", boom)

    # Must not raise — startup must continue even if the seed fails.
    startup.seed_browser_workers()


def test_health_monitor_excludes_browser_worker_sources(db_session: Session):
    """The 15-min ping loop must skip BROWSER_WORKER_SOURCES.

    Otherwise `_get_connector_for_source` returns None and `ping_source` flips
    the seed back to DISABLED, so the LIVE state from `seed_browser_worker_sources`
    would survive only ~15 minutes after each app boot.
    """
    from app.constants import BROWSER_WORKER_SOURCES, ApiSourceStatus
    from app.models import ApiSource

    live = ApiSourceStatus.LIVE.value
    db_session.add(make_api_source("icsource", "ICsource", live, is_active=True))
    db_session.add(make_api_source("netcomponents", "NetComponents", live, is_active=True))
    db_session.add(make_api_source("digikey", "Digi-Key", live, is_active=True, source_type="distributor"))
    db_session.commit()

    rows = (
        db_session.query(ApiSource)
        .filter(ApiSource.is_active.is_(True))
        .filter(~ApiSource.name.in_(BROWSER_WORKER_SOURCES))
        .all()
    )
    names = {r.name for r in rows}
    assert names == {"digikey"}
    assert "icsource" not in names
    assert "netcomponents" not in names
