"""Database connection and session factory.

All naive datetimes from PostgreSQL are auto-tagged as UTC via event listener to prevent
naive-vs-aware comparison errors.
"""

from datetime import timezone

from sqlalchemy import DateTime, TypeDecorator, create_engine
from sqlalchemy.orm import sessionmaker

from .config import settings


class UTCDateTime(TypeDecorator):
    """DateTime type that ensures UTC timezone on load."""

    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _make_engine(database_url: str):
    """Build the SQLAlchemy engine for ``database_url``.

    Split out from module scope so the PostgreSQL configuration branch is unit-testable
    directly — re-importing this module to exercise it would rebuild the shared engine
    and corrupt parallel (xdist) tests.
    """
    if database_url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    connect_args: dict[str, object] = {"connect_timeout": 10}
    if database_url.startswith("postgresql"):
        connect_args["options"] = "-c statement_timeout=30000 -c lock_timeout=5000"

    return create_engine(
        database_url,
        pool_size=20,
        max_overflow=20,
        pool_timeout=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


engine = _make_engine(settings.database_url)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
