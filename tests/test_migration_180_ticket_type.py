"""Tests for migration 180 (trouble_tickets.ticket_type + index).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto 179) plus an executable
      upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine asserting
      the ``ticket_type`` column AND its ``ix_trouble_tickets_ticket_type`` index are added,
      dropped, and re-added. Full PG16 round-trip is proven separately on a throwaway
      PostgreSQL 16. Mirrors tests/test_migration_181_display_timezone.py.

Called by: pytest
Depends on: alembic/versions/180_ticket_kind_discriminator.py, tests/migration_harness.run_ops
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
    os.path.dirname(__file__), "..", "alembic", "versions", "180_ticket_kind_discriminator.py"
)
_spec = importlib.util.spec_from_file_location("migration_180", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "180_ticket_kind_discriminator"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_prior_head(self):
        assert _mod.down_revision == "179_prepayment_lifecycle"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine, asserting the column
    + index delta."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table(
            "trouble_tickets",
            md,
            Column("id", Integer, primary_key=True),
            Column("subject", String(255)),
        )
        md.create_all(engine)
        return engine

    @staticmethod
    def _has_col(engine) -> bool:
        return "ticket_type" in {c["name"] for c in inspect(engine).get_columns("trouble_tickets")}

    @staticmethod
    def _has_index(engine) -> bool:
        return "ix_trouble_tickets_ticket_type" in {i["name"] for i in inspect(engine).get_indexes("trouble_tickets")}

    def test_round_trip(self):
        engine = self._engine()
        assert not self._has_col(engine)
        assert not self._has_index(engine)

        run_ops(engine, _mod.upgrade)
        assert self._has_col(engine)
        assert self._has_index(engine)

        run_ops(engine, _mod.downgrade)
        assert not self._has_col(engine)
        assert not self._has_index(engine)

        run_ops(engine, _mod.upgrade)
        assert self._has_col(engine)
        assert self._has_index(engine)
