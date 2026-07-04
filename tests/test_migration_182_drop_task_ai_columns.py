"""Tests for migration 182 (DROP requisition_tasks.ai_priority_score + ai_risk_flag).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto 181) plus an executable
      upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine. The scratch
      table is created WITH both ai columns (upgrade DROPS them); the test asserts they are
      dropped on upgrade and COME BACK (nullable) on downgrade. Full PG16 round-trip is
      proven separately on a throwaway PostgreSQL 16. Mirrors
      tests/test_migration_181_display_timezone.py.

Called by: pytest
Depends on: alembic/versions/182_drop_task_ai_columns.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
)
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "182_drop_task_ai_columns.py")
_spec = importlib.util.spec_from_file_location("migration_182", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_AI_COLS = ("ai_priority_score", "ai_risk_flag")


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "182_drop_task_ai_columns"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_prior_head(self):
        assert _mod.down_revision == "181_add_user_display_timezone"


class TestExecution:
    """Upgrade drops both ai columns; downgrade restores them (nullable)."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table(
            "requisition_tasks",
            md,
            Column("id", Integer, primary_key=True),
            Column("title", String(255)),
            # Present at the start so the migration's upgrade has something to DROP.
            Column("ai_priority_score", Float),
            Column("ai_risk_flag", String(255)),
        )
        md.create_all(engine)
        return engine

    @staticmethod
    def _cols(engine) -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("requisition_tasks")}

    def test_round_trip(self):
        engine = self._engine()
        cols = self._cols(engine)
        assert all(c in cols for c in _AI_COLS)  # both present before upgrade

        run_ops(engine, _mod.upgrade)
        cols = self._cols(engine)
        assert all(c not in cols for c in _AI_COLS)  # upgrade dropped both

        run_ops(engine, _mod.downgrade)
        cols = self._cols(engine)
        assert all(c in cols for c in _AI_COLS)  # downgrade brought them back

        run_ops(engine, _mod.upgrade)
        cols = self._cols(engine)
        assert all(c not in cols for c in _AI_COLS)  # dropped again — clean round trip
