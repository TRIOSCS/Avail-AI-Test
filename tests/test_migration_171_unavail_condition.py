"""Tests for migration 171 (vendor_part_unavailability.condition + partial unique
indexes).

What: revision metadata only — id <= 32 vs PG VARCHAR(32), chains onto
      170_prospecting_persistence.

NOTE: No SQLite run_ops round-trip here. op.drop_constraint(type_="unique") has no
      SQLite ALTER DROP CONSTRAINT equivalent and would raise NotImplementedError.
      The behavioral DDL correctness (upgrade -> downgrade -> upgrade) is verified by
      the controller on a throwaway Postgres instance separately.
Called by: pytest
Depends on: alembic/versions/171_unavail_condition.py
"""

import importlib.util
import os

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "alembic",
    "versions",
    "171_unavail_condition.py",
)
_spec = importlib.util.spec_from_file_location("migration_171", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "171_unavail_condition"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "170_prospecting_persistence"
