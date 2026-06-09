"""Structural + SQLite round-trip tests for migration 092 (SP2 spec/category
provenance).

Asserts the migration's revision metadata (id <=32 chars, chains off SP1's 091, single
head) and that its six add_column / drop_column calls round-trip via the real alembic CLI
machinery on a SQLite file DB (stamped at SP1's data-only 091, then upgrade→downgrade→upgrade
of the 092 step). The PG-only JSONB/category backfill is NOT executed here — SQLite has no
JSONB operators and the migration guards the data step to PostgreSQL (project rule
feedback_sqlite_masks_postgres: SQLite masks PG JSON ops, so the backfill SQL is verified
against live Postgres, not on SQLite).

Called by: pytest
Depends on: alembic/versions/092_spec_provenance.py
"""

import importlib.util
import os
import tempfile

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

# Load the migration module directly (alembic/versions has no __init__.py).
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIGRATION_PATH = os.path.join(_REPO_ROOT, "alembic", "versions", "092_spec_provenance.py")
_spec = importlib.util.spec_from_file_location("migration_092", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "092_spec_provenance"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres (feedback_alembic_revision_id_length).
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_off_sp1(self):
        assert _mod.down_revision == "091_cleanup_vague_descs"

    def test_single_head(self):
        # The migration chain must converge to exactly one head (no unmerged branches).
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert list(heads) == ["092_spec_provenance"], f"expected single head 092, got {heads}"


class TestRoundTrip:
    """Drive the migration through the real alembic CLI machinery on a SQLite file DB.

    The full migration chain cannot replay on SQLite (the 001 baseline issues
    ``CREATE EXTENSION pg_trgm``), so we instead create only the two tables 092 touches,
    ``stamp`` the DB at SP1's 091 (which is data-only PG SQL — stamping records the version
    WITHOUT executing it), then ``upgrade``/``downgrade`` exactly the 092 step. This is a
    genuine ``alembic upgrade → downgrade → upgrade`` of 092's DDL, not a hand-rolled op
    invocation, so it exercises the real add_column/drop_column the deploy will run. The
    PG-only JSONB/category backfill is guarded inside the migration and no-ops on SQLite.
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

        cfg = Config()
        cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        prev_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        # Record SP1's data-only 091 as applied WITHOUT running its PG-only SQL.
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

    def _columns(self, engine, table):
        return {c["name"] for c in inspect(engine).get_columns(table)}

    def test_upgrade_adds_six_columns(self, alembic_on_sqlite):
        engine, cfg, command = alembic_on_sqlite
        command.upgrade(cfg, _mod.revision)

        assert {"source", "confidence", "tier"} <= self._columns(engine, "material_spec_facets")
        assert {"category_source", "category_confidence", "category_tier"} <= self._columns(engine, "material_cards")

    def test_downgrade_drops_six_columns(self, alembic_on_sqlite):
        engine, cfg, command = alembic_on_sqlite
        command.upgrade(cfg, _mod.revision)
        command.downgrade(cfg, _mod.down_revision)

        assert self._columns(engine, "material_spec_facets").isdisjoint({"source", "confidence", "tier"})
        assert self._columns(engine, "material_cards").isdisjoint(
            {"category_source", "category_confidence", "category_tier"}
        )

    def test_upgrade_downgrade_upgrade_round_trips(self, alembic_on_sqlite):
        engine, cfg, command = alembic_on_sqlite
        command.upgrade(cfg, _mod.revision)
        command.downgrade(cfg, _mod.down_revision)
        command.upgrade(cfg, _mod.revision)

        assert {"source", "confidence", "tier"} <= self._columns(engine, "material_spec_facets")
        assert {"category_source", "category_confidence", "category_tier"} <= self._columns(engine, "material_cards")
