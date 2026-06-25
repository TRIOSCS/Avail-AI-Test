"""Round-trip test for migration 122 (prospect_accounts AI score columns).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto 121) plus an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite
      engine via the shared hermetic harness (tests/migration_harness.run_ops).
      The upgrade adds trio_match_score + opportunity_score (+ their indexes); the
      downgrade removes them. All ops are portable SQLite DDL (plain int columns,
      index create/drop, column drop), so the round trip runs in-process with no PG.

Called by: pytest
Depends on: alembic/versions/122_prospect_ai_scores.py, tests/migration_harness.run_ops
"""

from __future__ import annotations

import importlib.util
import os

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "122_prospect_ai_scores.py")
_spec = importlib.util.spec_from_file_location("migration_122", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "122_prospect_ai_scores"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "121_datasheet_lib_col_rename"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine."""

    @staticmethod
    def _engine() -> sa.engine.Engine:
        engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        meta = sa.MetaData()
        sa.Table(
            "prospect_accounts",
            meta,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(255)),
        )
        meta.create_all(engine)
        return engine

    def _columns(self, engine, table: str) -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns(table)}

    def _indexes(self, engine, table: str) -> set[str]:
        return {i["name"] for i in inspect(engine).get_indexes(table)}

    def test_upgrade_adds_columns_and_indexes(self):
        engine = self._engine()
        run_ops(engine, _mod.upgrade)
        cols = self._columns(engine, "prospect_accounts")
        assert "trio_match_score" in cols
        assert "opportunity_score" in cols
        idxs = self._indexes(engine, "prospect_accounts")
        assert "ix_prospect_accounts_trio_match_score" in idxs
        assert "ix_prospect_accounts_opportunity_score" in idxs

    def test_downgrade_removes_columns(self):
        engine = self._engine()
        run_ops(engine, _mod.upgrade)
        run_ops(engine, _mod.downgrade)
        cols = self._columns(engine, "prospect_accounts")
        assert "trio_match_score" not in cols
        assert "opportunity_score" not in cols

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = self._engine()
        run_ops(engine, _mod.upgrade)
        run_ops(engine, _mod.downgrade)
        run_ops(engine, _mod.upgrade)
        cols = self._columns(engine, "prospect_accounts")
        assert "trio_match_score" in cols
        assert "opportunity_score" in cols
