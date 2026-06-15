"""Tests for migration 105 (demand-telemetry columns + ix_mc_demand_queue).

What: Revision metadata checks (id length vs PG VARCHAR(32), chain wiring onto 104)
      plus an executable upgrade → downgrade → upgrade pass against a scratch
      in-memory SQLite engine. The two columns are portable DDL and exercised on
      SQLite; the PG-only partial DESC-NULLS-LAST index is guarded by a dialect check
      in the migration (SQLite gets the columns but not the index — its DESC NULLS LAST
      keys are not valid SQLite index DDL).
Called by: pytest
Depends on: alembic/versions/105_demand_telemetry.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "105_demand_telemetry.py")
_spec = importlib.util.spec_from_file_location("migration_105", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "105_demand_telemetry"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        # RE-CHAINED onto 104_trust_telemetry: feat/trust-architecture (104) merged to
        # main first, so 105 now chains onto it. Chain runs 103 -> 104 -> 105, a single
        # alembic head (verified by tests/test_migration_chain.py).
        assert _mod.down_revision == "104_trust_telemetry"


class TestExecution:
    """Upgrade → downgrade → upgrade on a scratch SQLite engine."""

    @staticmethod
    def _engine_with_table():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        # The migration ALTERs material_cards; create a minimal stand-in so add/drop
        # column run against a real table (only the columns the migration touches
        # plus the always-present id/normalized_mpn need exist).
        md = MetaData()
        Table(
            "material_cards",
            md,
            Column("id", Integer, primary_key=True),
            Column("normalized_mpn", String(255)),
        )
        md.create_all(engine)
        return engine

    def test_upgrade_downgrade_upgrade(self):
        engine = self._engine_with_table()

        run_ops(engine, _mod.upgrade)
        cols = {c["name"] for c in inspect(engine).get_columns("material_cards")}
        assert {"sourced_qty_90d", "last_sourced_at"} <= cols
        # PG-only partial index is NOT created on SQLite (dialect guard in upgrade()).
        index_names = {i["name"] for i in inspect(engine).get_indexes("material_cards")}
        assert _mod._INDEX_NAME not in index_names

        run_ops(engine, _mod.downgrade)
        cols_after = {c["name"] for c in inspect(engine).get_columns("material_cards")}
        assert "sourced_qty_90d" not in cols_after
        assert "last_sourced_at" not in cols_after

        run_ops(engine, _mod.upgrade)
        cols_again = {c["name"] for c in inspect(engine).get_columns("material_cards")}
        assert {"sourced_qty_90d", "last_sourced_at"} <= cols_again
