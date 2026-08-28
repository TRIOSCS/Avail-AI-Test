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


def _make_engine(database_url: str, *, pool_size: int = 5, max_overflow: int = 5):
    """Build the SQLAlchemy engine for ``database_url``.

    Split out from module scope so the PostgreSQL configuration branch is unit-testable
    directly — re-importing this module to exercise it would rebuild the shared engine
    and corrupt parallel (xdist) tests.

    ``pool_size``/``max_overflow`` are per-process caps (ignored on the sqlite
    branch, which always uses StaticPool). Defaults match ``Settings.db_pool_size``/
    ``db_max_overflow`` — see the connection-budget comment above the module-level
    ``engine`` below for how those defaults were sized.
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
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


# Phase-4 infra: connection-budget arithmetic against Postgres max_connections=100
# (docker-compose.yml's `db` service takes the postgres:16-alpine image default,
# which is 100 — no explicit max_connections override exists). Each engine caps
# itself at pool_size + max_overflow simultaneous connections; DB_POOL_SIZE /
# DB_MAX_OVERFLOW default to 5 + 5 = 10 max per process. Every process that
# imports this module and opens connections:
#   - `app` service:       2 uvicorn workers (docker-compose.yml --workers 2),
#                           10 each                                    = 20
#   - `scheduler` service: 1 process (full app, single uvicorn worker), 10  = 10
#   - `enrichment-worker`: 1 process, 10                                    = 10
#   - 3 host workers (avail-nc-worker, avail-ics-worker, avail-tbf-worker),
#     each one long-running process importing this module, 10 each     = 30
#   Total: 20 + 10 + 10 + 30 = 70, leaving 30 connections of headroom under
#   max_connections=100 for psql/admin sessions, one-off scripts, and Alembic
#   migrations. Re-run this arithmetic before raising DB_POOL_SIZE/
#   DB_MAX_OVERFLOW or the uvicorn --workers count.
engine = _make_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)

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
