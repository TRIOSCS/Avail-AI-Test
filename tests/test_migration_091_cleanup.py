"""Structural tests for migration 091 (SP1 vague-description / untrustworthy-stamp
cleanup).

Asserts the migration's revision metadata and the two WHERE-predicate string constants
carry the guards SP1 relies on. This is a STRUCTURAL test only — it inspects the module's
constants and identity, it does NOT execute the upgrade/downgrade.

The 091 data upgrade/downgrade itself is PostgreSQL-only SQL (CREATE TABLE AS ... SELECT,
UPDATE ... FROM, ILIKE) and so is validated read-only against live Postgres, NOT executed
on the in-memory SQLite test engine. SQLite tolerates Postgres-invalid SQL and would mask
real failures (project rule: feedback_sqlite_masks_postgres), so we deliberately keep this
test to module-level structure and never invoke op.get_bind()-backed code here.

Called by: pytest
Depends on: alembic/versions/091_cleanup_vague_descs.py
"""

import importlib.util
import os

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "091_cleanup_vague_descs.py")
_spec = importlib.util.spec_from_file_location("migration_091", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    """Revision identifiers and chain wiring."""

    def test_revision_id(self):
        assert _mod.revision == "091_cleanup_vague_descs"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "090_add_condition_mc"


class TestVagueDescWhere:
    """The hallucinated-description predicate must stay narrowly scoped."""

    def test_predicate_targets_never_sourced_not_found_cards(self):
        where = _mod._VAGUE_DESC_WHERE
        assert "not_found" in where
        assert "enrichment_source IS NULL" in where
        assert "deleted_at IS NULL" in where
        assert "description IS NOT NULL" in where

    def test_predicate_matches_hedging_tokens(self):
        where = _mod._VAGUE_DESC_WHERE
        for token in ("likely", "possibly", "may be", "proprietary", "appears to be", "could be"):
            assert token in where


class TestUntrustworthyStampWhere:
    """The stamp-clearing predicate must stay gated on untrustworthy status."""

    def test_predicate_clears_untrustworthy_specs_enriched_at(self):
        where = _mod._UNTRUSTWORTHY_STAMP_WHERE
        assert "specs_enriched_at" in where
        assert "deleted_at IS NULL" in where
        assert "verified" in where
        assert "web_sourced" in where
        assert "oem_sourced" in where
        assert "NOT IN" in where
