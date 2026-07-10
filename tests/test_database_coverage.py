"""Tests for app/database.py — SQLAlchemy engine setup and session factory.

Targets missing branches to bring coverage from 61% to 85%+.
Covers UTCDateTime type decorator and get_db generator.

Called by: pytest
Depends on: app.database
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import engine  # noqa: F401


class TestUTCDateTime:
    """Tests for UTCDateTime custom TypeDecorator."""

    def test_process_result_value_none_returns_none(self):
        """None input returns None unchanged."""
        from app.database import UTCDateTime

        utc_type = UTCDateTime()
        result = utc_type.process_result_value(None, dialect=None)
        assert result is None

    def test_process_result_value_naive_datetime_gets_utc(self):
        """Naive datetime gets UTC timezone attached."""
        from app.database import UTCDateTime

        utc_type = UTCDateTime()
        naive = datetime(2026, 1, 15, 12, 0, 0)
        assert naive.tzinfo is None

        result = utc_type.process_result_value(naive, dialect=None)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_process_result_value_aware_datetime_unchanged(self):
        """Datetime with existing tzinfo is returned unchanged."""
        from app.database import UTCDateTime

        utc_type = UTCDateTime()
        aware = datetime(2026, 3, 15, 9, 30, 0, tzinfo=UTC)

        result = utc_type.process_result_value(aware, dialect=None)
        assert result == aware
        assert result.tzinfo == UTC

    def test_utcdatetime_cache_ok(self):
        """UTCDateTime has cache_ok = True for SQLAlchemy query caching."""
        from app.database import UTCDateTime

        assert UTCDateTime.cache_ok is True

    def test_utcdatetime_impl_is_datetime(self):
        """UTCDateTime wraps the DateTime type."""
        from sqlalchemy import DateTime

        from app.database import UTCDateTime

        assert UTCDateTime.impl is DateTime


class TestGetDb:
    """Tests for get_db() session generator."""

    def test_get_db_yields_session(self):
        """get_db yields a database session."""
        from app.database import get_db

        gen = get_db()
        session = next(gen)
        assert session is not None
        try:
            next(gen)
        except StopIteration:
            pass
        finally:
            try:
                session.close()
            except Exception:
                pass

    def test_get_db_closes_on_normal_exit(self):
        """get_db closes the session on normal completion."""

        mock_session = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            from app.database import get_db

            gen = get_db()
            session = next(gen)
            assert session is mock_session
            try:
                next(gen)
            except StopIteration:
                pass

        mock_session.close.assert_called_once()

    def test_get_db_rollback_on_exception(self):
        """get_db rolls back the session when an exception is raised."""

        mock_session = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            from app.database import get_db

            gen = get_db()
            _ = next(gen)
            try:
                gen.throw(RuntimeError("DB error"))
            except RuntimeError:
                pass

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    def test_get_db_reraises_exception(self):
        """get_db re-raises exceptions after rollback."""

        mock_session = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            from app.database import get_db

            gen = get_db()
            next(gen)
            with pytest.raises(ValueError, match="test error"):
                gen.throw(ValueError("test error"))


class TestSessionLocal:
    """Tests for SessionLocal factory."""

    def test_session_local_creates_session(self):
        """SessionLocal() returns a usable session."""
        from app.database import SessionLocal

        session = SessionLocal()
        assert session is not None
        session.close()

    def test_engine_exists(self):
        """Engine is created and accessible."""
        from app.database import engine as db_engine

        assert db_engine is not None


class TestDatabaseModule:
    """Tests for module-level database configuration."""

    def test_database_url_uses_sqlite_in_tests(self):
        """In test environment, database URL is SQLite in-memory."""
        from app.config import settings

        assert "sqlite" in settings.database_url

    def test_session_local_is_sessionmaker(self):
        """SessionLocal is a sessionmaker factory."""
        from sqlalchemy.orm import sessionmaker

        from app.database import SessionLocal

        assert isinstance(SessionLocal, sessionmaker)


class TestMakeEngine:
    """Exercises the real engine-construction branches in app/database.py via the
    extracted _make_engine() factory.

    Earlier tests here reconstructed the kwargs inline and never touched app.database,
    so the production branch was effectively untested (CRIT-TEST-2). These call
    _make_engine() directly — with create_engine patched so the branch logic is asserted
    without a real connection and without rebuilding the shared engine.
    """

    def test_postgresql_branch_passes_pool_and_timeout_args(self):
        """A postgresql:// URL gets the production pool settings and the
        statement_timeout / lock_timeout connect option."""
        import app.database as database

        with patch.object(database, "create_engine") as mock_create:
            database._make_engine("postgresql://user:pass@localhost/testdb")

        assert mock_create.call_count == 1
        args, kwargs = mock_create.call_args
        assert args[0] == "postgresql://user:pass@localhost/testdb"
        assert kwargs["pool_size"] == 20
        assert kwargs["max_overflow"] == 20
        assert kwargs["pool_timeout"] == 10
        assert kwargs["pool_pre_ping"] is True
        assert kwargs["pool_recycle"] == 1800
        assert "statement_timeout" in kwargs["connect_args"]["options"]
        assert "lock_timeout" in kwargs["connect_args"]["options"]

    def test_sqlite_branch_uses_static_pool(self):
        """A sqlite:// URL builds a StaticPool engine with check_same_thread off."""
        import app.database as database

        with patch.object(database, "create_engine") as mock_create:
            database._make_engine("sqlite:///tmp/test.db")

        assert mock_create.call_count == 1
        _, kwargs = mock_create.call_args
        assert kwargs["poolclass"].__name__ == "StaticPool"
        assert kwargs["connect_args"] == {"check_same_thread": False}

    def test_non_postgresql_url_omits_timeout_options(self):
        """A non-sqlite, non-postgresql URL keeps the pool args but gets no
        statement_timeout options in connect_args."""
        import app.database as database

        with patch.object(database, "create_engine") as mock_create:
            database._make_engine("mysql://user:pass@localhost/db")

        assert mock_create.call_count == 1
        _, kwargs = mock_create.call_args
        assert kwargs["pool_size"] == 20
        assert "options" not in kwargs["connect_args"]

    def test_make_engine_postgresql_builds_real_queue_pool(self):
        """Sanity check with create_engine un-patched: a postgresql:// URL
        yields a real QueuePool engine (no connection is opened)."""
        from app.database import _make_engine

        eng = _make_engine("postgresql://user:pass@localhost:5432/testdb")
        try:
            assert eng.pool.__class__.__name__ == "QueuePool"
            assert eng.pool.size() == 20
            assert eng.dialect.name == "postgresql"
        finally:
            eng.dispose()
