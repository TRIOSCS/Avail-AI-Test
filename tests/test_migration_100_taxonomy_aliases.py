"""Tests for migration 100 (post-093 distributor-taxonomy alias backfill).

What: Structural checks on the revision metadata + frozen alias snapshot, AND an
      executable upgrade→downgrade→upgrade pass against a scratch in-memory SQLite
      engine. Like 093, this migration uses portable UPDATE statements only, so
      executing it here is honest coverage (every statement is valid on both engines).
Called by: pytest
Depends on: alembic/versions/100_taxonomy_alias_backfill.py, conftest.py (the SQLite
            type adapters it installs at import), app.models.
"""

import importlib.util
import os

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.models import Base, MaterialCard

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "100_taxonomy_alias_backfill.py")
_spec = importlib.util.spec_from_file_location("migration_100", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    """Revision identifiers and chain wiring."""

    def test_revision_id(self):
        assert _mod.revision == "100_taxonomy_alias_backfill"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "099_on_add_enrich"


class TestFrozenSnapshot:
    """The snapshot is the four post-093 aliases ONLY — frozen, never the live map."""

    def test_snapshot_is_exactly_the_post_093_aliases(self):
        # Hardcoded literals (not the live map) — pins the frozen rewrite behaviour.
        assert _mod._NEW_ALIASES == {
            "hard drives": "hdd",
            "internal hard drives": "hdd",
            "memory module": "dram",
            "memory modules": "dram",
        }

    def test_snapshot_keys_are_lower_trimmed(self):
        # The UPDATEs match on LOWER(TRIM(category)) — a non-lower/trimmed key could
        # never match anything (silently dead weight).
        for raw in _mod._NEW_ALIASES:
            assert raw == raw.strip().lower(), f"alias key {raw!r} must be lower/trimmed"


class TestExecution:
    """Upgrade → downgrade → upgrade on a scratch SQLite engine."""

    def _scratch_engine(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine, tables=[MaterialCard.__table__])
        with engine.begin() as conn:
            for mpn, cat in [
                ("hd1", "Hard Drives"),
                ("hd2", "  internal hard drives  "),
                ("mm1", "Memory Module"),
                ("mm2", "Memory Modules"),
                ("ok1", "hdd"),  # already canonical — untouched
                ("none1", None),  # NULL — untouched
                ("unk1", "Totally Unknown Category"),  # unmapped — untouched, never guessed
            ]:
                conn.execute(
                    text("INSERT INTO material_cards (normalized_mpn, display_mpn, category) VALUES (:m, :m, :c)"),
                    {"m": mpn, "c": cat},
                )
            # Soft-deleted row: the documented contract is that deleted rows normalize
            # too (restoring a card must yield a canonical category).
            conn.execute(
                text(
                    "INSERT INTO material_cards (normalized_mpn, display_mpn, category, deleted_at) "
                    "VALUES ('del1', 'del1', 'Hard Drives', '2026-06-10 00:00:00')"
                )
            )
        return engine

    @staticmethod
    def _run(engine, fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    def test_upgrade_downgrade_upgrade_normalizes_new_aliases(self):
        engine = self._scratch_engine()

        expected = {
            "hd1": "hdd",
            "hd2": "hdd",
            "mm1": "dram",
            "mm2": "dram",
            "ok1": "hdd",
            "none1": None,
            "unk1": "Totally Unknown Category",
            "del1": "hdd",  # soft-deleted — normalized too
        }

        self._run(engine, _mod.upgrade)
        with engine.connect() as conn:
            got = dict(conn.execute(text("SELECT normalized_mpn, category FROM material_cards")).fetchall())
        assert got == expected

        # Downgrade is a documented no-op (many-to-one normalization is irreversible).
        self._run(engine, _mod.downgrade)
        with engine.connect() as conn:
            got = dict(conn.execute(text("SELECT normalized_mpn, category FROM material_cards")).fetchall())
        assert got == expected

        # Re-upgrade is idempotent.
        self._run(engine, _mod.upgrade)
        with engine.connect() as conn:
            got = dict(conn.execute(text("SELECT normalized_mpn, category FROM material_cards")).fetchall())
        assert got == expected
