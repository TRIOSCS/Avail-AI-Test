"""Database connection and session factory.

All naive datetimes from PostgreSQL are auto-tagged as UTC via event
listener to prevent naive-vs-aware comparison errors.
"""
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .config import settings

engine = create_engine(settings.database_url, pool_size=10, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@event.listens_for(engine, "connect")
def _set_timezone(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("SET timezone = 'UTC'")
    cursor.close()


@event.listens_for(SessionLocal, "loaded_as_persistent")
def _make_datetimes_aware(session, instance):
    for attr in vars(instance):
        if attr.startswith("_"):
            continue
        val = getattr(instance, attr, None)
        if isinstance(val, datetime) and val.tzinfo is None:
            try:
                setattr(instance, attr, val.replace(tzinfo=timezone.utc))
            except Exception:
                pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
