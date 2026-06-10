"""Structural + ownership + SQLite round-trip tests for migration 098 (post-ingest
materials perf indexes).

Asserts the migration's revision metadata (id <=32 chars, chains off
096_spec_provenance with 097 reserved/skipped, single head), the integrity of its
declarative index lists (eight 098-owned names in _NEW_INDEXES plus the two
eabe89205d07-owned names in _HISTORICAL_INDEXES), the upgrade/downgrade ownership split
(upgrade creates all ten, idempotently only for the historical pair; downgrade drops
ONLY the eight 098-owned names — eabe89205d07 is still applied at 096 and owns the
other two), and that it round-trips via the real alembic CLI machinery on a SQLite file
DB (stamped at 096, then upgrade→downgrade→upgrade of the 098 step). The DDL itself is
PostgreSQL-only (GIN, pg_trgm, partial expression indexes, CREATE STATISTICS) and the
migration guards the whole body to PG — on SQLite the round trip verifies the guard
no-ops cleanly without touching the table (project rule feedback_sqlite_masks_postgres:
the index plans were verified against a live-volume Postgres scratch copy, not SQLite).

Called by: pytest
Depends on: alembic/versions/098_materials_perf_idx.py
"""

import importlib.util
import os
import tempfile
from unittest import mock

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

# Load the migration module directly (alembic/versions has no __init__.py).
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIGRATION_PATH = os.path.join(_REPO_ROOT, "alembic", "versions", "098_materials_perf_idx.py")
_spec = importlib.util.spec_from_file_location("migration_098", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_NEW_NAMES = {name for name, _cols, _kw in _mod._NEW_INDEXES}
_HISTORICAL_NAMES = {name for name, _cols, _kw in _mod._HISTORICAL_INDEXES}
_ALL_NAMES = _NEW_NAMES | _HISTORICAL_NAMES


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

    def test_historical_owner_still_on_mainline(self):
        # The two _HISTORICAL_INDEXES names are owned by eabe89205d07, which must
        # remain a live ancestor of 098 — the ownership split (downgrade keeps the
        # pair) is only correct while that revision is still in the applied history.
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))
        script = ScriptDirectory.from_config(cfg)
        ancestors = {rev.revision for rev in script.iterate_revisions(_mod.revision, "base")}
        assert "eabe89205d07" in ancestors, (
            "eabe89205d07 is no longer an ancestor of 098 — if it was removed from the "
            "chain, 098 must take over ownership (create AND drop) of "
            f"{sorted(_HISTORICAL_NAMES)}"
        )


class TestIndexList:
    """The index lists drive both upgrade and downgrade — guard their integrity."""

    def test_eight_new_plus_two_historical_uniquely_named(self):
        new = [name for name, _cols, _kw in _mod._NEW_INDEXES]
        historical = [name for name, _cols, _kw in _mod._HISTORICAL_INDEXES]
        assert len(new) == 8
        assert len(historical) == 2
        assert len(set(new + historical)) == 10, "duplicate index names would make the ownership split lossy"

    def test_historical_names_are_the_eabe89205d07_pair(self):
        # These two names are created by ancestor eabe89205d07 (still in the chain) on
        # every fresh replay. 098 re-creates them IF NOT EXISTS only because the live
        # DB was stamped past eabe89205d07 without executing it (its trigger/function
        # are also absent on live), so the live schema lacked them out-of-band.
        assert _HISTORICAL_NAMES == {"ix_material_cards_search_vector", "ix_material_cards_trgm_mpn"}

    def test_gin_indexes_marked_postgresql_using(self):
        gin = {
            name
            for name, _cols, kw in _mod._NEW_INDEXES + _mod._HISTORICAL_INDEXES
            if kw.get("postgresql_using") == "gin"
        }
        assert gin == {
            "ix_material_cards_search_vector",
            "ix_material_cards_trgm_mpn",
            "ix_mc_trgm_norm_mpn",
            "ix_mc_trgm_manufacturer",
            "ix_mc_trgm_description",
        }

    def test_partial_indexes_carry_postgresql_where(self):
        partial = {name for name, _cols, kw in _mod._NEW_INDEXES + _mod._HISTORICAL_INDEXES if "postgresql_where" in kw}
        assert partial == {
            "ix_mc_order_live",
            "ix_mc_cat_order_live",
            "ix_mc_has_datasheet",
            "ix_mc_has_crosses",
            "ix_mc_last_searched",
        }


class TestOwnershipSplit:
    """Pin exactly which names each direction touches, with the alembic ``op`` proxy
    mocked out and the bind reporting postgresql (the real DDL is PG-only).

    This is the regression guard for the downgrade-over-drop bug: 098's downgrade must
    NOT drop ix_material_cards_search_vector / ix_material_cards_trgm_mpn, because at
    096 ancestor eabe89205d07 is still applied and its history says they exist — only
    its own downgrade may remove them. Dropping them here would leave a downgraded DB
    silently missing the FTS index that multi-word q= search depends on.
    """

    def _run(self, direction: str) -> mock.MagicMock:
        fake_op = mock.MagicMock()
        fake_op.get_bind.return_value.dialect.name = "postgresql"
        with mock.patch.object(_mod, "op", fake_op):
            getattr(_mod, direction)()
        return fake_op

    def test_upgrade_creates_all_ten_idempotent_only_for_historical(self):
        fake_op = self._run("upgrade")
        created = {call.args[0]: call.kwargs for call in fake_op.create_index.call_args_list}
        assert set(created) == _ALL_NAMES
        for name, kwargs in created.items():
            # if_not_exists only where pre-existence is EXPECTED (fresh replays run
            # eabe89205d07 first); the eight new names must fail loudly on collision —
            # a same-named leftover is out-of-band DDL with a possibly different
            # definition, and IF NOT EXISTS would silently keep the wrong one.
            assert kwargs.get("if_not_exists", False) is (name in _HISTORICAL_NAMES), name

    def test_downgrade_drops_only_the_eight_098_owned_names(self):
        fake_op = self._run("downgrade")
        dropped = {call.args[0] for call in fake_op.drop_index.call_args_list}
        assert dropped == _NEW_NAMES
        assert dropped.isdisjoint(_HISTORICAL_NAMES)


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

        fd, dbf = tempfile.mkstemp(suffix=".db")
        os.close(fd)  # only the path is wanted — don't leak the handle
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

        command.upgrade(cfg, _mod.revision)
        # PG-only guard: SQLite must come out the other side with NO new indexes —
        # a partial/GIN index silently created here would mask PG-only semantics.
        assert self._index_names(engine).isdisjoint(_ALL_NAMES)

        command.downgrade(cfg, _mod.down_revision)
        assert self._index_names(engine).isdisjoint(_ALL_NAMES)

        command.upgrade(cfg, _mod.revision)
        assert self._index_names(engine).isdisjoint(_ALL_NAMES)
