"""Tests for migration 106 (brand canonicalization — HPE family + Texas Instruments).

What: Structural checks on the revision metadata, AND an executable
      upgrade -> downgrade -> upgrade pass against a scratch in-memory SQLite engine.
      The migration is data-only on the ``manufacturers`` lookup table (portable UPDATE/
      DELETE statements only), so executing it here is honest coverage — every statement
      is valid on both SQLite and PostgreSQL. Two upgrade paths are exercised: the normal
      rename of the legacy "Hewlett Packard Enterprise" canonical row to "HPE", and the
      seed-race path where a fresh "HPE" row already exists (legacy row is DELETEd).
Called by: pytest
Depends on: alembic/versions/106_brand_canonicalization.py, conftest.py (SQLite type
            adapters at import), app.models.Manufacturer.
"""

import importlib.util
import json
import os

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.models import Base, Manufacturer

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "106_brand_canonicalization.py")
_spec = importlib.util.spec_from_file_location("migration_106", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    """Revision identifiers and chain wiring (re-chained onto 105 at merge)."""

    def test_revision_id(self):
        assert _mod.revision == "106_brand_canonicalization"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        # Re-chained onto main's head (105_demand_telemetry) at merge time so the brand
        # canonicalization runs last in the chain 103 -> 104 -> 105 -> 106 (single head).
        assert _mod.down_revision == "105_demand_telemetry"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine."""

    @staticmethod
    def _scratch_engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine, tables=[Manufacturer.__table__])
        return engine

    @staticmethod
    def _run(engine, fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    @staticmethod
    def _rows(engine):
        with engine.connect() as conn:
            return {
                name: json.loads(aliases) if aliases else []
                for name, aliases in conn.execute(text("SELECT canonical_name, aliases FROM manufacturers")).fetchall()
            }

    def _seed(self, engine, rows):
        with engine.begin() as conn:
            for name, aliases in rows:
                conn.execute(
                    text("INSERT INTO manufacturers (canonical_name, aliases) VALUES (:n, :a)"),
                    {"n": name, "a": json.dumps(aliases)},
                )

    def test_upgrade_renames_hpe_and_extends_ti_then_downgrade_restores(self):
        engine = self._scratch_engine()
        self._seed(
            engine,
            [
                ("Hewlett Packard Enterprise", ["HPE", "HP"]),
                ("Texas Instruments", ["TI", "Texas Inst"]),
                ("Dell Technologies", ["Dell"]),  # untouched control
            ],
        )

        self._run(engine, _mod.upgrade)
        rows = self._rows(engine)
        assert "Hewlett Packard Enterprise" not in rows  # renamed away
        assert rows["HPE"] == ["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]
        assert rows["Texas Instruments"] == ["TI", "Texas Inst", "Texas Instruments (TI)"]
        assert rows["Dell Technologies"] == ["Dell"]  # untouched

        self._run(engine, _mod.downgrade)
        rows = self._rows(engine)
        assert "HPE" not in rows
        assert rows["Hewlett Packard Enterprise"] == ["HPE", "HP"]
        assert rows["Texas Instruments"] == ["TI", "Texas Inst"]
        assert rows["Dell Technologies"] == ["Dell"]

        # Re-upgrade is consistent (idempotent end-state).
        self._run(engine, _mod.upgrade)
        rows = self._rows(engine)
        assert "Hewlett Packard Enterprise" not in rows
        assert rows["HPE"] == ["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]

    def test_upgrade_seed_race_drops_legacy_row_and_reasserts_aliases(self):
        # A fresh-seeded process already wrote the short "HPE" row before the migration
        # ran (startup seed races alembic) — the legacy long-name row must be DELETEd,
        # never left as a duplicate facet slot, and the survivor's aliases reasserted.
        engine = self._scratch_engine()
        self._seed(
            engine,
            [
                ("HPE", ["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]),
                ("Hewlett Packard Enterprise", ["HPE", "HP"]),  # stale legacy duplicate
                ("Texas Instruments", ["TI", "Texas Inst"]),
            ],
        )

        self._run(engine, _mod.upgrade)
        rows = self._rows(engine)
        assert "Hewlett Packard Enterprise" not in rows  # legacy duplicate removed
        assert rows["HPE"] == ["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]
        assert rows["Texas Instruments"] == ["TI", "Texas Inst", "Texas Instruments (TI)"]
