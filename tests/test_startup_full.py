"""
test_startup_full.py -- Full coverage tests for app/startup.py
"""

import logging
import os
from unittest.mock import MagicMock, patch, call

import pytest
from sqlalchemy import create_engine, text as sqltext
from sqlalchemy.pool import StaticPool


def _make_sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class TestExecFunction:
    def test_exec_success_with_params(self):
        from app.startup import _exec
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE test_exec (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.commit()
            _exec(conn, "INSERT INTO test_exec (id, name) VALUES (:id, :name)", {"id": 1, "name": "test"})
            row = conn.execute(sqltext("SELECT name FROM test_exec WHERE id = 1")).fetchone()
            assert row[0] == "test"

    def test_exec_failure_rolls_back(self):
        from app.startup import _exec
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _exec(conn, "THIS IS NOT VALID SQL")
            conn.execute(sqltext("SELECT 1"))

    def test_exec_no_params(self):
        from app.startup import _exec
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE test_np (id INTEGER PRIMARY KEY)"))
            conn.commit()
            _exec(conn, "INSERT INTO test_np (id) VALUES (42)")
            row = conn.execute(sqltext("SELECT id FROM test_np")).fetchone()
            assert row[0] == 42


class TestEnablePgStatStatements:
    def test_pg_stat_statements_calls_exec(self):
        from app.startup import _enable_pg_stat_statements
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _enable_pg_stat_statements(conn)


class TestAddMissingColumns:
    def test_add_missing_columns_on_sqlite(self):
        from app.startup import _add_missing_columns
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE buy_plans (id INTEGER PRIMARY KEY)"))
            conn.execute(sqltext("CREATE TABLE vendor_cards (id INTEGER PRIMARY KEY, engagement_score FLOAT)"))
            conn.commit()
            _add_missing_columns(conn)


class TestCreateFtsTriggers:
    def test_create_fts_triggers_on_sqlite(self):
        from app.startup import _create_fts_triggers
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _create_fts_triggers(conn)


class TestBackfillFts:
    def test_backfill_fts_on_sqlite(self):
        from app.startup import _backfill_fts
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _backfill_fts(conn)


class TestSeedSystemConfig:
    def test_seed_system_config_on_sqlite(self):
        from app.startup import _seed_system_config
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _seed_system_config(conn)


class TestSeedSiteContacts:
    def test_seed_site_contacts_already_seeded(self):
        from app.startup import _seed_site_contacts
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE site_contacts (id INTEGER PRIMARY KEY, customer_site_id INT, "
                "full_name TEXT, title TEXT, email TEXT, phone TEXT, is_primary BOOLEAN)"
            ))
            conn.execute(sqltext(
                "INSERT INTO site_contacts (id, customer_site_id, full_name, title, email, phone, is_primary) "
                "VALUES (1, 1, 'Test', 'Eng', 'a@b.com', '123', 1)"
            ))
            conn.commit()
            _seed_site_contacts(conn)

    def test_seed_site_contacts_empty_table(self):
        from app.startup import _seed_site_contacts
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext(
                "CREATE TABLE site_contacts (id INTEGER PRIMARY KEY, customer_site_id INT, "
                "full_name TEXT, title TEXT, email TEXT, phone TEXT, is_primary BOOLEAN)"
            ))
            conn.execute(sqltext(
                "CREATE TABLE customer_sites (id INTEGER PRIMARY KEY, contact_name TEXT, "
                "contact_title TEXT, contact_email TEXT, contact_phone TEXT)"
            ))
            conn.execute(sqltext(
                "INSERT INTO customer_sites (id, contact_name, contact_title, contact_email, contact_phone) "
                "VALUES (1, 'Jane', 'Eng', 'jane@x.com', '555')"
            ))
            conn.commit()
            _seed_site_contacts(conn)
            row = conn.execute(sqltext("SELECT full_name FROM site_contacts")).fetchone()
            assert row[0] == "Jane"

    def test_seed_site_contacts_exception(self):
        from app.startup import _seed_site_contacts
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _seed_site_contacts(conn)


class TestAddCheckConstraints:
    def test_add_check_constraints_on_sqlite(self):
        from app.startup import _add_check_constraints
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _add_check_constraints(conn)


class TestCreatePerfIndexes:
    def test_create_perf_indexes_on_sqlite(self):
        from app.startup import _create_perf_indexes
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            _create_perf_indexes(conn)


class TestBackfillNormalizedMpn:
    def test_backfill_requirements_and_material_cards(self):
        from app.startup import _backfill_normalized_mpn
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, 'LM-317T', NULL)"))
            conn.execute(sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, 'LM-317T', NULL)"))
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
            conn.execute(sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)"))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_material_cards_exception(self):
        from app.startup import _backfill_normalized_mpn
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)"))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_skips_empty_mpn(self):
        from app.startup import _backfill_normalized_mpn
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, '---', NULL)"))
            conn.execute(sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, '---', NULL)"))
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
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, 'LM317T', 'lm317t')"))
            conn.execute(sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (2, 'LM-317T', NULL)"))
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
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, 'LM317T', 'lm317t')"))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()

    def test_backfill_with_empty_string_mpn(self):
        """Backfill with empty-string primary_mpn exercises _key falsy branch (line 221)."""
        from app.startup import _backfill_normalized_mpn
        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(sqltext("CREATE TABLE requirements (id INTEGER PRIMARY KEY, primary_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("CREATE TABLE material_cards (id INTEGER PRIMARY KEY, display_mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO requirements (id, primary_mpn, normalized_mpn) VALUES (1, '', NULL)"))
            conn.execute(sqltext("INSERT INTO material_cards (id, display_mpn, normalized_mpn) VALUES (1, '', NULL)"))
            conn.commit()
        with patch("app.startup.engine", eng):
            _backfill_normalized_mpn()
        with eng.connect() as conn:
            req = conn.execute(sqltext("SELECT normalized_mpn FROM requirements WHERE id = 1")).fetchone()
            assert req[0] is None



class TestRunStartupMigrationsNonTesting:
    def test_non_testing_mode_runs_all_migrations(self):
        from app.startup import run_startup_migrations
        eng = _make_sqlite_engine()
        original = os.environ.pop("TESTING", None)
        try:
            with patch("app.startup.engine", eng), \
                 patch("app.startup._add_missing_columns") as m_cols, \
                 patch("app.startup._enable_pg_stat_statements") as m_pg, \
                 patch("app.startup._create_fts_triggers") as m_fts, \
                 patch("app.startup._backfill_fts") as m_bfts, \
                 patch("app.startup._seed_system_config") as m_seed, \
                 patch("app.startup._seed_site_contacts") as m_site, \
                 patch("app.startup._add_check_constraints") as m_chk, \
                 patch("app.startup._create_perf_indexes") as m_idx, \
                 patch("app.startup._backfill_normalized_mpn") as m_bfill, \
                 patch("app.models.Base") as mock_base_cls:
                run_startup_migrations()
                m_cols.assert_called_once()
                m_pg.assert_called_once()
                m_fts.assert_called_once()
                m_bfts.assert_called_once()
                m_seed.assert_called_once()
                m_site.assert_called_once()
                m_chk.assert_called_once()
                m_idx.assert_called_once()
                m_bfill.assert_called_once()
                mock_base_cls.metadata.create_all.assert_called_once()
        finally:
            if original is not None:
                os.environ["TESTING"] = original
            else:
                os.environ["TESTING"] = "1"
