"""Tests for migration 181 (users.display_timezone).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto current head 180) plus an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine
      asserting the column is added, dropped, and re-added. Full PG16 round-trip is proven
      separately on a throwaway PostgreSQL 16.

Called by: pytest
Depends on: alembic/versions/181_add_user_display_timezone.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

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

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "alembic", "versions", "181_add_user_display_timezone.py"
)
_spec = importlib.util.spec_from_file_location("migration_181", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "181_add_user_display_timezone"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_current_head(self):
        assert _mod.down_revision == "180_ticket_kind_discriminator"


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
        return "display_timezone" in {c["name"] for c in inspect(engine).get_columns("users")}

    def test_round_trip(self):
        engine = self._engine()
        assert not self._has_col(engine)

        run_ops(engine, _mod.upgrade)
        assert self._has_col(engine)

        run_ops(engine, _mod.downgrade)
        assert not self._has_col(engine)

        run_ops(engine, _mod.upgrade)
        assert self._has_col(engine)
