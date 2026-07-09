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

from app.startup import _exec, _reconcile_connector_active, run_startup_migrations


def _make_sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


_CREATE_REQUIREMENTS = "CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)"
_CREATE_MATERIAL_CARDS = "CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)"
_CREATE_MPN_SIGHTINGS = "CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)"
_CREATE_OFFERS = "CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"
_CREATE_VENDOR_SIGHTINGS = (
    "CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT, vendor_name_normalized TEXT)"
)


def _mock_engine_conn(mock_engine):
    """Wire a mock engine so ``engine.connect()`` yields a context-manager conn."""
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_engine.connect.return_value = mock_conn
    return mock_conn


def _make_proactive_offer_engine(offer_insert_sql, offer_params=None):
    """SQLite engine with proactive_matches/requirements/proactive_offers seeded.

    Requirement id=1 has target_qty=50 and proactive_match id=10. The single
    proactive_offers row is inserted via the caller-supplied statement/params.
    """
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
        if offer_params is None:
            conn.execute(sqltext(offer_insert_sql))
        else:
            conn.execute(sqltext(offer_insert_sql), offer_params)
        conn.commit()
    return eng


class TestStartupGuard:
    def test_testing_mode_skips_migrations(self):
        """TESTING=1 -> run_startup_migrations does nothing."""
        assert os.environ.get("TESTING") == "1"
        run_startup_migrations()


class TestExec:
    @pytest.mark.parametrize(
        ("sql", "params"),
        [
            pytest.param("CREATE EXTENSION IF NOT EXISTS pg_trgm", None, id="pg_ddl_fails_gracefully"),
            pytest.param("SELECT 1", None, id="success"),
            pytest.param("SELECT :val", {"val": 42}, id="with_params"),
        ],
    )
    def test_exec_handles_statement(self, db_session, sql, params):
        """_exec runs valid SQLite statements and swallows PG-only DDL errors."""
        from tests.conftest import engine

        with engine.connect() as conn:
            if params is None:
                _exec(conn, sql)
            else:
                _exec(conn, sql, params)


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
    def test_default_role_is_buyer_when_role_unset(self, mock_sl, db_session):
        """With DEFAULT_USER_ROLE unset, the created user is a buyer — never an admin
        (CRIT-SEC-2: least privilege)."""
        from app.startup import _create_default_user_if_env_set

        mock_sl.return_value = db_session

        env = {
            "DEFAULT_USER_EMAIL": "defaultrole@test.com",
            "DEFAULT_USER_PASSWORD": "secret123",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("DEFAULT_USER_ROLE", None)
            _create_default_user_if_env_set()

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="defaultrole@test.com").first()
        assert u is not None
        assert u.role == "buyer"

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


class TestSeedAdminUser:
    """_seed_admin_user_if_env_set logic — env-driven, no hard-coded default admin."""

    _ENV = {"SEED_ADMIN_EMAIL": "ops@example.com", "SEED_ADMIN_NAME": "Ops"}

    @patch("app.startup.SessionLocal")
    def test_creates_admin_user_from_env(self, mock_sl, db_session):
        """Creates the env-named admin user when not present."""
        from app.startup import _seed_admin_user_if_env_set

        mock_sl.return_value = db_session
        with patch.dict("os.environ", self._ENV):
            _seed_admin_user_if_env_set()

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="ops@example.com").first()
        assert u is not None
        assert u.role == "admin"

    @patch("app.startup.SessionLocal")
    def test_skips_existing_admin(self, mock_sl, db_session):
        """Does not duplicate the admin user."""
        from app.models.auth import User
        from app.startup import _seed_admin_user_if_env_set

        mock_sl.return_value = db_session
        existing = User(email="ops@example.com", name="Ops", role="admin")
        db_session.add(existing)
        db_session.commit()

        with patch.dict("os.environ", self._ENV):
            _seed_admin_user_if_env_set()
        count = db_session.query(User).filter_by(email="ops@example.com").count()
        assert count == 1

    def test_seed_with_passed_db(self, db_session):
        """When db is passed directly, does not create/close own session."""
        from app.startup import _seed_admin_user_if_env_set

        with patch.dict("os.environ", self._ENV):
            _seed_admin_user_if_env_set(db=db_session)

        from app.models.auth import User

        u = db_session.query(User).filter_by(email="ops@example.com").first()
        assert u is not None
        # Seeded through the PASSED session with the admin role the env seed
        # promises — visible here without the helper committing its own session.
        assert u.role == "admin"
        assert u.is_active is True

    @patch("app.startup.SessionLocal")
    def test_env_unset_seeds_nothing_and_opens_no_session(self, mock_sl):
        """No SEED_ADMIN_EMAIL means no seed and no DB session (CFG-8).

        The old hard-coded default seeded an admin into every fresh install.
        """
        import os

        from app.startup import _seed_admin_user_if_env_set

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("SEED_ADMIN_EMAIL", None)
            _seed_admin_user_if_env_set()
        mock_sl.assert_not_called()

    @patch("app.startup.SessionLocal")
    def test_seed_handles_error(self, mock_sl):
        """DB error is rolled back and re-raised."""
        from app.startup import _seed_admin_user_if_env_set

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.add.side_effect = RuntimeError("DB error")
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        with patch.dict("os.environ", self._ENV), pytest.raises(RuntimeError, match="DB error"):
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

        mock_conn = _mock_engine_conn(mock_engine)
        mock_conn.execute.return_value.fetchall.return_value = []

        _backfill_proactive_offer_qty()
        assert mock_conn.execute.call_count == 1

    @patch("app.startup.engine")
    def test_fixes_offer_quantities(self, mock_engine, db_session):
        """Recalculates offer totals when target_qty < qty_available."""
        from app.startup import _backfill_proactive_offer_qty

        mock_conn = _mock_engine_conn(mock_engine)

        match_rows = [(10, 50)]
        line_items = json.dumps([{"match_id": 10, "qty": 100, "sell_price": 1.0, "unit_price": 0.5}])
        offers = [(1, line_items)]

        mock_conn.execute.return_value.fetchall.side_effect = [match_rows, offers]

        _backfill_proactive_offer_qty()
        assert mock_conn.execute.call_count >= 3

    @patch("app.startup.engine")
    def test_no_change_when_qty_matches(self, mock_engine, db_session):
        """No UPDATE when qty already <= target_qty."""
        from app.startup import _backfill_proactive_offer_qty

        mock_conn = _mock_engine_conn(mock_engine)

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

        mock_conn = _mock_engine_conn(mock_engine)
        mock_conn.execute.side_effect = OperationalError("select", {}, Exception("DB gone"))
        mock_conn.rollback = MagicMock()

        _backfill_proactive_offer_qty()

    def test_null_line_items_skipped(self):
        """Offers with null line_items are skipped."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_proactive_offer_engine(
            "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, NULL, 0, 0)"
        )

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

    def test_item_without_match_id_in_target_map(self):
        """Line items with match_id not in target_map use original qty."""
        from app.startup import _backfill_proactive_offer_qty

        items = json.dumps(
            [
                {
                    "match_id": 999,
                    "qty": 200,
                    "unit_price": 5.0,
                }
            ]
        )
        eng = _make_proactive_offer_engine(
            "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, :items, 1000, 1000)",
            {"items": items},
        )

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

        with eng.connect() as conn:
            row = conn.execute(sqltext("SELECT total_sell, total_cost FROM proactive_offers WHERE id = 1")).fetchone()
            assert row[0] == 1000  # unchanged

    def test_fixes_offer_quantities_real_db(self):
        """Offers with qty > target_qty get corrected (real SQLite tables)."""
        from app.startup import _backfill_proactive_offer_qty

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
        eng = _make_proactive_offer_engine(
            "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, :items, 1400, 1000)",
            {"items": items},
        )

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
    """Additional _create_default_user_if_env_set coverage.

    P6.3: converted from a whole-session MagicMock (``query().filter_by().first()``
    stubbed to always return None, so the REAL "does this email already exist" query
    was never exercised) to the real ``db_session`` fixture — the same
    ``patch("app.startup.SessionLocal") ... mock_sl.return_value = db_session`` pattern
    ``TestCreateDefaultUser`` already uses. This is functionally the same scenario as
    ``TestCreateDefaultUser.test_default_role_is_buyer_when_role_unset`` (kept
    separately per this class's own historical grouping), now asserting against the
    real persisted User row instead of introspecting ``mock_session.add.call_args``.
    """

    @patch("app.startup.SessionLocal")
    def test_default_role_is_buyer(self, mock_sl, db_session):
        """Without DEFAULT_USER_ROLE, role defaults to least-privilege 'buyer', never
        'admin' (CRIT-SEC-2)."""
        from app.models.auth import User
        from app.startup import _create_default_user_if_env_set

        mock_sl.return_value = db_session

        env = {
            "DEFAULT_USER_EMAIL": "default@example.com",
            "DEFAULT_USER_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("DEFAULT_USER_ROLE", None)
            _create_default_user_if_env_set()

        created_user = db_session.query(User).filter_by(email="default@example.com").first()
        assert created_user is not None
        assert created_user.role == "buyer"


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
            conn.execute(sqltext(_CREATE_REQUIREMENTS))
            conn.execute(sqltext(_CREATE_MATERIAL_CARDS))
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
            conn.execute(sqltext(_CREATE_MATERIAL_CARDS))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_material_cards_exception(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(_CREATE_REQUIREMENTS))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_skips_empty_mpn(self):
        from app.startup import _backfill_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(_CREATE_REQUIREMENTS))
            conn.execute(sqltext(_CREATE_MATERIAL_CARDS))
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
            conn.execute(sqltext(_CREATE_REQUIREMENTS))
            conn.execute(sqltext(_CREATE_MATERIAL_CARDS))
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
            conn.execute(sqltext(_CREATE_REQUIREMENTS))
            conn.execute(sqltext(_CREATE_MATERIAL_CARDS))
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
            conn.execute(sqltext(_CREATE_REQUIREMENTS))
            conn.execute(sqltext(_CREATE_MATERIAL_CARDS))
            conn.execute(sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, '', NULL)"))
            conn.execute(sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, '', NULL)"))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()
        with eng.connect() as conn:
            req = conn.execute(sqltext("SELECT normalized_mpn FROM requirements WHERE id = 1")).fetchone()
            assert req[0] is None


class TestRunStartupMigrationsNonTesting:
    """run_startup_migrations with TESTING unset -- exercises the FAST pre-yield path
    only (P2.7 split the SLOW backfills/ANALYZE into run_deferred_startup_backfills,
    covered by TestRunDeferredStartupBackfills below)."""

    def test_non_testing_mode_runs_fast_migrations_only(self):
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
                patch("app.startup._backfill_offer_vendor_normalized") as m_ov,
                patch("app.startup._backfill_proactive_offer_qty") as m_pq,
                patch("app.startup._backfill_ticket_defaults"),
                patch("app.startup._exec") as m_exec,
                patch("app.startup._seed_admin_user_if_env_set") as m_vinod,
                patch("app.startup._seed_agent_user"),
                patch("app.startup._seed_commodity_schemas"),
            ):
                run_startup_migrations()
                # FAST ops run synchronously, pre-yield.
                m_fts.assert_called_once()
                m_seed.assert_called_once()
                m_ct.assert_called_once()
                m_vinod.assert_called_once()
                # SLOW ops moved to run_deferred_startup_backfills — NOT called here.
                m_bfts.assert_not_called()
                m_site.assert_not_called()
                m_bc.assert_not_called()
                m_analyze.assert_not_called()
                m_bfill.assert_not_called()
                m_so.assert_not_called()
                m_sv.assert_not_called()
                m_ov.assert_not_called()
                m_pq.assert_not_called()
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            else:
                os.environ["TESTING"] = "1"


class TestRunDeferredStartupBackfills:
    """run_deferred_startup_backfills — the P2.7 SLOW-op phase moved off the pre-yield
    critical path."""

    def test_testing_mode_skips_and_marks_completed(self):
        """TESTING=1 -> no-op, but the state still flips to COMPLETED (there is nothing
        to wait for under TESTING)."""
        import app.startup as startup_mod
        from app.constants import DeferredBackfillState
        from app.startup import run_deferred_startup_backfills

        assert os.environ.get("TESTING") == "1"
        startup_mod.deferred_backfills_state = DeferredBackfillState.RUNNING
        run_deferred_startup_backfills()
        assert startup_mod.deferred_backfills_state == DeferredBackfillState.COMPLETED

    def test_non_testing_runs_all_slow_ops_and_marks_completed(self):
        """With TESTING unset, every SLOW op runs exactly once and the state flips to
        COMPLETED when the phase finishes successfully."""
        import app.startup as startup_mod
        from app.constants import DeferredBackfillState
        from app.startup import run_deferred_startup_backfills

        eng = _make_sqlite_engine()
        original = os.environ.pop("TESTING", None)
        startup_mod.deferred_backfills_state = DeferredBackfillState.RUNNING
        try:
            with (
                patch("app.startup.engine", eng),
                patch("app.startup._backfill_fts") as m_bfts,
                patch("app.startup._seed_site_contacts") as m_site,
                patch("app.startup._backfill_company_counts") as m_bc,
                patch("app.startup._maybe_analyze_hot_tables") as m_analyze,
                patch("app.startup._backfill_normalized_mpn") as m_bfill,
                patch("app.startup._backfill_sighting_offer_normalized_mpn") as m_so,
                patch("app.startup._backfill_sighting_vendor_normalized") as m_sv,
                patch("app.startup._backfill_offer_vendor_normalized") as m_ov,
                patch("app.startup._backfill_proactive_offer_qty") as m_pq,
                patch("app.startup._backfill_ticket_defaults") as m_td,
                patch("app.startup._backfill_material_cards") as m_mc,
                patch("app.startup._backfill_sweep_cooldown") as m_sc,
                patch("app.startup._complete_reverted_active_plans") as m_cp,
                patch("app.startup._warn_non_canonical_categories") as m_warn,
            ):
                run_deferred_startup_backfills()
                for mock in (
                    m_bfts,
                    m_site,
                    m_bc,
                    m_analyze,
                    m_bfill,
                    m_so,
                    m_sv,
                    m_ov,
                    m_pq,
                    m_td,
                    m_mc,
                    m_sc,
                    m_cp,
                    m_warn,
                ):
                    mock.assert_called_once()
            assert startup_mod.deferred_backfills_state == DeferredBackfillState.COMPLETED
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            else:
                os.environ["TESTING"] = "1"
            startup_mod.deferred_backfills_state = DeferredBackfillState.COMPLETED

    def test_marks_failed_on_unexpected_exception(self):
        """A bug that lets an exception escape one deferred op must flip the state to
        FAILED (never silently report COMPLETED/ready) — the except: branch always re-
        logs and sets FAILED before re-raising."""
        import app.startup as startup_mod
        from app.constants import DeferredBackfillState
        from app.startup import run_deferred_startup_backfills

        eng = _make_sqlite_engine()
        original = os.environ.pop("TESTING", None)
        startup_mod.deferred_backfills_state = DeferredBackfillState.RUNNING
        try:
            with (
                patch("app.startup.engine", eng),
                patch("app.startup._backfill_fts", side_effect=RuntimeError("boom")),
            ):
                with pytest.raises(RuntimeError, match="boom"):
                    run_deferred_startup_backfills()
            assert startup_mod.deferred_backfills_state == DeferredBackfillState.FAILED
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            else:
                os.environ["TESTING"] = "1"
            startup_mod.deferred_backfills_state = DeferredBackfillState.COMPLETED


class TestDeferredBackfillsReadyFlag:
    """mark_deferred_backfills_pending / is_deferred_backfills_ready /
    get_deferred_backfills_state — the P2.7 readiness seam GET /health/ready reads."""

    def test_default_is_ready(self):
        import app.startup as startup_mod
        from app.constants import DeferredBackfillState
        from app.startup import is_deferred_backfills_ready

        startup_mod.deferred_backfills_state = DeferredBackfillState.COMPLETED
        assert is_deferred_backfills_ready() is True

    def test_mark_pending_flips_not_ready(self):
        import app.startup as startup_mod
        from app.constants import DeferredBackfillState
        from app.startup import is_deferred_backfills_ready, mark_deferred_backfills_pending

        startup_mod.deferred_backfills_state = DeferredBackfillState.COMPLETED
        mark_deferred_backfills_pending()
        assert is_deferred_backfills_ready() is False
        assert startup_mod.deferred_backfills_state == DeferredBackfillState.RUNNING
        startup_mod.deferred_backfills_state = DeferredBackfillState.COMPLETED  # reset

    def test_failed_state_is_not_ready(self):
        """The bug this tri-state fixes: a crashed deferred phase must report
        ready=False, not silently ready=True."""
        import app.startup as startup_mod
        from app.constants import DeferredBackfillState
        from app.startup import get_deferred_backfills_state, is_deferred_backfills_ready

        startup_mod.deferred_backfills_state = DeferredBackfillState.FAILED
        assert is_deferred_backfills_ready() is False
        assert get_deferred_backfills_state() == DeferredBackfillState.FAILED
        startup_mod.deferred_backfills_state = DeferredBackfillState.COMPLETED  # reset


class TestMaybeAnalyzeHotTables:
    """_maybe_analyze_hot_tables — since-last-deploy ANALYZE gate (P2.7 item 3)."""

    _CREATE_SYSTEM_CONFIG = (
        "CREATE TABLE system_config (id INTEGER PRIMARY KEY, key TEXT UNIQUE, value TEXT, description TEXT)"
    )

    def test_runs_analyze_on_first_boot_and_writes_marker(self):
        from app.startup import _maybe_analyze_hot_tables

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(self._CREATE_SYSTEM_CONFIG))
            conn.commit()

        with (
            patch.dict(os.environ, {"BUILD_COMMIT": "sha-1"}),
            patch("app.startup._analyze_hot_tables") as m_analyze,
        ):
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)
                conn.commit()
        m_analyze.assert_called_once()

        with eng.connect() as conn:
            row = conn.execute(
                sqltext("SELECT value FROM system_config WHERE key = 'startup_last_analyze_build'")
            ).fetchone()
        assert row[0] == "sha-1"

    def test_skips_on_second_boot_same_build(self):
        from app.startup import _maybe_analyze_hot_tables

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(self._CREATE_SYSTEM_CONFIG))
            conn.commit()

        with (
            patch.dict(os.environ, {"BUILD_COMMIT": "sha-1"}),
            patch("app.startup._analyze_hot_tables") as m_analyze,
        ):
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)  # first boot
                conn.commit()
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)  # second boot, same BUILD_COMMIT
                conn.commit()
        m_analyze.assert_called_once()

    def test_reruns_after_marker_cleared(self):
        from app.startup import _maybe_analyze_hot_tables

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(self._CREATE_SYSTEM_CONFIG))
            conn.commit()

        with (
            patch.dict(os.environ, {"BUILD_COMMIT": "sha-1"}),
            patch("app.startup._analyze_hot_tables") as m_analyze,
        ):
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)
                conn.commit()

        with eng.connect() as conn:
            conn.execute(sqltext("DELETE FROM system_config WHERE key = 'startup_last_analyze_build'"))
            conn.commit()

        with (
            patch.dict(os.environ, {"BUILD_COMMIT": "sha-1"}),
            patch("app.startup._analyze_hot_tables") as m_analyze,
        ):
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)  # marker cleared -> reruns
                conn.commit()
        m_analyze.assert_called_once()

    def test_reruns_after_new_deploy_build_commit_changes(self):
        from app.startup import _maybe_analyze_hot_tables

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(self._CREATE_SYSTEM_CONFIG))
            conn.commit()

        with patch.dict(os.environ, {"BUILD_COMMIT": "sha-1"}):
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)
                conn.commit()

        with patch.dict(os.environ, {"BUILD_COMMIT": "sha-2"}), patch("app.startup._analyze_hot_tables") as m_analyze:
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)  # new deploy -> reruns
                conn.commit()
        m_analyze.assert_called_once()

    def test_marker_read_failure_falls_back_to_running_analyze(self):
        """A read error on system_config (e.g. missing table) must not block the ANALYZE
        it's meant to gate — degrade to 'always run' rather than 'never run'."""
        from app.startup import _maybe_analyze_hot_tables

        eng = _make_sqlite_engine()  # no system_config table at all

        with patch.dict(os.environ, {"BUILD_COMMIT": "sha-1"}), patch("app.startup._analyze_hot_tables") as m_analyze:
            with eng.connect() as conn:
                _maybe_analyze_hot_tables(conn)
        m_analyze.assert_called_once()


class TestBackfillSightingOfferNormalizedMpn:
    """_backfill_sighting_offer_normalized_mpn logic."""

    def test_backfill_sightings_and_offers(self):
        """Rows with NULL normalized_mpn get updated from mpn_matched / mpn."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(_CREATE_MPN_SIGHTINGS))
            conn.execute(sqltext(_CREATE_OFFERS))
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
            conn.execute(sqltext(_CREATE_MPN_SIGHTINGS))
            conn.execute(sqltext(_CREATE_OFFERS))
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
            conn.execute(sqltext(_CREATE_MPN_SIGHTINGS))
            conn.execute(sqltext(_CREATE_OFFERS))
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
            conn.execute(sqltext(_CREATE_MPN_SIGHTINGS))
            conn.execute(sqltext(_CREATE_OFFERS))
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
            conn.execute(sqltext(_CREATE_OFFERS))
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
            conn.execute(sqltext(_CREATE_MPN_SIGHTINGS))
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
            conn.execute(sqltext(_CREATE_VENDOR_SIGHTINGS))
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
            conn.execute(sqltext(_CREATE_VENDOR_SIGHTINGS))
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
            conn.execute(sqltext(_CREATE_VENDOR_SIGHTINGS))
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
            conn.execute(sqltext(_CREATE_VENDOR_SIGHTINGS))
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
            conn.execute(sqltext(_CREATE_VENDOR_SIGHTINGS))
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

    def test_empty_normalizing_row_does_not_hang(self):
        """Regression: a legacy row whose vendor_name normalizes to '' (e.g. 'LLC') must
        NOT spin the backfill forever. The id cursor skips it and still processes the
        rest — no stop-loop exception hack needed (the pre-fix loop re-selected the
        never-updatable row endlessly and hung startup)."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(_CREATE_VENDOR_SIGHTINGS))
            # id=1 normalizes to '' → never updatable; id=2 is a real vendor at a higher id.
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (1, 'LLC', NULL)")
            )
            conn.execute(
                sqltext("INSERT INTO sightings (id, vendor_name, vendor_name_normalized) VALUES (2, 'Arrow', NULL)")
            )
            conn.commit()

        with (
            patch("app.startup.engine", eng),
            patch(
                "app.vendor_utils.normalize_vendor_name", side_effect=lambda n: "" if n == "LLC" else n.lower().strip()
            ),
        ):
            _backfill_sighting_vendor_normalized()  # must TERMINATE (would hang pre-fix)

        with eng.connect() as conn:
            r1 = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 1")).fetchone()
            assert r1[0] is None  # junk row skipped, stays NULL
            r2 = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 2")).fetchone()
            assert r2[0] == "arrow"  # loop advanced past the junk and processed the rest


class TestBackfillOfferVendorNormalized:
    """_backfill_offer_vendor_normalized logic (offers tab matches on the normalized
    name)."""

    _CREATE = "CREATE TABLE offers (id INTEGER PRIMARY KEY, vendor_name TEXT, vendor_name_normalized TEXT)"

    def test_backfill_updates_null_rows(self):
        """Offers with NULL vendor_name_normalized get populated from vendor_name."""
        from app.startup import _backfill_offer_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(self._CREATE))
            conn.execute(
                sqltext("INSERT INTO offers (id, vendor_name, vendor_name_normalized) VALUES (1, 'XSS Vend', NULL)")
            )
            conn.execute(
                sqltext("INSERT INTO offers (id, vendor_name, vendor_name_normalized) VALUES (2, 'Arrow', 'arrow')")
            )
            conn.commit()

        with (
            patch("app.startup.engine", eng),
            patch("app.vendor_utils.normalize_vendor_name", side_effect=lambda n: n.lower().strip()),
        ):
            _backfill_offer_vendor_normalized()

        with eng.connect() as conn:
            r1 = conn.execute(sqltext("SELECT vendor_name_normalized FROM offers WHERE id = 1")).fetchone()
            assert r1[0] == "xss vend"  # NULL row backfilled
            r2 = conn.execute(sqltext("SELECT vendor_name_normalized FROM offers WHERE id = 2")).fetchone()
            assert r2[0] == "arrow"  # already-set row untouched

    def test_column_not_exists_returns_early(self):
        """If vendor_name_normalized column doesn't exist, function returns without
        error."""
        from app.startup import _backfill_offer_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, vendor_name TEXT)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_offer_vendor_normalized()  # no raise

    def test_empty_normalizing_row_does_not_hang(self):
        """Regression: an offer whose vendor_name normalizes to '' (e.g. 'LLC') must NOT
        spin the backfill forever. The id cursor skips it and still processes the rest —
        the pre-fix loop re-selected the never-updatable row endlessly and hung startup."""
        from app.startup import _backfill_offer_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(self._CREATE))
            conn.execute(
                sqltext("INSERT INTO offers (id, vendor_name, vendor_name_normalized) VALUES (1, 'LLC', NULL)")
            )
            conn.execute(
                sqltext("INSERT INTO offers (id, vendor_name, vendor_name_normalized) VALUES (2, 'Arrow', NULL)")
            )
            conn.commit()

        with (
            patch("app.startup.engine", eng),
            patch(
                "app.vendor_utils.normalize_vendor_name", side_effect=lambda n: "" if n == "LLC" else n.lower().strip()
            ),
        ):
            _backfill_offer_vendor_normalized()  # must TERMINATE (would hang pre-fix)

        with eng.connect() as conn:
            r1 = conn.execute(sqltext("SELECT vendor_name_normalized FROM offers WHERE id = 1")).fetchone()
            assert r1[0] is None  # junk row skipped, stays NULL
            r2 = conn.execute(sqltext("SELECT vendor_name_normalized FROM offers WHERE id = 2")).fetchone()
            assert r2[0] == "arrow"  # loop advanced past the junk and processed the rest


class TestBackfillMaterialCards:
    """_backfill_material_cards logic."""

    @patch("app.startup.SessionLocal")
    def test_links_unlinked_requirement(self, mock_sl):
        """Requirement with primary_mpn but no material_card_id gets linked after
        backfill."""
        from app.startup import _backfill_material_cards

        mock_db = MagicMock()
        mock_req = MagicMock()
        mock_req.primary_mpn = "LM317T"
        mock_req.manufacturer = "TI"
        mock_req.material_card_id = None
        mock_req.substitutes = []
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_req]
        mock_sl.return_value = mock_db

        fake_card = MagicMock()
        fake_card.id = 42

        with patch("app.search_service.resolve_material_card", return_value=fake_card) as mock_resolve:
            _backfill_material_cards()
            mock_resolve.assert_called_once_with("LM317T", mock_db, manufacturer="TI")

        assert mock_req.material_card_id == 42
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.startup.SessionLocal")
    def test_fast_exit_when_all_linked(self, mock_sl):
        """When all requirements already have material_card_id, function returns
        quickly."""
        from app.startup import _backfill_material_cards

        mock_db = MagicMock()
        # Query returns empty list — all requirements already linked
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_sl.return_value = mock_db

        with patch("app.search_service.resolve_material_card") as mock_resolve:
            _backfill_material_cards()
            mock_resolve.assert_not_called()

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("app.startup.SessionLocal")
    def test_handles_dict_format_substitutes(self, mock_sl):
        """Requirement with dict-format substitutes gets cards resolved."""
        from app.startup import _backfill_material_cards

        mock_db = MagicMock()
        mock_req = MagicMock()
        mock_req.primary_mpn = "LM317T"
        mock_req.manufacturer = "TI"
        mock_req.material_card_id = None
        mock_req.substitutes = [{"mpn": "LM337T", "manufacturer": "TI"}]
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_req]
        mock_sl.return_value = mock_db

        fake_card = MagicMock()
        fake_card.id = 10

        with patch("app.search_service.resolve_material_card", return_value=fake_card) as mock_resolve:
            _backfill_material_cards()
            # Called once for primary_mpn ("LM317T") and once for substitute ("LM337T")
            assert mock_resolve.call_count == 2
            mock_resolve.assert_any_call("LM317T", mock_db, manufacturer="TI")
            mock_resolve.assert_any_call("LM337T", mock_db)

        mock_db.commit.assert_called_once()

    @patch("app.startup.SessionLocal")
    def test_exception_triggers_rollback(self, mock_sl):
        """When resolve_material_card raises, rollback happens and no crash."""
        from app.startup import _backfill_material_cards

        mock_db = MagicMock()
        mock_req = MagicMock()
        mock_req.primary_mpn = "LM317T"
        mock_req.manufacturer = "TI"
        mock_req.substitutes = []
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_req]
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        with patch("app.search_service.resolve_material_card", side_effect=RuntimeError("DB exploded")):
            _backfill_material_cards()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestWarnNonCanonicalCategories:
    """Boot residue observability: categories no commodity filter can bucket are LOGGED.

    Migration 093 normalized the known legacy aliases; anything outside both the
    canonical tree keys and that cut line silently vanishes from commodity browsing —
    the boot warning is the only place that number is visible (covers DBs already past
    093 and any post-093 vendor-taxonomy drift).
    """

    @staticmethod
    def _capture_warnings(fn):
        from loguru import logger as loguru_logger

        records: list[str] = []
        sink_id = loguru_logger.add(lambda message: records.append(str(message)), level="WARNING")
        try:
            fn()
        finally:
            loguru_logger.remove(sink_id)
        return records

    def test_warns_with_count_and_samples(self, db_session):
        from app.models import MaterialCard
        from app.startup import _warn_non_canonical_categories
        from tests.conftest import force_card_category

        # The non-canonical rows are exactly the legacy residue the @validates guard now
        # rejects on assignment, so seed them through force_card_category (Core UPDATE) as
        # a pre-guard writer would have left them — this warning's whole job is to surface
        # them. Canonical + NULL rows go through the normal constructor.
        canonical = MaterialCard(normalized_mpn="res-3", display_mpn="RES-3", category="ssd")  # not residue
        null_cat = MaterialCard(normalized_mpn="res-4", display_mpn="RES-4", category=None)  # not residue
        residue = [
            MaterialCard(normalized_mpn="res-1", display_mpn="RES-1"),
            MaterialCard(normalized_mpn="res-2", display_mpn="RES-2"),
        ]
        db_session.add_all([canonical, null_cat, *residue])
        db_session.flush()
        force_card_category(db_session, residue[0], "Totally Unknown Category")
        force_card_category(db_session, residue[1], "  Totally Unknown Category ")
        db_session.flush()

        warnings = self._capture_warnings(lambda: _warn_non_canonical_categories(db_session))
        assert any(
            "2 material_cards" in w and "totally unknown category" in w and "non-canonical" in w for w in warnings
        ), warnings

    def test_silent_when_every_category_is_canonical(self, db_session):
        from app.models import MaterialCard
        from app.startup import _warn_non_canonical_categories

        db_session.add(MaterialCard(normalized_mpn="ok-1", display_mpn="OK-1", category="dram"))
        db_session.flush()

        warnings = self._capture_warnings(lambda: _warn_non_canonical_categories(db_session))
        assert not any("non-canonical" in w for w in warnings), warnings


# ── Fix 4: startup sweep-cooldown backfill ───────────────────────────


class TestBackfillSweepCooldown:
    """_backfill_sweep_cooldown fills reclaim_blocked_until on swept rows that are
    missing it (crash window between the two commits in Phase 4 sweep)."""

    def test_null_cooldown_on_swept_row_gets_backfilled(self, db_session):
        """A swept ProspectAccount with NULL reclaim_blocked_until is backfilled."""
        from datetime import timedelta

        from app.models.prospect_account import ProspectAccount
        from app.startup import _backfill_sweep_cooldown

        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        pa = ProspectAccount(
            name="Swept Backfill",
            domain="backfill-sweep.com",
            discovery_source="auto_sweep",
            status="suggested",
            fit_score=0,
            readiness_score=0,
            swept_at=now,
            reclaim_blocked_until=None,
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.startup.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            _backfill_sweep_cooldown()

        db_session.refresh(pa)
        assert pa.reclaim_blocked_until is not None
        delta = pa.reclaim_blocked_until - pa.swept_at
        assert abs(delta - timedelta(days=30)) < timedelta(seconds=5)

    def test_already_set_cooldown_is_not_modified(self, db_session):
        """Rows with an existing reclaim_blocked_until are left untouched."""
        from datetime import timedelta

        from app.models.prospect_account import ProspectAccount
        from app.startup import _backfill_sweep_cooldown

        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        future = now + timedelta(days=30)
        pa = ProspectAccount(
            name="Already Set",
            domain="already-set-sweep.com",
            discovery_source="auto_sweep",
            status="suggested",
            fit_score=0,
            readiness_score=0,
            swept_at=now,
            reclaim_blocked_until=future,
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.startup.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            _backfill_sweep_cooldown()

        db_session.refresh(pa)
        # Should be unchanged — the filter only touches NULL rows
        assert pa.reclaim_blocked_until == future

    def test_dismissed_rows_are_skipped(self, db_session):
        """Dismissed rows with NULL cooldown are not touched."""
        from app.models.prospect_account import ProspectAccount
        from app.startup import _backfill_sweep_cooldown

        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        pa = ProspectAccount(
            name="Dismissed",
            domain="dismissed-sweep.com",
            discovery_source="auto_sweep",
            status="dismissed",
            fit_score=0,
            readiness_score=0,
            swept_at=now,
            reclaim_blocked_until=None,
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.startup.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            _backfill_sweep_cooldown()

        db_session.refresh(pa)
        assert pa.reclaim_blocked_until is None


def _make_api_sources_engine():
    """SQLite engine with a minimal ``api_sources`` table for reconciliation tests.

    Mirrors only the two columns ``_reconcile_connector_active`` arbitrates: the
    auto-managed health ``status`` and the operator ``is_active`` toggle.
    """
    eng = _make_sqlite_engine()
    with eng.connect() as conn:
        conn.execute(
            sqltext("CREATE TABLE api_sources (id INTEGER PRIMARY KEY, name TEXT, status TEXT, is_active BOOLEAN)")
        )
        # Operator turned this source ON; health later marked it 'disabled' (no connector).
        conn.execute(
            sqltext("INSERT INTO api_sources (id, name, status, is_active) VALUES (1, 'brokerbin', 'disabled', 1)")
        )
        # Health says 'live' but the operator turned it OFF — reconciliation must not re-enable it.
        conn.execute(sqltext("INSERT INTO api_sources (id, name, status, is_active) VALUES (2, 'nexar', 'live', 0)"))
        conn.commit()
    return eng


def test_reconcile_connector_active_preserves_operator_intent():
    """Boot reconciliation must never clobber the operator's ``is_active`` toggle.

    Regression for the boot-reset defect: startup coupled the auto-managed health
    ``status`` to the operator ``is_active`` toggle, flipping operator-enabled
    sources OFF on every reboot. Reconciliation must leave ``is_active`` untouched
    in BOTH directions — neither disable a health-'disabled' source the operator
    turned on (so it can run again once health recovers), nor auto-enable a
    health-'live' source the operator turned off.
    """
    eng = _make_api_sources_engine()
    with eng.connect() as conn:
        _reconcile_connector_active(conn)
        rows = dict(conn.execute(sqltext("SELECT name, is_active FROM api_sources")).all())

    assert rows["brokerbin"], "operator-enabled source must stay active despite status='disabled'"
    assert not rows["nexar"], "reconciliation must not auto-enable an operator-disabled source"
