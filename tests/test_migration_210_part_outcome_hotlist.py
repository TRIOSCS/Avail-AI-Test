"""Tests for migration 210 (part-level archive becomes Won/Lost/Hotlist).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto head 209), and an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite
      engine covering the portable pieces: the archived->lost remap with the
      "Archived (legacy)" sentinel stamped only where no reason exists, the
      deterministic downgrade (sentinel rows return to archived with the reason
      NULLed, already-reasoned rows stay lost, hotlist falls back to archived),
      and the cloned_from_id column + ix_requirements_cloned_from index add/drop.
      The CHECK/FK constraint DDL is PostgreSQL-only (dialect-guarded in the
      migration — SQLite cannot ALTER constraints and never carried the CHECK);
      the real DDL is proven on a throwaway PostgreSQL pre-PR (see the migration
      docstring / PR body).

Called by: pytest
Depends on: alembic/versions/210_part_outcome_hotlist.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "210_part_outcome_hotlist.py")
_spec = importlib.util.spec_from_file_location("migration_210", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "210_part_outcome_hotlist"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_current_head(self):
        assert _mod.down_revision == "209_buyplan_halt_snapshot"


def _engine_with_rows():
    """Scratch requirements table with one row per interesting starting state."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    md = MetaData()
    Table(
        "requirements",
        md,
        Column("id", Integer, primary_key=True),
        Column("sourcing_status", String(20)),
        Column("outcome_reason", Text),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO requirements (id, sourcing_status, outcome_reason) VALUES (:i, :s, :r)"),
            [
                {"i": 1, "s": "archived", "r": None},  # sentinel-stamped, reversible
                {"i": 2, "s": "archived", "r": ""},  # blank counts as no reason
                {"i": 3, "s": "archived", "r": "customer folded"},  # keeps its reason, stays lost
                {"i": 4, "s": "lost", "r": "price"},  # pre-existing lost: untouched
                {"i": 5, "s": "open", "r": None},  # active: untouched
                {"i": 6, "s": None, "r": None},  # legacy NULL: untouched
            ],
        )
    return engine


def _rows(engine):
    with engine.connect() as conn:
        result = conn.execute(text("SELECT id, sourcing_status, outcome_reason FROM requirements ORDER BY id"))
        return {row[0]: (row[1], row[2]) for row in result}


class TestUpgrade:
    def test_archived_rows_become_lost_with_sentinel_where_blank(self):
        engine = _engine_with_rows()
        run_ops(engine, _mod.upgrade)

        rows = _rows(engine)
        assert rows[1] == ("lost", "Archived (legacy)")
        assert rows[2] == ("lost", "Archived (legacy)")
        assert rows[3] == ("lost", "customer folded")  # real reason preserved
        assert rows[4] == ("lost", "price")  # untouched
        assert rows[5] == ("open", None)
        assert rows[6] == (None, None)

    def test_adds_cloned_from_id_column_and_index(self):
        engine = _engine_with_rows()
        run_ops(engine, _mod.upgrade)

        cols = {c["name"] for c in inspect(engine).get_columns("requirements")}
        assert "cloned_from_id" in cols
        idx = {i["name"] for i in inspect(engine).get_indexes("requirements")}
        assert "ix_requirements_cloned_from" in idx


class TestRoundTrip:
    def test_downgrade_restores_sentinel_rows_only(self):
        engine = _engine_with_rows()
        run_ops(engine, _mod.upgrade)
        run_ops(engine, _mod.downgrade)

        rows = _rows(engine)
        # Sentinel rows return to archived, reason NULLed.
        assert rows[1] == ("archived", None)
        assert rows[2] == ("archived", None)
        # Already-reasoned archived row stays lost (documented lossy, mirrors 158).
        assert rows[3] == ("lost", "customer folded")
        assert rows[4] == ("lost", "price")
        assert rows[5] == ("open", None)
        assert rows[6] == (None, None)
        cols = {c["name"] for c in inspect(engine).get_columns("requirements")}
        assert "cloned_from_id" not in cols

    def test_downgrade_maps_hotlist_to_archived(self):
        engine = _engine_with_rows()
        run_ops(engine, _mod.upgrade)
        with engine.begin() as conn:
            conn.execute(text("UPDATE requirements SET sourcing_status = 'hotlist' WHERE id = 5"))
        run_ops(engine, _mod.downgrade)

        assert _rows(engine)[5] == ("archived", None)

    def test_upgrade_downgrade_upgrade_clean(self):
        engine = _engine_with_rows()
        run_ops(engine, _mod.upgrade)
        run_ops(engine, _mod.downgrade)
        run_ops(engine, _mod.upgrade)

        rows = _rows(engine)
        assert rows[1] == ("lost", "Archived (legacy)")
        assert rows[3] == ("lost", "customer folded")
        cols = {c["name"] for c in inspect(engine).get_columns("requirements")}
        assert "cloned_from_id" in cols
