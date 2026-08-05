"""
test_startup.py -- Tests for app/startup.py

Covers: TESTING guard, _exec error handling, _create_default_user_if_env_set,
_seed_admin_user_if_env_set, _create_count_triggers, _create_fts_triggers,
_seed_system_config, _analyze_hot_tables, run_startup_migrations (non-testing
mode), and the deferred phase (ANALYZE + observability; the one-time backfills
were verified complete and deleted in W2.9).

Called by: pytest
Depends on: app/startup.py, conftest fixtures
"""

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


class TestAnalyzeHotTables:
    """Lines 548-549: _analyze_hot_tables."""

    def test_analyze_fails_gracefully_on_sqlite(self, db_session):
        """ANALYZE on PG tables fails gracefully on SQLite."""
        from app.startup import _analyze_hot_tables
        from tests.conftest import engine

        with engine.connect() as conn:
            _analyze_hot_tables(conn)


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


class TestSeedSystemConfig:
    """_seed_system_config (PG-specific, fails gracefully on SQLite)."""

    def test_seed_system_config_on_sqlite(self):
        from app.startup import _seed_system_config

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _seed_system_config(conn)


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
                patch("app.startup._seed_system_config") as m_seed,
                patch("app.startup._seed_manufacturers"),
                patch("app.startup._create_count_triggers") as m_ct,
                patch("app.startup._analyze_hot_tables") as m_analyze,
                patch("app.startup._exec"),
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
                # SLOW ops live in run_deferred_startup_backfills — NOT called here.
                m_analyze.assert_not_called()
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
        COMPLETED when the phase finishes successfully.

        (W2.9: the phase now holds only the deploy-gated ANALYZE and the category-
        observability scan — the one-time backfills were verified complete and deleted.)
        """
        import app.startup as startup_mod
        from app.constants import DeferredBackfillState
        from app.startup import run_deferred_startup_backfills

        eng = _make_sqlite_engine()
        original = os.environ.pop("TESTING", None)
        startup_mod.deferred_backfills_state = DeferredBackfillState.RUNNING
        try:
            with (
                patch("app.startup.engine", eng),
                patch("app.startup._maybe_analyze_hot_tables") as m_analyze,
                patch("app.startup._warn_non_canonical_categories") as m_warn,
            ):
                run_deferred_startup_backfills()
                m_analyze.assert_called_once()
                m_warn.assert_called_once()
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
                patch("app.startup._maybe_analyze_hot_tables", side_effect=RuntimeError("boom")),
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
