"""Tests for app/database.py — SQLAlchemy engine setup and session factory.

Targets missing branches to bring coverage from 61% to 85%+.
Covers UTCDateTime type decorator and get_db generator.

Called by: pytest
Depends on: app.database
"""

import importlib
import os
import sys

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
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
        assert result.tzinfo == timezone.utc
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_process_result_value_aware_datetime_unchanged(self):
        """Datetime with existing tzinfo is returned unchanged."""
        from app.database import UTCDateTime

        utc_type = UTCDateTime()
        aware = datetime(2026, 3, 15, 9, 30, 0, tzinfo=timezone.utc)

        result = utc_type.process_result_value(aware, dialect=None)
        assert result == aware
        assert result.tzinfo == timezone.utc

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


class TestDatabaseNonSQLite:
    """Tests for the PostgreSQL engine creation path (lines 38-42)."""

    def test_postgresql_branch_creates_engine_with_pool_args(self):
        """When database_url is PostgreSQL, engine is created with pool settings."""
        import app.database as db_mod

        # Save original state
        original_engine = db_mod.engine
        original_session = db_mod.SessionLocal

        mock_engine = MagicMock()
        mock_session_cls = MagicMock()

        try:
            with (
                patch("app.config.settings") as mock_settings,
                patch("sqlalchemy.create_engine", return_value=mock_engine) as mock_create,
                patch("sqlalchemy.orm.sessionmaker", return_value=mock_session_cls),
            ):
                mock_settings.database_url = "postgresql://user:pass@localhost/testdb"
                # Remove cached module and reload to execute module-level else branch
                sys.modules.pop("app.database", None)
                importlib.import_module("app.database")

            # Verify create_engine was called (for the postgres branch)
            assert mock_create.called
            call_kwargs = mock_create.call_args[1]
            assert "pool_size" in call_kwargs
            assert call_kwargs["pool_size"] == 20
        finally:
            # Restore the original module to avoid breaking other tests
            db_mod.engine = original_engine
            db_mod.SessionLocal = original_session
            sys.modules["app.database"] = db_mod

    def test_postgresql_options_added_for_postgresql_url(self):
        """PostgreSQL URL gets statement_timeout and lock_timeout options."""
        import app.database as db_mod

        original_engine = db_mod.engine
        original_session = db_mod.SessionLocal

        mock_engine = MagicMock()

        try:
            with (
                patch("app.config.settings") as mock_settings,
                patch("sqlalchemy.create_engine", return_value=mock_engine) as mock_create,
                patch("sqlalchemy.orm.sessionmaker", return_value=MagicMock()),
            ):
                mock_settings.database_url = "postgresql://user:pass@localhost/testdb"
                sys.modules.pop("app.database", None)
                importlib.import_module("app.database")

            call_kwargs = mock_create.call_args[1]
            connect_args = call_kwargs.get("connect_args", {})
            assert "options" in connect_args
            assert "statement_timeout" in connect_args["options"]
        finally:
            db_mod.engine = original_engine
            db_mod.SessionLocal = original_session
            sys.modules["app.database"] = db_mod
