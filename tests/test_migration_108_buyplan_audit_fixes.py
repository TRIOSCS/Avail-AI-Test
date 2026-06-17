"""Tests for migration 108 (drop dead token-approval columns; add
buy_plan_lines.last_nudge_at).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto 107) plus an executable
      upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine. Portable
      DDL (drop/add columns + plain indexes) runs on SQLite via the migration harness
      (render_as_batch handles SQLite column drops).
Called by: pytest
Depends on: alembic/versions/108_buyplan_audit_fixes.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "108_buyplan_audit_fixes.py")
_spec = importlib.util.spec_from_file_location("migration_108", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "108_buyplan_audit_fixes"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        # Re-chained onto bp_cph_recorded_at at merge (#343 landed first); see migration header.
        assert _mod.down_revision == "bp_cph_recorded_at"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table(
            "buy_plans_v3",
            md,
            Column("id", Integer, primary_key=True),
            Column("approval_token", String(100)),
            Column("token_expires_at", DateTime),
        )
        Table(
            "buy_plan_lines",
            md,
            Column("id", Integer, primary_key=True),
            Column("status", String(30)),
        )
        md.create_all(engine)
        # Pre-existing index the upgrade drops.
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE INDEX ix_bpv3_token ON buy_plans_v3 (approval_token)")
        return engine

    def test_upgrade_downgrade_upgrade(self):
        engine = self._engine()

        run_ops(engine, _mod.upgrade)
        bp_cols = {c["name"] for c in inspect(engine).get_columns("buy_plans_v3")}
        assert "approval_token" not in bp_cols and "token_expires_at" not in bp_cols
        assert "ix_bpv3_token" not in {i["name"] for i in inspect(engine).get_indexes("buy_plans_v3")}
        line_cols = {c["name"] for c in inspect(engine).get_columns("buy_plan_lines")}
        assert "last_nudge_at" in line_cols
        assert "ix_bpl_nudge_status" in {i["name"] for i in inspect(engine).get_indexes("buy_plan_lines")}

        run_ops(engine, _mod.downgrade)
        bp_cols2 = {c["name"] for c in inspect(engine).get_columns("buy_plans_v3")}
        assert {"approval_token", "token_expires_at"} <= bp_cols2
        assert "last_nudge_at" not in {c["name"] for c in inspect(engine).get_columns("buy_plan_lines")}

        run_ops(engine, _mod.upgrade)
        assert "last_nudge_at" in {c["name"] for c in inspect(engine).get_columns("buy_plan_lines")}
