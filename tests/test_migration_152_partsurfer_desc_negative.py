"""Tests for migration 152 (partsurfer_desc_negative table).

What: Revision metadata checks (id length vs PG VARCHAR(32), chain wiring onto 124)
      plus an executable upgrade -> downgrade -> upgrade pass against a scratch
      in-memory SQLite engine -- the migration only uses portable create/drop DDL,
      so executing it here is honest coverage on both engines. Also asserts the
      unique (spare_norm) key dedupes negatives and the reason CHECK rejects garbage.
Called by: pytest
Depends on: alembic/versions/152_partsurfer_desc_negative.py
"""

import importlib.util
import os

import pytest
import sqlalchemy.exc
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "alembic", "versions", "152_partsurfer_desc_negative.py"
)
_spec = importlib.util.spec_from_file_location("migration_152", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "152_partsurfer_desc_negative"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "151_user_notify_prefs"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine."""

    @staticmethod
    def _run(engine, fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    @staticmethod
    def _exec(engine, stmt):
        with engine.begin() as conn:
            conn.execute(text(stmt))

    def test_upgrade_downgrade_upgrade(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        self._run(engine, _mod.upgrade)
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("partsurfer_desc_negative")}
        assert {
            "id",
            "spare_norm",
            "spare_raw",
            "reason",
            "looked_up_at",
            "retry_after",
            "created_at",
            "updated_at",
        } <= cols
        index_names = {i["name"] for i in insp.get_indexes("partsurfer_desc_negative")}
        assert "ix_partsurfer_neg_retry_after" in index_names

        # One row per spare_norm: a duplicate must collide on the unique key.
        row = (
            "INSERT INTO partsurfer_desc_negative (spare_norm, spare_raw, reason, looked_up_at, retry_after) "
            "VALUES ('875942001', '875942-001', 'no_result', '2026-06-19', '2026-09-17')"
        )
        self._exec(engine, row)
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            self._exec(engine, row)

        # ck_partsurfer_neg_reason: only the two known reasons are allowed.
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            self._exec(
                engine,
                "INSERT INTO partsurfer_desc_negative (spare_norm, spare_raw, reason, looked_up_at, retry_after) "
                "VALUES ('111111001', '111111-001', 'bogus', '2026-06-19', '2026-09-17')",
            )

        self._run(engine, _mod.downgrade)
        assert "partsurfer_desc_negative" not in inspect(engine).get_table_names()

        self._run(engine, _mod.upgrade)
        assert "partsurfer_desc_negative" in inspect(engine).get_table_names()
