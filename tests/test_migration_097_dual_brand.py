"""Structural + SQLite round-trip tests for migration 097 (dual-brand columns).

Asserts the migration's revision metadata (id <=32 chars — feedback_alembic_revision_id_
length, chains off 098_materials_perf_idx, on the single-head mainline) and that its
nine add_column calls + ix_material_cards_brand round-trip (upgrade → downgrade →
upgrade) on a scratch SQLite engine. The migration is purely additive, portable DDL (no data
writes), so executing its upgrade()/downgrade() directly is honest coverage on both
engines. Execution uses the hermetic MigrationContext + Operations.context pattern
(like test_migration_094_fru_links) rather than the in-process alembic CLI: the CLI
path routes through alembic/env.py + the alembic.op module's PROCESS-GLOBAL proxy and
an os.environ DATABASE_URL channel, which proved load-flaky under xdist (intermittent
"table missing" skips from the env.py idempotent wrappers when the full suite runs in
parallel).

Called by: pytest
Depends on: alembic/versions/097_dual_brand.py
"""

import importlib.util
import os

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import StaticPool

# Load the migration module directly (alembic/versions has no __init__.py).
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIGRATION_PATH = os.path.join(_REPO_ROOT, "alembic", "versions", "097_dual_brand.py")
_spec = importlib.util.spec_from_file_location("migration_097", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_BRAND_COLS = {"brand", "brand_source", "brand_confidence", "brand_tier", "brand_updated_at"}
_MFR_COLS = {
    "manufacturer_source",
    "manufacturer_confidence",
    "manufacturer_tier",
    "manufacturer_updated_at",
}


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "097_dual_brand"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres (feedback_alembic_revision_id_length).
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_off_098(self):
        # 098_materials_perf_idx (PR #262) deliberately skipped the 097 number this
        # branch had reserved — the chain runs 096 → 098 → 097 (numeric order is not
        # chain order; same precedent as 094 chaining over the reserved 092).
        assert _mod.down_revision == "098_materials_perf_idx"

    def test_on_single_head_mainline(self):
        # The migration chain must converge to exactly one head (no unmerged branches —
        # test_migration_chain.py owns that invariant) AND 097 must sit on the mainline
        # walked from that head. Reachability instead of a pinned head name, so this
        # test survives future migrations landing on top.
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1, f"expected a single head, got {heads}"
        mainline = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
        assert "097_dual_brand" in mainline, "097_dual_brand fell off the mainline walked from the head"


class TestRoundTrip:
    """Upgrade → downgrade → upgrade of the migration module's own DDL on scratch
    SQLite."""

    @staticmethod
    def _engine():
        engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        meta = sa.MetaData()
        sa.Table(
            "material_cards",
            meta,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("manufacturer", sa.String(255)),
        )
        meta.create_all(engine)
        return engine

    @staticmethod
    def _run(engine, fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    @staticmethod
    def _columns(engine):
        return {c["name"] for c in sa.inspect(engine).get_columns("material_cards")}

    @staticmethod
    def _indexes(engine):
        return {i["name"] for i in sa.inspect(engine).get_indexes("material_cards")}

    def test_upgrade_adds_nine_columns_and_index(self):
        engine = self._engine()
        self._run(engine, _mod.upgrade)

        assert (_BRAND_COLS | _MFR_COLS) <= self._columns(engine)
        assert "ix_material_cards_brand" in self._indexes(engine)

    def test_downgrade_drops_nine_columns_and_index(self):
        engine = self._engine()
        self._run(engine, _mod.upgrade)
        self._run(engine, _mod.downgrade)

        assert self._columns(engine).isdisjoint(_BRAND_COLS | _MFR_COLS)
        assert "ix_material_cards_brand" not in self._indexes(engine)

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = self._engine()
        self._run(engine, _mod.upgrade)
        self._run(engine, _mod.downgrade)
        self._run(engine, _mod.upgrade)

        assert (_BRAND_COLS | _MFR_COLS) <= self._columns(engine)
        assert "ix_material_cards_brand" in self._indexes(engine)
