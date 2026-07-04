"""Tests for migration 184 (users.reports_to_id — per-rep manager self-FK).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto current head 183) plus an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine
      asserting the column is added, dropped, and re-added. SQLite cannot ALTER-ADD/DROP an
      FK constraint, so create_foreign_key / drop_constraint are no-oped during the test
      (the real column DDL still runs). The FK semantics are PG-only and are proven
      separately on a throwaway PostgreSQL 16 round-trip + at deploy time.

Called by: pytest
Depends on: alembic/versions/184_user_reports_to.py, tests/migration_harness.run_ops
"""

import importlib.util
import os
from unittest.mock import patch

from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
)
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "184_user_reports_to.py")
_spec = importlib.util.spec_from_file_location("migration_184", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "184_user_reports_to"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_current_head(self):
        assert _mod.down_revision == "183_customer_bid_lifecycle"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine, asserting the
    column."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table(
            "users",
            md,
            Column("id", Integer, primary_key=True),
            Column("email", String(255)),
        )
        md.create_all(engine)
        return engine

    @staticmethod
    def _has_col(engine) -> bool:
        return "reports_to_id" in {c["name"] for c in inspect(engine).get_columns("users")}

    def test_round_trip(self):
        engine = self._engine()
        assert not self._has_col(engine)

        # SQLite has no ALTER-ADD/DROP CONSTRAINT — no-op the FK ops so the round-trip
        # exercises the real add_column/drop_column DDL. The users.id self-FK is PG-only
        # (verified on a throwaway PG16 + live deploy).
        with (
            patch.object(Operations, "create_foreign_key", lambda *a, **k: None),
            patch.object(Operations, "drop_constraint", lambda *a, **k: None),
        ):
            run_ops(engine, _mod.upgrade)
            assert self._has_col(engine)

            run_ops(engine, _mod.downgrade)
            assert not self._has_col(engine)

            run_ops(engine, _mod.upgrade)
            assert self._has_col(engine)
