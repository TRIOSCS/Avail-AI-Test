"""Database connection and session factory.

All naive datetimes from PostgreSQL are auto-tagged as UTC via event listener to prevent
naive-vs-aware comparison errors.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, TypeDecorator, create_engine
from sqlalchemy.orm import sessionmaker

from .config import settings


class UTCDateTime(TypeDecorator[datetime]):
    """DateTime that stores and returns timezone-aware UTC values.

    Maps to ``TIMESTAMP WITH TIME ZONE`` on every dialect (via
    ``load_dialect_impl``) so column storage is uniform regardless of whether a
    column was declared ``UTCDateTime`` or ``UTCDateTime(timezone=True)``.

    Normalizes on both directions:
    - write (``process_bind_param``): naive values are assumed UTC; aware values
      are converted to UTC. This closes the silent-corruption gap where a naive
      *local* time would otherwise be stored verbatim and later mislabeled UTC.
    - read (``process_result_value``): naive values coming back (legacy rows,
      SQLite) are tagged UTC.

    Net effect: the application layer always sees aware UTC datetimes.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        # Force TIMESTAMP WITH TIME ZONE uniformly; on SQLite the timezone flag
        # is ignored by the dialect (values round-trip through bind/result).
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        # Only normalize real datetimes; strings/None pass through (callers may
        # bind ISO strings or NULL, and the dialect handles those).
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
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
