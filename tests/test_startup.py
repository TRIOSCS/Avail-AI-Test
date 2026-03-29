"""
test_startup.py -- Tests for app/startup.py

Covers: TESTING guard, _exec error handling, _create_default_user_if_env_set,
_seed_admin_user_if_env_set, _create_count_triggers, _create_fts_triggers, _backfill_fts,
_seed_system_config, _seed_site_contacts, _backfill_normalized_mpn,
_backfill_sighting_offer_normalized_mpn, _backfill_sighting_vendor_normalized,
_backfill_company_counts, _analyze_hot_tables, _backfill_proactive_offer_qty,
run_startup_migrations (non-testing mode).

Called by: pytest
Depends on: app/startup.py, conftest fixtures
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sqltext
from sqlalchemy.pool import StaticPool

from app.startup import _exec, run_startup_migrations


def _make_sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


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
        """Creation error is logged and re-raised (M6: critical seed failures
        propagate)."""
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
            with pytest.raises(RuntimeError, match="DB error"):
                _create_default_user_if_env_set()


class TestSeedVinodUser:
    """Lines 102, 112-114, 117: _seed_admin_user_if_env_set logic."""

    @patch("app.startup.SessionLocal")
    def test_creates_vinod_user(self, mock_sl, db_session):
        """Creates Vinod admin user when not present."""
        from app.startup import _seed_admin_user_if_env_set

        mock_sl.return_value = db_session
        _seed_admin_user_if_env_set()

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="vinod@trioscs.com").first()
        assert u is not None
        assert u.role == "admin"

    @patch("app.startup.SessionLocal")
    def test_skips_existing_vinod(self, mock_sl, db_session):
        """Does not duplicate Vinod user."""
        from app.models.auth import User
        from app.startup import _seed_admin_user_if_env_set

        mock_sl.return_value = db_session
        existing = User(email="vinod@trioscs.com", name="Vinod", role="admin")
        db_session.add(existing)
        db_session.commit()

        _seed_admin_user_if_env_set()
        count = db_session.query(User).filter_by(email="vinod@trioscs.com").count()
        assert count == 1

    def test_seed_vinod_with_passed_db(self, db_session):
        """When db is passed directly, does not create/close own session."""
        from app.startup import _seed_admin_user_if_env_set

        _seed_admin_user_if_env_set(db=db_session)

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="vinod@trioscs.com").first()
        assert u is not None

    @patch("app.startup.SessionLocal")
    def test_seed_vinod_handles_error(self, mock_sl):
        """DB error is rolled back and re-raised."""
        from app.startup import _seed_admin_user_if_env_set

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.add.side_effect = RuntimeError("DB error")
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        with pytest.raises(RuntimeError, match="DB error"):
            _seed_admin_user_if_env_set()
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
        from sqlalchemy.exc import OperationalError

        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_conn.execute.side_effect = OperationalError("select", {}, Exception("DB gone"))
        mock_conn.rollback = MagicMock()

        _backfill_proactive_offer_qty()

    def test_null_line_items_skipped(self):
        """Offers with null line_items are skipped."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"))
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"))
            conn.execute(
                sqltext(
                    "CREATE TABLE proactive_offers "
                    "(id INTEGER PRIMARY KEY, line_items TEXT, total_sell REAL, total_cost REAL)"
                )
            )
            conn.execute(sqltext("INSERT INTO requirements (id, target_qty) VALUES (1, 50)"))
            conn.execute(sqltext("INSERT INTO proactive_matches (id, requirement_id) VALUES (10, 1)"))
            conn.execute(
                sqltext("INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, NULL, 0, 0)")
            )
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

    def test_item_without_match_id_in_target_map(self):
        """Line items with match_id not in target_map use original qty."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"))
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"))
            conn.execute(
                sqltext(
                    "CREATE TABLE proactive_offers "
                    "(id INTEGER PRIMARY KEY, line_items TEXT, total_sell REAL, total_cost REAL)"
                )
            )
            conn.execute(sqltext("INSERT INTO requirements (id, target_qty) VALUES (1, 50)"))
            conn.execute(sqltext("INSERT INTO proactive_matches (id, requirement_id) VALUES (10, 1)"))
            items = json.dumps(
                [
                    {
                        "match_id": 999,
                        "qty": 200,
                        "unit_price": 5.0,
                    }
                ]
            )
            conn.execute(
                sqltext(
                    "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, :items, 1000, 1000)"
                ),
                {"items": items},
            )
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

        with eng.connect() as conn:
            row = conn.execute(sqltext("SELECT total_sell, total_cost FROM proactive_offers WHERE id = 1")).fetchone()
            assert row[0] == 1000  # unchanged

    def test_fixes_offer_quantities_real_db(self):
        """Offers with qty > target_qty get corrected (real SQLite tables)."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"))
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"))
            conn.execute(
                sqltext(
                    "CREATE TABLE proactive_offers "
                    "(id INTEGER PRIMARY KEY, line_items TEXT, total_sell REAL, total_cost REAL)"
                )
            )
            conn.execute(sqltext("INSERT INTO requirements (id, target_qty) VALUES (1, 50)"))
            conn.execute(sqltext("INSERT INTO proactive_matches (id, requirement_id) VALUES (10, 1)"))
            items = json.dumps(
                [
                    {
                        "match_id": 10,
                        "qty": 200,
                        "unit_price": 5.0,
                        "sell_price": 7.0,
                    }
                ]
            )
            conn.execute(
                sqltext(
                    "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, :items, 1400, 1000)"
                ),
                {"items": items},
            )
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

        with eng.connect() as conn:
            row = conn.execute(
                sqltext("SELECT line_items, total_sell, total_cost FROM proactive_offers WHERE id = 1")
            ).fetchone()
            updated_items = json.loads(row[0])
            assert updated_items[0]["qty"] == 50
            assert row[1] == 350.0
            assert row[2] == 250.0


class TestCreateDefaultUserDefaultRole:
    """Additional _create_default_user_if_env_set coverage."""

    def test_default_role_is_admin(self):
        """Without DEFAULT_USER_ROLE, role defaults to 'admin'."""
        from app.startup import _create_default_user_if_env_set

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        env = {
            "DEFAULT_USER_EMAIL": "admin@example.com",
            "DEFAULT_USER_PASSWORD": "secret",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("app.startup.SessionLocal", return_value=mock_session),
        ):
            os.environ.pop("DEFAULT_USER_ROLE", None)
            _create_default_user_if_env_set()

        created_user = mock_session.add.call_args[0][0]
        assert created_user.role == "admin"


class TestExecAdditional:
    """Additional _exec scenarios with INSERT and failure recovery."""

    def test_exec_insert_and_verify(self):
        """_exec can INSERT data into a table."""
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE test_exec (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.commit()
            _exec(conn, "INSERT INTO test_exec (id, name) VALUES (:id, :name)", {"id": 1, "name": "test"})
            row = conn.execute(sqltext("SELECT name FROM test_exec WHERE id = 1")).fetchone()
            assert row[0] == "test"

    def test_exec_failure_connection_still_usable(self):
        """After _exec fails, the connection is still usable."""
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _exec(conn, "THIS IS NOT VALID SQL")
            conn.execute(sqltext("SELECT 1"))

    def test_exec_insert_no_params(self):
        """_exec INSERT without params dict."""
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE test_np (id INTEGER PRIMARY KEY)"))
            conn.commit()
            _exec(conn, "INSERT INTO test_np (id) VALUES (42)")
            row = conn.execute(sqltext("SELECT id FROM test_np")).fetchone()
            assert row[0] == 42


class TestCreateFtsTriggers:
    """_create_fts_triggers (PG-specific, fails gracefully on SQLite)."""

    def test_create_fts_triggers_on_sqlite(self):
        from app.startup import _create_fts_triggers

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _create_fts_triggers(conn)


class TestBackfillFts:
    """_backfill_fts (PG-specific, fails gracefully on SQLite)."""

    def test_backfill_fts_on_sqlite(self):
        from app.startup import _backfill_fts

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _backfill_fts(conn)


class TestSeedSystemConfig:
    """_seed_system_config (PG-specific, fails gracefully on SQLite)."""

    def test_seed_system_config_on_sqlite(self):
        from app.startup import _seed_system_config

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _seed_system_config(conn)


class TestSeedSiteContacts:
    """_seed_site_contacts logic."""

    def test_seed_site_contacts_already_seeded(self):
        from app.startup import _seed_site_contacts

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext(
                    "CREATE TABLE site_contacts (id INTEGER PRIMARY KEY, customer_site_id INT, "
                    "full_name TEXT, title TEXT, email TEXT, phone TEXT, is_primary BOOLEAN)"
                )
            )
            conn.execute(
                sqltext(
                    "INSERT INTO site_contacts (id, customer_site_id, full_name, title, email, phone, is_primary) "
                    "VALUES (1, 1, 'Test', 'Eng', 'a@b.com', '123', 1)"
                )
            )
            conn.commit()
            _seed_site_contacts(conn)

    def test_seed_site_contacts_empty_table(self):
        from app.startup import _seed_site_contacts

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext(
                    "CREATE TABLE site_contacts (id INTEGER PRIMARY KEY, customer_site_id INT, "
                    "full_name TEXT, title TEXT, email TEXT, phone TEXT, is_primary BOOLEAN)"
                )
            )
            conn.execute(
                sqltext(
                    "CREATE TABLE customer_sites (id INTEGER PRIMARY KEY, contact_name TEXT, "
                    "contact_title TEXT, contact_email TEXT, contact_phone TEXT)"
                )
            )
            conn.execute(
                sqltext(
                    "INSERT INTO customer_sites (id, contact_name, contact_title, contact_email, contact_phone) "
                    "VALUES (1, 'Jane', 'Eng', 'jane@x.com', '555')"
                )
            )
            conn.commit()
            _seed_site_contacts(conn)
            row = conn.execute(sqltext("SELECT full_name FROM site_contacts")).fetchone()
            assert row[0] == "Jane"

    def test_seed_site_contacts_exception(self):
        from app.startup import _seed_site_contacts

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _seed_site_contacts(conn)


class TestBackfillNormalizedMpn:
    """_backfill_normalized_mpn logic."""

    def test_backfill_requirements_and_material_cards(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, 'LM-317T', NULL)")
            )
            conn.execute(
                sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, 'LM-317T', NULL)")
            )
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()
        with eng.connect() as conn:
            req_row = conn.execute(sqltext("SELECT normalized_mpn FROM requirements WHERE id = 1")).fetchone()
            assert req_row[0] == "lm317t"
            mc_row = conn.execute(sqltext("SELECT normalized_mpn FROM material_cards WHERE id = 1")).fetchone()
            assert mc_row[0] == "lm317t"

    def test_backfill_requirements_exception(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_material_cards_exception(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_skips_empty_mpn(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, '---', NULL)"))
            conn.execute(
                sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, '---', NULL)")
            )
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()
        with eng.connect() as conn:
            req = conn.execute(sqltext("SELECT normalized_mpn FROM requirements WHERE id = 1")).fetchone()
            assert req[0] is None

    def test_backfill_material_cards_duplicate_skipped(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, 'LM317T', 'lm317t')")
            )
            conn.execute(
                sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (2, 'LM-317T', NULL)")
            )
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()
        with eng.connect() as conn:
            mc = conn.execute(sqltext("SELECT normalized_mpn FROM material_cards WHERE id = 2")).fetchone()
            assert mc[0] is None

    def test_backfill_no_rows_to_update(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, 'LM317T', 'lm317t')")
            )
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_with_empty_string_mpn(self):
        """Backfill with empty-string primary_mpn exercises _key falsy branch."""
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(
                sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, '', NULL)"))
            conn.execute(sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, '', NULL)"))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()
        with eng.connect() as conn:
            req = conn.execute(sqltext("SELECT normalized_mpn FROM requirements WHERE id = 1")).fetchone()
            assert req[0] is None


class TestRunStartupMigrationsNonTesting:
    """run_startup_migrations with TESTING unset -- exercises real migration path."""

    def test_non_testing_mode_runs_all_migrations(self):
        eng = _make_sqlite_engine()
        original = os.environ.pop("TESTING", None)
        try:
            with (
                patch("app.startup.engine", eng),
                patch("app.startup._create_fts_triggers") as m_fts,
                patch("app.startup._backfill_fts") as m_bfts,
                patch("app.startup._seed_system_config") as m_seed,
                patch("app.startup._seed_site_contacts") as m_site,
                patch("app.startup._seed_manufacturers"),
                patch("app.startup._create_count_triggers") as m_ct,
                patch("app.startup._backfill_company_counts") as m_bc,
                patch("app.startup._analyze_hot_tables") as m_analyze,
                patch("app.startup._backfill_normalized_mpn") as m_bfill,
                patch("app.startup._backfill_sighting_offer_normalized_mpn") as m_so,
                patch("app.startup._backfill_sighting_vendor_normalized") as m_sv,
                patch("app.startup._backfill_proactive_offer_qty") as m_pq,
                patch("app.startup._backfill_ticket_defaults"),
                patch("app.startup._exec") as m_exec,
                patch("app.startup._seed_admin_user_if_env_set") as m_vinod,
                patch("app.startup._seed_agent_user"),
                patch("app.startup._seed_commodity_schemas"),
            ):
                run_startup_migrations()
                m_fts.assert_called_once()
                m_bfts.assert_called_once()
                m_seed.assert_called_once()
                m_site.assert_called_once()
                m_ct.assert_called_once()
                m_bc.assert_called_once()
                m_analyze.assert_called_once()
                m_bfill.assert_called_once()
                m_so.assert_called_once()
                m_sv.assert_called_once()
                m_pq.assert_called_once()
                m_vinod.assert_called_once()
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            else:
                os.environ["TESTING"] = "1"


class TestBackfillSightingOfferNormalizedMpn:
    """_backfill_sighting_offer_normalized_mpn logic."""

    def test_backfill_sightings_and_offers(self):
        """Rows with NULL normalized_mpn get updated from mpn_matched / mpn."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (1, 'LM-317T', NULL)"))
            conn.execute(
                sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (2, 'RC-0805 FR', NULL)")
            )
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (1, 'SN-74HC595', NULL)"))
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (2, 'ATmega328P', NULL)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()

        with eng.connect() as conn:
            s1 = conn.execute(sqltext("SELECT normalized_mpn FROM sightings WHERE id = 1")).fetchone()
            assert s1[0] == "lm317t"
            s2 = conn.execute(sqltext("SELECT normalized_mpn FROM sightings WHERE id = 2")).fetchone()
            assert s2[0] == "rc0805fr"
            o1 = conn.execute(sqltext("SELECT normalized_mpn FROM offers WHERE id = 1")).fetchone()
            assert o1[0] == "sn74hc595"
            o2 = conn.execute(sqltext("SELECT normalized_mpn FROM offers WHERE id = 2")).fetchone()
            assert o2[0] == "atmega328p"

    def test_key_with_empty_string(self):
        """_key('') returns '' so row is skipped (not updated)."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (1, '  ', NULL)"))
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (1, '---', NULL)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()

        with eng.connect() as conn:
            s = conn.execute(sqltext("SELECT normalized_mpn FROM sightings WHERE id = 1")).fetchone()
            assert s[0] is None
            o = conn.execute(sqltext("SELECT normalized_mpn FROM offers WHERE id = 1")).fetchone()
            assert o[0] is None

    def test_key_with_all_special_chars(self):
        """_key('!!!') returns '' -- exercises the falsy branch."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (1, '!!!', NULL)"))
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (1, '!!!', NULL)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()

        with eng.connect() as conn:
            s = conn.execute(sqltext("SELECT normalized_mpn FROM sightings WHERE id = 1")).fetchone()
            assert s[0] is None
            o = conn.execute(sqltext("SELECT normalized_mpn FROM offers WHERE id = 1")).fetchone()
            assert o[0] is None

    def test_no_rows_to_backfill(self):
        """When no rows have NULL normalized_mpn, the function exits cleanly."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(
                sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (1, 'LM317', 'lm317')")
            )
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (1, 'LM317', 'lm317')"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()

    def test_sightings_exception_path(self):
        """If sightings table doesn't exist, the exception path is taken."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (1, 'LM317', NULL)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()

        with eng.connect() as conn:
            o = conn.execute(sqltext("SELECT normalized_mpn FROM offers WHERE id = 1")).fetchone()
            assert o[0] == "lm317"

    def test_offers_exception_path(self):
        """If offers table doesn't exist, the exception path is taken."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (1, 'LM317', NULL)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()

        with eng.connect() as conn:
            s = conn.execute(sqltext("SELECT normalized_mpn FROM sightings WHERE id = 1")).fetchone()
            assert s[0] == "lm317"


class TestBackfillSightingVendorNormalized:
    """_backfill_sighting_vendor_normalized logic."""

    def test_backfill_vendor_names(self):
        """Rows with NULL vendor_name_normalized get updated."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext(
                    "CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT, vendor_name_normalized TEXT)"
                )
            )
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (1, 'Acme Inc.', NULL)")
            )
            conn.execute(
                sqltext(
                    "INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (2, 'GlobalParts LLC', NULL)"
                )
            )
            conn.commit()

        with (
            patch("app.startup.engine", eng),
            patch("app.vendor_utils.normalize_vendor_name", side_effect=lambda n: n.lower().replace(" ", "")),
        ):
            _backfill_sighting_vendor_normalized()

        with eng.connect() as conn:
            r1 = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 1")).fetchone()
            assert r1[0] == "acmeinc."
            r2 = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 2")).fetchone()
            assert r2[0] == "globalpartsllc"

    def test_column_not_exists_returns_early(self):
        """If vendor_name_normalized column doesn't exist, function returns without
        error."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_vendor_normalized()

    def test_no_rows_to_update(self):
        """When no rows have NULL vendor_name_normalized, function exits cleanly."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext(
                    "CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT, vendor_name_normalized TEXT)"
                )
            )
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (1, 'Acme', 'acme')")
            )
            conn.commit()

        with (
            patch("app.startup.engine", eng),
            patch("app.vendor_utils.normalize_vendor_name", side_effect=lambda n: n.lower()),
        ):
            _backfill_sighting_vendor_normalized()

    def test_normalize_returns_empty_skips_row(self):
        """If normalize_vendor_name returns empty, row is not updated."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext(
                    "CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT, vendor_name_normalized TEXT)"
                )
            )
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (1, '???', NULL)")
            )
            conn.commit()

        call_count = 0

        def normalize_then_fail(name):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ""
            raise RuntimeError("stop loop")

        with (
            patch("app.startup.engine", eng),
            patch("app.vendor_utils.normalize_vendor_name", side_effect=normalize_then_fail),
        ):
            _backfill_sighting_vendor_normalized()

        with eng.connect() as conn:
            r = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 1")).fetchone()
            assert r[0] is None

    def test_exception_during_batch_breaks_loop(self):
        """If an exception occurs during batch processing, loop breaks cleanly."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext(
                    "CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT, vendor_name_normalized TEXT)"
                )
            )
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (1, 'Acme', NULL)")
            )
            conn.commit()

        def exploding_normalize(name):
            raise RuntimeError("boom")

        with (
            patch("app.startup.engine", eng),
            patch("app.vendor_utils.normalize_vendor_name", side_effect=exploding_normalize),
        ):
            _backfill_sighting_vendor_normalized()

        with eng.connect() as conn:
            r = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 1")).fetchone()
            assert r[0] is None

    def test_logs_total_when_rows_updated(self):
        """When rows are updated, the total is logged."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext(
                    "CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT, vendor_name_normalized TEXT)"
                )
            )
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (1, 'Acme', NULL)")
            )
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (2, 'Beta Corp', NULL)")
            )
            conn.commit()

        with (
            patch("app.startup.engine", eng),
            patch("app.vendor_utils.normalize_vendor_name", side_effect=lambda n: n.lower()),
        ):
            _backfill_sighting_vendor_normalized()

        with eng.connect() as conn:
            r1 = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 1")).fetchone()
            assert r1[0] == "acme"
            r2 = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 2")).fetchone()
            assert r2[0] == "beta corp"
