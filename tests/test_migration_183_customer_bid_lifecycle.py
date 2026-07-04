"""Tests for migration 183 (customer_bids send/accept/reject lifecycle stamps).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto 182) plus an executable
      upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine asserting the
      three stamp columns (sent_at, responded_at, responded_by_id) are added and dropped.

      SQLite can't ALTER TABLE ADD/DROP a FOREIGN KEY constraint, so — following the
      tests/test_migration_136_137_138.py pattern — the FK create/drop ops are patched to
      real no-op callables (SQLAlchemy inspects signatures, so MagicMock won't do). The
      column adds/drops run portably; the FK's PG semantics are verified on the live
      Postgres instance at deploy time. Full PG16 round-trip is proven separately on a
      throwaway PostgreSQL 16.

Called by: pytest
Depends on: alembic/versions/183_customer_bid_lifecycle.py, tests/migration_harness.run_ops
"""

import importlib.util
import os
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    inspect,
)
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "183_customer_bid_lifecycle.py")
_spec = importlib.util.spec_from_file_location("migration_183", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_NEW_COLS = ("sent_at", "responded_at", "responded_by_id")


# ---------------------------------------------------------------------------
# SQLite can't ALTER TABLE ADD/DROP a FK constraint — patch those ops to real
# no-op callables (alembic inspects __code__/__annotations__, so a MagicMock
# won't dispatch). Column adds/drops are exercised portably.
# ---------------------------------------------------------------------------


def _noop_fk(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op replacement for create_foreign_key on SQLite."""


def _noop_constraint(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op replacement for drop_constraint on SQLite."""


@contextmanager
def _sqlite_compat():
    with (
        patch("alembic.operations.Operations.create_foreign_key", _noop_fk),
        patch("alembic.operations.Operations.drop_constraint", _noop_constraint),
    ):
        yield


def _run(engine, fn):
    with _sqlite_compat():
        run_ops(engine, fn)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "183_customer_bid_lifecycle"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_prior_head(self):
        assert _mod.down_revision == "182_drop_task_ai_columns"


class TestExecution:
    """Upgrade adds the three stamp columns; downgrade drops them (FK ops no-op'd)."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table(
            "users",  # FK target (the create_foreign_key op itself is no-op'd on SQLite)
            md,
            Column("id", Integer, primary_key=True),
        )
        Table(
            "customer_bids",
            md,
            Column("id", Integer, primary_key=True),
        )
        md.create_all(engine)
        return engine

    @staticmethod
    def _cols(engine) -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("customer_bids")}

    def test_round_trip(self):
        engine = self._engine()
        cols = self._cols(engine)
        assert all(c not in cols for c in _NEW_COLS)  # none present before upgrade

        _run(engine, _mod.upgrade)
        cols = self._cols(engine)
        assert all(c in cols for c in _NEW_COLS)  # upgrade added all three stamps

        _run(engine, _mod.downgrade)
        cols = self._cols(engine)
        assert all(c not in cols for c in _NEW_COLS)  # downgrade dropped all three

        _run(engine, _mod.upgrade)
        cols = self._cols(engine)
        assert all(c in cols for c in _NEW_COLS)  # re-added — clean round trip
