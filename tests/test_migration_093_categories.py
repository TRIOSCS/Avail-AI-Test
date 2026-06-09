"""Tests for migration 093 (legacy material_cards.category normalization).

What: Structural checks on the revision metadata + frozen alias snapshot, AND an
      executable upgrade→downgrade→upgrade pass against a scratch in-memory SQLite
      engine. Unlike 091 (PostgreSQL-only SQL), 093 uses portable UPDATE/DELETE/INSERT
      statements, so actually executing it here is honest coverage rather than a
      SQLite-masks-Postgres trap — every statement it runs is valid on both engines.
Called by: pytest
Depends on: alembic/versions/093_normalize_legacy_categories.py, conftest.py (the
            SQLite type adapters it installs at import), app.models.
"""

import importlib.util
import os

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.models import Base, CommoditySpecSchema, MaterialCard
from app.services.category_normalizer import normalize_category

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "alembic", "versions", "093_normalize_legacy_categories.py"
)
_spec = importlib.util.spec_from_file_location("migration_093", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    """Revision identifiers and chain wiring."""

    def test_revision_id(self):
        assert _mod.revision == "093_normalize_legacy_categories"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        # 092 is reserved by a concurrent branch — this revision deliberately chains
        # onto 091 and skips the number.
        assert _mod.down_revision == "091_cleanup_vague_descs"


class TestFrozenSnapshot:
    """The frozen alias snapshot must agree with the live normalizer at time of
    writing."""

    def test_snapshot_pairs_match_live_normalizer(self):
        for raw, target in _mod._CATEGORY_ALIASES.items():
            assert normalize_category(raw) == target, f"{raw!r} drifted from the live alias map"

    def test_snapshot_covers_trio_vocabulary(self):
        for raw, target in [
            ("main board", "motherboards"),
            ("hard drive", "hdd"),
            ("memory", "dram"),
            ("lcd", "displays"),
            ("lcd assy", "displays"),
            ("psu", "power_supplies"),
            ("graphics card", "gpu"),
            ("tape drive", "tape_drives"),
            ("ic", "ics_other"),
            ("oem assy", "oem_assemblies"),
            ("integrated circuits (ics)", "ics_other"),
        ]:
            assert _mod._CATEGORY_ALIASES[raw] == target

    def test_canonical_keys_snapshot_resolves_via_normalizer(self):
        for key in _mod._CANONICAL_KEYS:
            assert normalize_category(key) == key, f"{key!r} is not canonical in the live tree"


class TestExecution:
    """Upgrade → downgrade → upgrade on a scratch SQLite engine."""

    def _scratch_engine(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine, tables=[MaterialCard.__table__, CommoditySpecSchema.__table__])
        with engine.begin() as conn:
            for mpn, cat in [
                ("ic1", "Integrated Circuits (ICs)"),
                ("cap1", "Capacitors"),
                ("res1", "Resistors"),
                ("hd1", "Hard Drive"),
                ("lcd1", "LCD ASSY"),
                ("mem1", "  Memory  "),
                ("ok1", "ssd"),
                ("none1", None),
                ("unk1", "Totally Unknown Category"),
            ]:
                conn.execute(
                    text("INSERT INTO material_cards (normalized_mpn, display_mpn, category) VALUES (:m, :m, :c)"),
                    {"m": mpn, "c": cat},
                )
            conn.execute(
                text(
                    "INSERT INTO commodity_spec_schemas "
                    "(commodity, spec_key, display_name, data_type, sort_order, is_filterable, is_primary) "
                    "VALUES ('connectors', 'series', 'Series', 'enum', 6, 1, 0)"
                )
            )
        return engine

    @staticmethod
    def _run(engine, fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    def test_upgrade_downgrade_upgrade_normalizes_legacy_categories(self):
        engine = self._scratch_engine()

        self._run(engine, _mod.upgrade)

        with engine.connect() as conn:
            got = dict(conn.execute(text("SELECT normalized_mpn, category FROM material_cards")).fetchall())
            series = conn.execute(
                text("SELECT COUNT(*) FROM commodity_spec_schemas WHERE spec_key = 'series'")
            ).scalar()
        assert got == {
            "ic1": "ics_other",
            "cap1": "capacitors",
            "res1": "resistors",
            "hd1": "hdd",
            "lcd1": "displays",
            "mem1": "dram",
            "ok1": "ssd",  # already canonical — untouched
            "none1": None,  # NULL — untouched
            "unk1": "Totally Unknown Category",  # unmapped — untouched, never guessed
        }
        assert series == 0  # retired open-vocab facet removed

        # Downgrade: category normalization is a documented no-op; the series schema
        # row IS deterministically restored.
        self._run(engine, _mod.downgrade)
        with engine.connect() as conn:
            cat = conn.execute(text("SELECT category FROM material_cards WHERE normalized_mpn = 'ic1'")).scalar()
            series = conn.execute(
                text("SELECT COUNT(*) FROM commodity_spec_schemas WHERE spec_key = 'series'")
            ).scalar()
        assert cat == "ics_other"  # not reversible by design
        assert series == 1

        # Second upgrade is a clean no-op on categories and re-retires series.
        self._run(engine, _mod.upgrade)
        with engine.connect() as conn:
            got2 = dict(conn.execute(text("SELECT normalized_mpn, category FROM material_cards")).fetchall())
            series = conn.execute(
                text("SELECT COUNT(*) FROM commodity_spec_schemas WHERE spec_key = 'series'")
            ).scalar()
        assert got2 == got
        assert series == 0
