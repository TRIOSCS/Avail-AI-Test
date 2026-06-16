"""Tests for migration 094 (fru_links table).

What: Revision metadata checks (id length vs PG VARCHAR(32), chain wiring onto 093)
      plus an executable upgrade → downgrade → upgrade pass against a scratch
      in-memory SQLite engine — the migration only uses portable create/drop DDL,
      so executing it here is honest coverage on both engines.
Called by: pytest
Depends on: alembic/versions/094_fru_links.py
"""

import importlib.util
import os

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "094_fru_links.py")
_spec = importlib.util.spec_from_file_location("migration_094", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "094_fru_links"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "093_normalize_legacy_categories"


class TestExecution:
    """Upgrade → downgrade → upgrade on a scratch SQLite engine."""

    @staticmethod
    def _run(engine, fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    def test_upgrade_downgrade_upgrade(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        self._run(engine, _mod.upgrade)
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("fru_links")}
        assert {
            "id",
            "fru_raw",
            "fru_norm",
            "related_raw",
            "related_norm",
            "rel_kind",
            "manufacturer",
            "description",
            "series",
            "machine",
            "qual_status",
            "qual_date",
            "note",
            "source_sheet",
            "created_at",
            "updated_at",
        } <= cols
        index_names = {i["name"] for i in insp.get_indexes("fru_links")}
        assert {"ix_fru_links_fru_norm", "ix_fru_links_related_norm"} <= index_names

        # The unique key must reject duplicate edges.
        insert_edge = text(
            "INSERT INTO fru_links (fru_raw, fru_norm, related_raw, related_norm, rel_kind, source_sheet) "
            "VALUES ('00AJ001', '00aj001', '68Y7789', '68y7789', 'ibm_11s', 'Main')"
        )
        with engine.begin() as conn:
            conn.execute(insert_edge)
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(insert_edge)

        self._run(engine, _mod.downgrade)
        assert not inspect(engine).has_table("fru_links")

        self._run(engine, _mod.upgrade)
        assert inspect(engine).has_table("fru_links")
