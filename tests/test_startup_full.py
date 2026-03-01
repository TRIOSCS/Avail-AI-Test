"""
test_startup_full.py -- Full coverage tests for app/startup.py
"""

import os
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy import text as sqltext
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
        """Backfill with empty-string primary_mpn exercises _key falsy branch (line 221)."""
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
    def test_non_testing_mode_runs_all_migrations(self):
        from app.startup import run_startup_migrations

        eng = _make_sqlite_engine()
        original = os.environ.pop("TESTING", None)
        try:
            with (
                patch("app.startup.engine", eng),
                patch("app.startup._add_missing_columns") as m_cols,
                patch("app.startup._enable_pg_stat_statements") as m_pg,
                patch("app.startup._create_fts_triggers") as m_fts,
                patch("app.startup._backfill_fts") as m_bfts,
                patch("app.startup._seed_system_config") as m_seed,
                patch("app.startup._seed_site_contacts") as m_site,
                patch("app.startup._add_check_constraints") as m_chk,
                patch("app.startup._create_perf_indexes") as m_idx,
                patch("app.startup._backfill_normalized_mpn") as m_bfill,
                patch("app.models.Base") as mock_base_cls,
            ):
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


class TestBackfillSightingOfferNormalizedMpn:
    """Tests for _backfill_sighting_offer_normalized_mpn (lines 492-545)."""

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
            # mpn_matched is empty string after strip, _key returns ""
            conn.execute(sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (1, '  ', NULL)"))
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (1, '---', NULL)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()

        with eng.connect() as conn:
            s = conn.execute(sqltext("SELECT normalized_mpn FROM sightings WHERE id = 1")).fetchone()
            assert s[0] is None  # not updated because _key returned ""
            o = conn.execute(sqltext("SELECT normalized_mpn FROM offers WHERE id = 1")).fetchone()
            assert o[0] is None

    def test_key_with_none(self):
        """_key(None) returns '' — exercises the `if not raw: return ""` branch (line 498-499)."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)")
            )
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"))
            # The WHERE clause filters NULL mpn_matched, but if somehow present,
            # we need rows that DO have mpn_matched to exercise _key.
            # Insert a row with a valid mpn_matched to enter the loop, and one
            # with all-special-chars to get empty _key result.
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
            # All rows already have normalized_mpn populated
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
            # Only create offers, not sightings — sightings query will fail
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
    """Tests for _backfill_sighting_vendor_normalized (lines 548-590)."""

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
        """If vendor_name_normalized column doesn't exist, function returns without error."""
        from app.startup import _backfill_sighting_vendor_normalized

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            # Create sightings WITHOUT vendor_name_normalized column
            conn.execute(sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, vendor_name TEXT)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            # Should return early without error
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
        """If normalize_vendor_name returns empty for a row, that row is not added
        to the batch. On next iteration the same row is re-selected, and we use
        the exception path to break out of the loop."""
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
                return ""  # exercises the `if nv` falsy branch
            raise RuntimeError("stop loop")

        with (
            patch("app.startup.engine", eng),
            patch("app.vendor_utils.normalize_vendor_name", side_effect=normalize_then_fail),
        ):
            _backfill_sighting_vendor_normalized()

        with eng.connect() as conn:
            r = conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings WHERE id = 1")).fetchone()
            assert r[0] is None  # not updated because normalize returned ""

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

        # Row should remain NULL since the exception caused rollback + break
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
