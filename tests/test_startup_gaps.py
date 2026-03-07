"""
test_startup_gaps.py -- Coverage gap tests for app/startup.py

Covers the functions that test_startup.py and test_startup_full.py miss:
- _create_default_user_if_env_set (lines 52-89)
- _seed_vinod_user (lines 92-117)
- _backfill_proactive_offer_qty (lines 552-634)
- _backfill_null_ticket_fields (lines 637-664)
- _create_count_triggers / _backfill_company_counts / _analyze_hot_tables (PG stubs)
- run_startup_migrations non-TESTING with _backfill_null_ticket_fields call

Called by: pytest
Depends on: app/startup.py, conftest.py
"""

import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy import text as sqltext
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.conftest import engine  # noqa: F401


def _make_sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# ═══════════════════════════════════════════════════════════════════════
# _create_default_user_if_env_set
# ═══════════════════════════════════════════════════════════════════════


class TestCreateDefaultUserIfEnvSet:
    def test_no_env_vars_does_nothing(self):
        """Missing DEFAULT_USER_EMAIL/PASSWORD -> returns without creating user."""
        from app.startup import _create_default_user_if_env_set

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFAULT_USER_EMAIL", None)
            os.environ.pop("DEFAULT_USER_PASSWORD", None)
            _create_default_user_if_env_set()

    def test_creates_user_when_env_vars_set(self, db_session):
        """When DEFAULT_USER_EMAIL and DEFAULT_USER_PASSWORD are set, creates user."""
        from app.startup import _create_default_user_if_env_set

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with (
            patch.dict(os.environ, {
                "DEFAULT_USER_EMAIL": "newuser@example.com",
                "DEFAULT_USER_PASSWORD": "secret123",
                "DEFAULT_USER_ROLE": "buyer",
            }),
            patch("app.startup.SessionLocal", return_value=mock_session),
        ):
            _create_default_user_if_env_set()

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

        created_user = mock_session.add.call_args[0][0]
        assert created_user.email == "newuser@example.com"
        assert created_user.role == "buyer"
        assert created_user.password_hash is not None
        assert "$" in created_user.password_hash  # salt$hash format

    def test_existing_user_skips_creation(self):
        """When user already exists, does not create a duplicate."""
        from app.startup import _create_default_user_if_env_set

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = MagicMock()

        with (
            patch.dict(os.environ, {
                "DEFAULT_USER_EMAIL": "existing@example.com",
                "DEFAULT_USER_PASSWORD": "secret123",
            }),
            patch("app.startup.SessionLocal", return_value=mock_session),
        ):
            _create_default_user_if_env_set()

        mock_session.add.assert_not_called()
        mock_session.close.assert_called_once()

    def test_exception_during_creation(self):
        """Exception during user creation is caught and logged."""
        from app.startup import _create_default_user_if_env_set

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None
        mock_session.commit.side_effect = RuntimeError("DB error")

        with (
            patch.dict(os.environ, {
                "DEFAULT_USER_EMAIL": "fail@example.com",
                "DEFAULT_USER_PASSWORD": "secret123",
            }),
            patch("app.startup.SessionLocal", return_value=mock_session),
        ):
            _create_default_user_if_env_set()

        mock_session.close.assert_called_once()

    def test_default_role_is_admin(self):
        """Without DEFAULT_USER_ROLE, role defaults to 'admin'."""
        from app.startup import _create_default_user_if_env_set

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        env = {
            "DEFAULT_USER_EMAIL": "admin@example.com",
            "DEFAULT_USER_PASSWORD": "secret",
        }
        # Remove DEFAULT_USER_ROLE if present
        with (
            patch.dict(os.environ, env, clear=False),
            patch("app.startup.SessionLocal", return_value=mock_session),
        ):
            os.environ.pop("DEFAULT_USER_ROLE", None)
            _create_default_user_if_env_set()

        created_user = mock_session.add.call_args[0][0]
        assert created_user.role == "admin"


# ═══════════════════════════════════════════════════════════════════════
# _seed_vinod_user
# ═══════════════════════════════════════════════════════════════════════


class TestSeedVinodUser:
    def test_creates_vinod_user(self):
        """Creates Vinod admin user when not existing."""
        from app.startup import _seed_vinod_user

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.startup.SessionLocal", return_value=mock_session):
            _seed_vinod_user()

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

        user = mock_session.add.call_args[0][0]
        assert user.email == "vinod@trioscs.com"
        assert user.role == "admin"

    def test_existing_vinod_skips(self):
        """Existing Vinod user skips creation."""
        from app.startup import _seed_vinod_user

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = MagicMock()

        with patch("app.startup.SessionLocal", return_value=mock_session):
            _seed_vinod_user()

        mock_session.add.assert_not_called()
        mock_session.close.assert_called_once()

    def test_exception_during_creation(self):
        """Exception during Vinod creation is caught, rolled back, session closed."""
        from app.startup import _seed_vinod_user

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None
        mock_session.commit.side_effect = RuntimeError("DB error")

        with patch("app.startup.SessionLocal", return_value=mock_session):
            _seed_vinod_user()

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    def test_with_provided_db_session(self):
        """When db session is passed directly, does not create/close own session."""
        from app.startup import _seed_vinod_user

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        _seed_vinod_user(db=mock_db)

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_not_called()  # Should NOT close when db is passed


# ═══════════════════════════════════════════════════════════════════════
# _backfill_proactive_offer_qty
# ═══════════════════════════════════════════════════════════════════════


class TestBackfillProactiveOfferQty:
    def test_no_target_map_returns_early(self):
        """When no proactive_matches with target_qty, returns immediately."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE proactive_offers (id INTEGER PRIMARY KEY, line_items TEXT)"
            ))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

    def test_fixes_offer_quantities(self):
        """Offers with qty > target_qty get corrected."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE proactive_offers "
                "(id INTEGER PRIMARY KEY, line_items TEXT, total_sell REAL, total_cost REAL)"
            ))
            # Match with target_qty=50
            conn.execute(sqltext(
                "INSERT INTO requirements (id, target_qty) VALUES (1, 50)"
            ))
            conn.execute(sqltext(
                "INSERT INTO proactive_matches (id, requirement_id) VALUES (10, 1)"
            ))
            # Offer with qty=200 (should become 50)
            items = json.dumps([{
                "match_id": 10,
                "qty": 200,
                "unit_price": 5.0,
                "sell_price": 7.0,
            }])
            conn.execute(sqltext(
                "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, :items, 1400, 1000)"
            ), {"items": items})
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

        with eng.connect() as conn:
            row = conn.execute(sqltext("SELECT line_items, total_sell, total_cost FROM proactive_offers WHERE id = 1")).fetchone()
            updated_items = json.loads(row[0])
            assert updated_items[0]["qty"] == 50
            assert row[1] == 350.0  # 50 * 7.0
            assert row[2] == 250.0  # 50 * 5.0

    def test_no_change_when_qty_already_correct(self):
        """Offers where qty <= target_qty are not updated."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE proactive_offers "
                "(id INTEGER PRIMARY KEY, line_items TEXT, total_sell REAL, total_cost REAL)"
            ))
            conn.execute(sqltext(
                "INSERT INTO requirements (id, target_qty) VALUES (1, 500)"
            ))
            conn.execute(sqltext(
                "INSERT INTO proactive_matches (id, requirement_id) VALUES (10, 1)"
            ))
            items = json.dumps([{
                "match_id": 10,
                "qty": 100,
                "unit_price": 5.0,
                "sell_price": 7.0,
            }])
            conn.execute(sqltext(
                "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, :items, 700, 500)"
            ), {"items": items})
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

        # Should remain unchanged since qty (100) < target_qty (500)
        with eng.connect() as conn:
            row = conn.execute(sqltext("SELECT total_sell, total_cost FROM proactive_offers WHERE id = 1")).fetchone()
            assert row[0] == 700  # unchanged
            assert row[1] == 500

    def test_exception_during_backfill(self):
        """Exception during backfill is caught and rolled back."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"
            ))
            # Missing proactive_offers table will cause exception
            conn.commit()

        with patch("app.startup.engine", eng):
            # Should not raise
            _backfill_proactive_offer_qty()

    def test_null_line_items_skipped(self):
        """Offers with null line_items are skipped."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE proactive_offers "
                "(id INTEGER PRIMARY KEY, line_items TEXT, total_sell REAL, total_cost REAL)"
            ))
            conn.execute(sqltext(
                "INSERT INTO requirements (id, target_qty) VALUES (1, 50)"
            ))
            conn.execute(sqltext(
                "INSERT INTO proactive_matches (id, requirement_id) VALUES (10, 1)"
            ))
            # Offer with NULL line_items
            conn.execute(sqltext(
                "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, NULL, 0, 0)"
            ))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

    def test_item_without_match_id_in_target_map(self):
        """Line items with match_id not in target_map use original qty."""
        from app.startup import _backfill_proactive_offer_qty

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE proactive_matches (id INTEGER PRIMARY KEY, requirement_id INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE requirements (id INTEGER PRIMARY KEY, target_qty INTEGER)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE proactive_offers "
                "(id INTEGER PRIMARY KEY, line_items TEXT, total_sell REAL, total_cost REAL)"
            ))
            conn.execute(sqltext(
                "INSERT INTO requirements (id, target_qty) VALUES (1, 50)"
            ))
            conn.execute(sqltext(
                "INSERT INTO proactive_matches (id, requirement_id) VALUES (10, 1)"
            ))
            # Item with match_id=999 which is NOT in target_map
            items = json.dumps([{
                "match_id": 999,
                "qty": 200,
                "unit_price": 5.0,
            }])
            conn.execute(sqltext(
                "INSERT INTO proactive_offers (id, line_items, total_sell, total_cost) VALUES (1, :items, 1000, 1000)"
            ), {"items": items})
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_proactive_offer_qty()

        # No change because match_id 999 is not in target_map
        with eng.connect() as conn:
            row = conn.execute(sqltext("SELECT total_sell, total_cost FROM proactive_offers WHERE id = 1")).fetchone()
            assert row[0] == 1000  # unchanged


# ═══════════════════════════════════════════════════════════════════════
# _backfill_null_ticket_fields
# ═══════════════════════════════════════════════════════════════════════


class TestBackfillNullTicketFields:
    def test_backfills_null_risk_and_category(self):
        """Tickets with null risk_tier and category get defaults."""
        from app.startup import _backfill_null_ticket_fields

        mock_ticket_1 = MagicMock()
        mock_ticket_1.risk_tier = None
        mock_ticket_1.category = None
        mock_ticket_2 = MagicMock()
        mock_ticket_2.risk_tier = None
        mock_ticket_2.category = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = [mock_ticket_1, mock_ticket_2]

        with patch("app.startup.SessionLocal", return_value=mock_session):
            _backfill_null_ticket_fields()

        assert mock_ticket_1.risk_tier == "low"
        assert mock_ticket_1.category == "other"
        assert mock_ticket_2.risk_tier == "low"
        assert mock_ticket_2.category == "other"
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    def test_no_null_tickets_does_nothing(self):
        """When no tickets have null fields, no commit needed."""
        from app.startup import _backfill_null_ticket_fields

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = []

        with patch("app.startup.SessionLocal", return_value=mock_session):
            _backfill_null_ticket_fields()

        mock_session.commit.assert_not_called()
        mock_session.close.assert_called_once()

    def test_exception_during_backfill(self):
        """Exception is caught, rolled back, and session closed."""
        from app.startup import _backfill_null_ticket_fields

        mock_session = MagicMock()
        mock_session.query.side_effect = RuntimeError("DB error")

        with patch("app.startup.SessionLocal", return_value=mock_session):
            _backfill_null_ticket_fields()

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _create_count_triggers, _backfill_company_counts, _analyze_hot_tables
# ═══════════════════════════════════════════════════════════════════════


class TestCountTriggersAndAnalyze:
    def test_create_count_triggers_on_sqlite(self):
        """PG-specific trigger DDL fails gracefully on SQLite."""
        from app.startup import _create_count_triggers

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _create_count_triggers(conn)

    def test_backfill_company_counts_on_sqlite(self):
        """PG-specific subquery update fails gracefully on SQLite."""
        from app.startup import _backfill_company_counts

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _backfill_company_counts(conn)

    def test_analyze_hot_tables_on_sqlite(self):
        """ANALYZE on non-existent tables fails gracefully on SQLite."""
        from app.startup import _analyze_hot_tables

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _analyze_hot_tables(conn)


# ═══════════════════════════════════════════════════════════════════════
# run_startup_migrations with _backfill_null_ticket_fields in the chain
# ═══════════════════════════════════════════════════════════════════════


class TestRunStartupMigrationsWithTicketBackfill:
    def test_non_testing_calls_backfill_null_ticket_fields(self):
        """run_startup_migrations calls _backfill_null_ticket_fields when not TESTING."""
        from app.startup import run_startup_migrations

        eng = _make_sqlite_engine()
        original = os.environ.pop("TESTING", None)
        try:
            with (
                patch("app.startup.engine", eng),
                patch("app.startup._create_fts_triggers"),
                patch("app.startup._backfill_fts"),
                patch("app.startup._seed_system_config"),
                patch("app.startup._seed_site_contacts"),
                patch("app.startup._create_count_triggers"),
                patch("app.startup._backfill_company_counts"),
                patch("app.startup._analyze_hot_tables"),
                patch("app.startup._backfill_normalized_mpn"),
                patch("app.startup._backfill_sighting_offer_normalized_mpn"),
                patch("app.startup._backfill_sighting_vendor_normalized"),
                patch("app.startup._backfill_proactive_offer_qty") as m_pq,
                patch("app.startup._backfill_null_ticket_fields") as m_bt,
                patch("app.startup._exec"),
                patch("app.startup._seed_vinod_user") as m_vinod,
                patch("app.startup._create_default_user_if_env_set"),
            ):
                run_startup_migrations()
                m_pq.assert_called_once()
                m_bt.assert_called_once()
                m_vinod.assert_called_once()
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            else:
                os.environ["TESTING"] = "1"
