"""Structural + SQLite round-trip tests for migration 098 (post-ingest materials perf
indexes).

Asserts the migration's revision metadata (id <=32 chars, chains off
096_spec_provenance with 097 reserved/skipped, single head), the integrity of its
declarative _INDEXES list (10 uniquely-named material_cards indexes, restoring the two
FTS/trgm names lost in the migration-001 rewrite), and that it round-trips via the real
alembic CLI machinery on a SQLite file DB (stamped at 096, then
upgrade→downgrade→upgrade of the 098 step). The DDL itself is PostgreSQL-only (GIN,
pg_trgm, partial expression indexes, CREATE STATISTICS) and the migration guards the
whole body to PG — on SQLite the round trip verifies the guard no-ops cleanly without
touching the table (project rule feedback_sqlite_masks_postgres: the index plans were
verified against a live-volume Postgres scratch copy, not SQLite).

Called by: pytest
Depends on: alembic/versions/098_materials_perf_idx.py
"""

import importlib.util
import os
import tempfile

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

# Load the migration module directly (alembic/versions has no __init__.py).
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIGRATION_PATH = os.path.join(_REPO_ROOT, "alembic", "versions", "098_materials_perf_idx.py")
_spec = importlib.util.spec_from_file_location("migration_098", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "098_materials_perf_idx"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres (feedback_alembic_revision_id_length).
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_off_spec_provenance(self):
        # 097 is skipped (reserved by a concurrent branch when this shipped) — same
        # precedent as 094 chaining over the reserved 092.
        assert _mod.down_revision == "096_spec_provenance"

    def test_on_single_head_mainline(self):
        # The migration chain must converge to exactly one head (no unmerged branches —
        # test_migration_chain.py owns that invariant) AND 098 must sit on the mainline
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
        assert "098_materials_perf_idx" in mainline, "098_materials_perf_idx fell off the mainline walked from the head"


class TestIndexList:
    """The _INDEXES list drives both upgrade and downgrade — guard its integrity."""

    def test_ten_uniquely_named_indexes(self):
        names = [name for name, _cols, _kw in _mod._INDEXES]
        assert len(names) == 10
        assert len(set(names)) == 10, "duplicate index names would make downgrade lossy"

    def test_restored_fts_and_trgm_names(self):
        # These two names existed pre-001-rewrite (old revision eabe89205d07) and were
        # lost from the chain; 098 restores them under their historical names so any
        # environment that still has them is a clean idempotent no-op.
        names = {name for name, _cols, _kw in _mod._INDEXES}
        assert {"ix_material_cards_search_vector", "ix_material_cards_trgm_mpn"} <= names

    def test_gin_indexes_marked_postgresql_using(self):
        gin = {name for name, _cols, kw in _mod._INDEXES if kw.get("postgresql_using") == "gin"}
        assert gin == {
            "ix_material_cards_search_vector",
            "ix_material_cards_trgm_mpn",
            "ix_mc_trgm_norm_mpn",
            "ix_mc_trgm_manufacturer",
            "ix_mc_trgm_description",
        }

    def test_partial_indexes_carry_postgresql_where(self):
        partial = {name for name, _cols, kw in _mod._INDEXES if "postgresql_where" in kw}
        assert partial == {
            "ix_mc_order_live",
            "ix_mc_cat_order_live",
            "ix_mc_has_datasheet",
            "ix_mc_has_crosses",
            "ix_mc_last_searched",
        }


class TestRoundTrip:
    """Drive the migration through the real alembic CLI machinery on a SQLite file DB.

    The full chain cannot replay on SQLite (the 001 baseline issues ``CREATE EXTENSION
    pg_trgm``), so we create only material_cards, ``stamp`` at 096_spec_provenance
    (stamping records the version WITHOUT executing it), then upgrade/downgrade exactly
    the 098 step. 098's body is guarded to PostgreSQL, so on SQLite this verifies the
    guard returns cleanly in both directions and the version bookkeeping round-trips.
    """

    @pytest.fixture
    def alembic_on_sqlite(self):
        from alembic.config import Config

        from alembic import command

        dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        url = f"sqlite:///{dbf}"
        engine = sa.create_engine(url)

        meta = sa.MetaData()
        sa.Table(
            "material_cards",
            meta,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("category", sa.String(255)),
            sa.Column("search_count", sa.Integer),
            sa.Column("created_at", sa.DateTime),
            sa.Column("deleted_at", sa.DateTime),
            sa.Column("last_searched_at", sa.DateTime),
            sa.Column("datasheet_url", sa.String(1000)),
            sa.Column("cross_references", sa.JSON),
            sa.Column("display_mpn", sa.String(255)),
            sa.Column("normalized_mpn", sa.String(255)),
            sa.Column("manufacturer", sa.String(255)),
            sa.Column("description", sa.String(1000)),
        )
        meta.create_all(engine)

        cfg = Config()
        cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        prev_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        # Record 096_spec_provenance (the down-revision) as applied WITHOUT running it.
        command.stamp(cfg, _mod.down_revision)
        try:
            yield engine, cfg, command
        finally:
            if prev_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev_url
            engine.dispose()
            os.unlink(dbf)

    def _index_names(self, engine):
        return {ix["name"] for ix in inspect(engine).get_indexes("material_cards")}

    def test_upgrade_downgrade_upgrade_round_trips_as_noop_on_sqlite(self, alembic_on_sqlite):
        engine, cfg, command = alembic_on_sqlite
        migration_names = {name for name, _cols, _kw in _mod._INDEXES}

        command.upgrade(cfg, _mod.revision)
        # PG-only guard: SQLite must come out the other side with NO new indexes —
        # a partial/GIN index silently created here would mask PG-only semantics.
        assert self._index_names(engine).isdisjoint(migration_names)

        command.downgrade(cfg, _mod.down_revision)
        assert self._index_names(engine).isdisjoint(migration_names)

        command.upgrade(cfg, _mod.revision)
        assert self._index_names(engine).isdisjoint(migration_names)
