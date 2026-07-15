"""Tests for migration 188 (canonicalize the FK on offers.excess_line_item_id).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto current head 187),
      the SQLite dialect guard (whole migration is a no-op off PostgreSQL), and the
      PostgreSQL convergence decision table — driven with a mocked ``op``/bind so all
      three reachable constraint states are exercised without a live PG:
        both names   -> DROP the stray fk_offers_excess_line_item_id
        stray only   -> RENAME it to offers_excess_line_item_id_fkey
        canonical only / none -> no-op
      The real DDL was proven on a throwaway PostgreSQL 16: full-chain replay
      reproduced the duplicate FK, 188 converged all three states, and
      upgrade -> downgrade -> upgrade round-tripped clean (see the migration
      docstring / PR body).

Called by: pytest
Depends on: alembic/versions/188_canonical_offers_excess_fk.py,
      tests/migration_harness.run_ops
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "alembic", "versions", "188_canonical_offers_excess_fk.py"
)
_spec = importlib.util.spec_from_file_location("migration_188", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_CANONICAL = "offers_excess_line_item_id_fkey"
_STRAY = "fk_offers_excess_line_item_id"


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "188_canonical_offers_excess_fk"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_current_head(self):
        assert _mod.down_revision == "187_startup_backfill_partial_idx"


class TestSqliteDialectGuard:
    """The migration is PostgreSQL-only (pg_constraint / RENAME CONSTRAINT): on the
    SQLite test dialect upgrade() and downgrade() must both return without emitting any
    DDL."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table("offers", md, Column("id", Integer, primary_key=True), Column("excess_line_item_id", Integer))
        md.create_all(engine)
        return engine

    def test_upgrade_and_downgrade_are_noops_on_sqlite(self):
        engine = self._engine()
        before = {c["name"] for c in inspect(engine).get_columns("offers")}

        run_ops(engine, _mod.upgrade)
        run_ops(engine, _mod.downgrade)

        after = {c["name"] for c in inspect(engine).get_columns("offers")}
        assert after == before


class TestPostgresConvergenceDecisionTable:
    """Exercise upgrade()'s three-state convergence logic with a mocked ``op`` whose
    bind reports the postgresql dialect, and ``_excess_fk_names`` patched to each
    reachable constraint state."""

    @staticmethod
    def _run_with_state(names: set[str]) -> MagicMock:
        mock_op = MagicMock()
        mock_op.get_bind.return_value.dialect.name = "postgresql"
        with (
            patch.object(_mod, "op", mock_op),
            patch.object(_mod, "_excess_fk_names", return_value=names),
        ):
            _mod.upgrade()
        return mock_op

    def test_both_names_drops_the_stray(self):
        mock_op = self._run_with_state({_CANONICAL, _STRAY})
        mock_op.drop_constraint.assert_called_once_with(_STRAY, "offers", type_="foreignkey")
        mock_op.execute.assert_not_called()

    def test_stray_only_renames_to_canonical(self):
        mock_op = self._run_with_state({_STRAY})
        mock_op.drop_constraint.assert_not_called()
        mock_op.execute.assert_called_once_with(f"ALTER TABLE offers RENAME CONSTRAINT {_STRAY} TO {_CANONICAL}")

    def test_canonical_only_is_a_noop(self):
        mock_op = self._run_with_state({_CANONICAL})
        mock_op.drop_constraint.assert_not_called()
        mock_op.execute.assert_not_called()

    def test_no_fk_at_all_is_a_noop(self):
        mock_op = self._run_with_state(set())
        mock_op.drop_constraint.assert_not_called()
        mock_op.execute.assert_not_called()
