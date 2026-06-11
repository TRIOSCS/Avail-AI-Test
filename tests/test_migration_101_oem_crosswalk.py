"""Tests for migration 101 (oem_crosswalk table).

What: Revision metadata checks (id length vs PG VARCHAR(32), chain wiring onto 100)
      plus an executable upgrade → downgrade → upgrade pass against a scratch
      in-memory SQLite engine — the migration only uses portable create/drop DDL,
      so executing it here is honest coverage on both engines.
Called by: pytest
Depends on: alembic/versions/101_oem_crosswalk.py
"""

import importlib.util
import os

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "101_oem_crosswalk.py")
_spec = importlib.util.spec_from_file_location("migration_101", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "101_oem_crosswalk"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "100_taxonomy_alias_backfill"


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
        cols = {c["name"] for c in insp.get_columns("oem_crosswalk")}
        assert {
            "id",
            "spare_raw",
            "spare_norm",
            "vendor",
            "status",
            "canonical_mpn_raw",
            "canonical_mpn_norm",
            "canonical_manufacturer",
            "title",
            "confidence",
            "source_url",
            "source_domain",
            "payload",
            "looked_up_at",
            "created_at",
            "updated_at",
        } <= cols
        index_names = {i["name"] for i in insp.get_indexes("oem_crosswalk")}
        assert {
            "ix_oem_crosswalk_spare_norm",
            "ix_oem_crosswalk_canonical_norm",
            "ix_oem_crosswalk_status",
        } <= index_names

        # The unique key must reject duplicate (spare_norm, vendor, source_domain) edges.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, canonical_mpn_norm, "
                    "source_domain, looked_up_at) VALUES ('875942-001', '875942001', 'hpe', 'resolved', "
                    "'cd8067303409000', 'partsurfer.hp.com', '2026-06-10')"
                )
            )
        import sqlalchemy.exc

        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, canonical_mpn_norm, "
                        "source_domain, looked_up_at) VALUES ('875942-001', '875942001', 'hpe', 'resolved', "
                        "'cd8067303409000', 'partsurfer.hp.com', '2026-06-10')"
                    )
                )
            raise AssertionError("duplicate edge insert should have violated uq_oem_crosswalk_edge")
        except sqlalchemy.exc.IntegrityError:
            pass

        # no_match rows carry the '' sentinel domain (server default), NOT NULL —
        # NULLs are pairwise-distinct in a UNIQUE constraint, so a nullable domain
        # would never dedupe negatives. Two no_match rows for the same (spare_norm,
        # vendor) must collide.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, looked_up_at) "
                    "VALUES ('918042-601', '918042601', 'hpe', 'no_match', '2026-06-10')"
                )
            )
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, looked_up_at) "
                        "VALUES ('918042-601', '918042601', 'hpe', 'no_match', '2026-06-11')"
                    )
                )
            raise AssertionError("duplicate no_match insert should have violated uq_oem_crosswalk_edge")
        except sqlalchemy.exc.IntegrityError:
            pass

        # ck_oem_crosswalk_status_canonical: canonical_mpn_norm is non-NULL iff
        # status='resolved' — both illegal shapes must be rejected.
        for stmt in (
            "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, looked_up_at) "
            "VALUES ('111111-001', '111111001', 'hpe', 'resolved', '2026-06-10')",
            "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, canonical_mpn_norm, looked_up_at) "
            "VALUES ('222222-001', '222222001', 'hpe', 'no_match', 'st4000nm0035', '2026-06-10')",
        ):
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                raise AssertionError("insert should have violated ck_oem_crosswalk_status_canonical")
            except sqlalchemy.exc.IntegrityError:
                pass

        self._run(engine, _mod.downgrade)
        assert "oem_crosswalk" not in inspect(engine).get_table_names()

        self._run(engine, _mod.upgrade)
        assert "oem_crosswalk" in inspect(engine).get_table_names()
