"""Database connection and session factory.

All naive datetimes from PostgreSQL are auto-tagged as UTC via event
listener to prevent naive-vs-aware comparison errors.
"""

from datetime import datetime, timezone

from sqlalchemy import create_engine, event, DateTime, TypeDecorator
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


engine = create_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={"connect_timeout": 10},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@event.listens_for(engine, "connect")
def _set_timezone(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("SET timezone = 'UTC'")
    cursor.close()


@event.listens_for(SessionLocal, "loaded_as_persistent")
def _make_datetimes_aware(session, instance):
    for key in instance.__class__.__table__.columns.keys():
        val = getattr(instance, key, None)
        if isinstance(val, datetime) and val.tzinfo is None:
            try:
                setattr(instance, key, val.replace(tzinfo=timezone.utc))
            except Exception:
                pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
