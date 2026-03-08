"""
test_startup.py -- Tests for app/startup.py

Covers: TESTING guard, _exec error handling, _create_default_user_if_env_set,
_seed_vinod_user, _create_count_triggers,
_backfill_company_counts, _analyze_hot_tables, _backfill_proactive_offer_qty.

Called by: pytest
Depends on: app/startup.py, conftest fixtures
"""

import os
from unittest.mock import MagicMock, patch

from app.startup import _exec, run_startup_migrations


class TestStartupGuard:
    def test_testing_mode_skips_migrations(self):
        """TESTING=1 -> run_startup_migrations does nothing."""
        assert os.environ.get("TESTING") == "1"
        run_startup_migrations()


class TestExec:
    def test_pg_ddl_fails_on_sqlite_gracefully(self, db_session):
        """PG-specific DDL fails on SQLite but _exec swallows the error."""
        from tests.conftest import engine

        with engine.connect() as conn:
            _exec(conn, "CREATE EXTENSION IF NOT EXISTS pg_trgm")

    def test_exec_success(self, db_session):
        """_exec succeeds with a valid SQLite statement."""
        from tests.conftest import engine

        with engine.connect() as conn:
            _exec(conn, "SELECT 1")

    def test_exec_with_params(self, db_session):
        """_exec passes params correctly."""
        from tests.conftest import engine

        with engine.connect() as conn:
            _exec(conn, "SELECT :val", {"val": 42})


class TestCreateDefaultUser:
    """Lines 43, 102: _create_default_user_if_env_set logic."""

    def test_no_env_vars_does_nothing(self, db_session):
        """Without DEFAULT_USER_EMAIL/PASSWORD, function returns early."""
        from app.startup import _create_default_user_if_env_set

        env = {"DEFAULT_USER_EMAIL": "", "DEFAULT_USER_PASSWORD": ""}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("DEFAULT_USER_EMAIL", None)
            os.environ.pop("DEFAULT_USER_PASSWORD", None)
            _create_default_user_if_env_set()

    @patch("app.startup.SessionLocal")
    def test_creates_user_when_env_set(self, mock_sl, db_session):
        """Creates a user when both email and password are in env."""
        from app.startup import _create_default_user_if_env_set

        mock_sl.return_value = db_session

        env = {
            "DEFAULT_USER_EMAIL": "newadmin@test.com",
            "DEFAULT_USER_PASSWORD": "secret123",
            "DEFAULT_USER_ROLE": "admin",
        }
        with patch.dict(os.environ, env, clear=False):
            _create_default_user_if_env_set()

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="newadmin@test.com").first()
        assert u is not None
        assert u.role == "admin"
        assert "$" in u.password_hash

    @patch("app.startup.SessionLocal")
    def test_skips_if_user_already_exists(self, mock_sl, db_session, admin_user):
        """Does not create duplicate user."""
        from app.startup import _create_default_user_if_env_set

        mock_sl.return_value = db_session

        env = {
            "DEFAULT_USER_EMAIL": admin_user.email,
            "DEFAULT_USER_PASSWORD": "secret123",
        }
        with patch.dict(os.environ, env, clear=False):
            _create_default_user_if_env_set()

        from app.models.auth import User

        count = db_session.query(User).filter_by(email=admin_user.email).count()
        assert count == 1

    @patch("app.startup.SessionLocal")
    def test_handles_creation_error(self, mock_sl, db_session):
        """Handles exception during user creation gracefully."""
        from app.startup import _create_default_user_if_env_set

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.add.side_effect = RuntimeError("DB error")
        mock_db.commit = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        env = {
            "DEFAULT_USER_EMAIL": "fail@test.com",
            "DEFAULT_USER_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            _create_default_user_if_env_set()


class TestSeedVinodUser:
    """Lines 102, 112-114, 117: _seed_vinod_user logic."""

    @patch("app.startup.SessionLocal")
    def test_creates_vinod_user(self, mock_sl, db_session):
        """Creates Vinod admin user when not present."""
        from app.startup import _seed_vinod_user

        mock_sl.return_value = db_session
        _seed_vinod_user()

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="vinod@trioscs.com").first()
        assert u is not None
        assert u.role == "admin"

    @patch("app.startup.SessionLocal")
    def test_skips_existing_vinod(self, mock_sl, db_session):
        """Does not duplicate Vinod user."""
        from app.models.auth import User
        from app.startup import _seed_vinod_user

        mock_sl.return_value = db_session
        existing = User(email="vinod@trioscs.com", name="Vinod", role="admin")
        db_session.add(existing)
        db_session.commit()

        _seed_vinod_user()
        count = db_session.query(User).filter_by(email="vinod@trioscs.com").count()
        assert count == 1

    def test_seed_vinod_with_passed_db(self, db_session):
        """When db is passed directly, does not create/close own session."""
        from app.startup import _seed_vinod_user

        _seed_vinod_user(db=db_session)

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="vinod@trioscs.com").first()
        assert u is not None

    @patch("app.startup.SessionLocal")
    def test_seed_vinod_handles_error(self, mock_sl):
        """Handles DB error gracefully (lines 112-114)."""
        from app.startup import _seed_vinod_user

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.add.side_effect = RuntimeError("DB error")
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        _seed_vinod_user()
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestCreateCountTriggers:
    """Lines 436-509: _create_count_triggers (PG-specific, test error path on SQLite)."""

    def test_count_triggers_fail_gracefully_on_sqlite(self, db_session):
        """PG trigger DDL fails on SQLite but _exec handles it."""
        from app.startup import _create_count_triggers
        from tests.conftest import engine

        with engine.connect() as conn:
            _create_count_triggers(conn)


class TestBackfillCompanyCounts:
    """Lines 524-533: _backfill_company_counts."""

    def test_backfill_counts_fail_gracefully_on_sqlite(self, db_session):
        """PG-specific UPDATE with subquery may fail on SQLite -- handled by _exec."""
        from app.startup import _backfill_company_counts
        from tests.conftest import engine

        with engine.connect() as conn:
            _backfill_company_counts(conn)


class TestAnalyzeHotTables:
    """Lines 548-549: _analyze_hot_tables."""

    def test_analyze_fails_gracefully_on_sqlite(self, db_session):
        """ANALYZE on PG tables fails gracefully on SQLite."""
        from app.startup import _analyze_hot_tables
        from tests.conftest import engine

        with engine.connect() as conn:
            _analyze_hot_tables(conn)


class TestBackfillProactiveOfferQty:
    """Lines 562-634: _backfill_proactive_offer_qty."""

    @patch("app.startup.engine")
    def test_no_target_map_returns_early(self, mock_engine, db_session):
        """When no matches have target_qty, returns early."""
        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = []

        _backfill_proactive_offer_qty()
        assert mock_conn.execute.call_count == 1

    @patch("app.startup.engine")
    def test_fixes_offer_quantities(self, mock_engine, db_session):
        """Recalculates offer totals when target_qty < qty_available."""
        import json

        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn

        match_rows = [(10, 50)]
        line_items = json.dumps([{"match_id": 10, "qty": 100, "sell_price": 1.0, "unit_price": 0.5}])
        offers = [(1, line_items)]

        mock_conn.execute.return_value.fetchall.side_effect = [match_rows, offers]

        _backfill_proactive_offer_qty()
        assert mock_conn.execute.call_count >= 3

    @patch("app.startup.engine")
    def test_no_change_when_qty_matches(self, mock_engine, db_session):
        """No UPDATE when qty already <= target_qty."""
        import json

        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn

        match_rows = [(10, 100)]
        line_items = json.dumps([{"match_id": 10, "qty": 50, "sell_price": 1.0, "unit_price": 0.5}])
        offers = [(1, line_items)]

        mock_conn.execute.return_value.fetchall.side_effect = [match_rows, offers]

        _backfill_proactive_offer_qty()
        # 2 queries (match_rows + offers), no UPDATE since qty (50) <= target (100)
        assert mock_conn.execute.call_count == 2

    @patch("app.startup.engine")
    def test_handles_error_gracefully(self, mock_engine):
        """Catches and logs exceptions during backfill."""
        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_conn.execute.side_effect = RuntimeError("DB gone")
        mock_conn.rollback = MagicMock()

        _backfill_proactive_offer_qty()
