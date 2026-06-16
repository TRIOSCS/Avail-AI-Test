"""Structural + SQLite round-trip tests for migration 096 (SP2 spec/category
provenance).

Asserts the migration's revision metadata (id <=32 chars, chains off 095_wechat_id,
single head), that its SQL CASE tier snapshot stays in sync with the live
spec_tiers.SOURCE_TIER map, and that its seven add_column / drop_column calls round-trip
(upgrade→downgrade→upgrade) on a scratch SQLite engine via the shared hermetic harness
(tests/migration_harness.run_ops — see TestRoundTrip docstring for why the in-process
alembic CLI was dropped). The PG-only JSONB/category backfill is NOT
executed here — SQLite has no JSONB operators and the migration guards the data step to
PostgreSQL (project rule feedback_sqlite_masks_postgres: SQLite masks PG JSON ops, so the
backfill SQL is verified against live Postgres, not on SQLite).

Called by: pytest
Depends on: alembic/versions/096_spec_provenance.py
"""

import importlib.util
import os

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

# Load the migration module directly (alembic/versions has no __init__.py).
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIGRATION_PATH = os.path.join(_REPO_ROOT, "alembic", "versions", "096_spec_provenance.py")
_spec = importlib.util.spec_from_file_location("migration_096", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "096_spec_provenance"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres (feedback_alembic_revision_id_length).
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_off_wechat_id(self):
        # Re-parented onto main's 095_wechat_id head (was 091, then 094, pre-merge) —
        # re-parenting beats a no-op merge revision for a not-yet-deployed migration.
        assert _mod.down_revision == "095_wechat_id"

    def test_on_single_head_mainline(self):
        # The migration chain must converge to exactly one head (no unmerged branches —
        # test_migration_chain.py owns that invariant) AND 096 must sit on the mainline
        # walked from that head. Asserting reachability instead of pinning the head name
        # keeps this test from rotting every time a newer migration lands (it broke when
        # 098 became the head).
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1, f"expected a single head, got {heads}"
        mainline = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
        assert "096_spec_provenance" in mainline, "096_spec_provenance fell off the mainline walked from the head"

    def test_source_tier_sql_case_matches_live_ladder(self):
        # The migration cannot import app code, so its CASE is a literal snapshot of
        # spec_tiers.SOURCE_TIER. This parses the literal and asserts EXACT equality —
        # a new ladder source (e.g. desc_parse, fru_matrix_decode) added without
        # updating the migration would backfill its facet rows to tier 0 (ELSE branch),
        # silently misranking them below ai_guess. This test makes that drift a CI failure.
        import re as _re

        from app.services.spec_tiers import SOURCE_TIER

        parsed = {
            m.group(1): int(m.group(2)) for m in _re.finditer(r"WHEN '([^']+)' THEN (\d+)", _mod._SOURCE_TIER_SQL_CASE)
        }
        assert parsed == SOURCE_TIER


class TestRoundTrip:
    """Upgrade → downgrade → upgrade of 096's own DDL on a scratch SQLite engine.

    The full migration chain cannot replay on SQLite (the 001 baseline issues
    ``CREATE EXTENSION pg_trgm``), so we create only the two tables 096 touches and
    execute the migration module's upgrade()/downgrade() directly through the shared
    hermetic harness (tests/migration_harness.run_ops). Previously this drove the
    in-process alembic CLI (command.stamp/upgrade), but that path routes through
    alembic/env.py + the alembic.op module's PROCESS-GLOBAL proxy and an os.environ
    DATABASE_URL channel, which proved load-flaky under xdist (intermittent "table
    missing" skips from env.py's idempotent wrappers while the full suite runs in
    parallel). The PG-only JSONB/category backfill is guarded inside the migration and
    no-ops on SQLite.
    """

    @staticmethod
    def _engine():
        engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        meta = sa.MetaData()
        sa.Table(
            "material_cards",
            meta,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("category", sa.String(255)),
            sa.Column("specs_structured", sa.JSON),
        )
        sa.Table(
            "material_spec_facets",
            meta,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("material_card_id", sa.Integer),
            sa.Column("spec_key", sa.String(100)),
        )
        meta.create_all(engine)
        return engine

    _run = staticmethod(run_ops)

    # The seven columns 096 adds, grouped by table.
    _FACET_COLUMNS = {"source", "confidence", "tier"}
    _CARD_COLUMNS = {"category_source", "category_confidence", "category_tier", "category_updated_at"}

    def _columns(self, engine, table):
        return {c["name"] for c in inspect(engine).get_columns(table)}

    def _assert_columns_present(self, engine):
        assert self._FACET_COLUMNS <= self._columns(engine, "material_spec_facets")
        assert self._CARD_COLUMNS <= self._columns(engine, "material_cards")

    def test_upgrade_adds_seven_columns(self):
        engine = self._engine()
        self._run(engine, _mod.upgrade)

        self._assert_columns_present(engine)

    def test_downgrade_drops_seven_columns(self):
        engine = self._engine()
        self._run(engine, _mod.upgrade)
        self._run(engine, _mod.downgrade)

        assert self._columns(engine, "material_spec_facets").isdisjoint(self._FACET_COLUMNS)
        assert self._columns(engine, "material_cards").isdisjoint(self._CARD_COLUMNS)

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = self._engine()
        self._run(engine, _mod.upgrade)
        self._run(engine, _mod.downgrade)
        self._run(engine, _mod.upgrade)

        self._assert_columns_present(engine)
