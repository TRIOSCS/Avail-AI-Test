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
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, CommoditySpecSchema, MaterialCard
from app.services.commodity_registry import reseed_changed_schemas, seed_commodity_schemas

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
        # 092 is permanently unused — the concurrent desc_extractor branch introduced no
        # schema change, so this revision chains straight onto 091 (gap is intentional;
        # never fill 092).
        assert _mod.down_revision == "091_cleanup_vague_descs"


class TestFrozenSnapshot:
    """Internal consistency of the FROZEN alias/canonical-key snapshot.

    Deliberately NOT coupled to the live normalizer or COMMODITY_TREE: the snapshot was
    verified equal to the live maps when this migration shipped and is frozen so the
    migration's behaviour can never drift afterwards. If CATEGORY_ALIASES /
    TRIO_SFDC_COMMODITY_CODES / COMMODITY_TREE legitimately evolves later (an alias
    retargeted, a tree key renamed), update or remove THESE TESTS — never the shipped
    migration. Only invariants internal to the snapshot itself are asserted here,
    because those can never legitimately change.
    """

    def test_alias_targets_are_canonical_within_snapshot(self):
        for raw, target in _mod._CATEGORY_ALIASES.items():
            assert target in _mod._CANONICAL_KEYS, f"{raw!r} -> {target!r} is not in the frozen canonical-key snapshot"

    def test_snapshot_keys_are_lower_trimmed(self):
        # The UPDATEs match on LOWER(TRIM(category)), so a non-lower/trimmed key could
        # never match anything — it would be silently dead weight in the snapshot.
        for raw in _mod._CATEGORY_ALIASES:
            assert raw == raw.strip().lower(), f"alias key {raw!r} must be lower/trimmed"
        for key in _mod._CANONICAL_KEYS:
            assert key == key.strip().lower(), f"canonical key {key!r} must be lower/trimmed"

    def test_canonical_keys_snapshot_has_no_duplicates(self):
        assert len(_mod._CANONICAL_KEYS) == len(set(_mod._CANONICAL_KEYS))

    def test_snapshot_covers_trio_vocabulary(self):
        # Hardcoded literals (not the live maps) — pins the frozen rewrite behaviour.
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
            # Soft-deleted row with a legacy category: the migration's documented
            # contract is that deleted rows normalize too (restoring a card must yield
            # a canonical category), so the UPDATEs must NOT filter on deleted_at.
            conn.execute(
                text(
                    "INSERT INTO material_cards (normalized_mpn, display_mpn, category, deleted_at) "
                    "VALUES ('del1', 'del1', 'Hard Drive', '2026-06-09 00:00:00')"
                )
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
            "del1": "hdd",  # soft-deleted — normalized too (restore must be canonical)
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

    def test_boot_seeding_after_upgrade_does_not_resurrect_series(self):
        """The migration→boot sequence every existing DB passes through on this deploy:

        after 093 retires connectors/series, the next boot's insert-only seeder +
        reconcile pass must NOT re-insert it (it is gone from the JSON seeds) while
        still inserting the new connectors extension specs.
        """
        engine = self._scratch_engine()
        self._run(engine, _mod.upgrade)

        with Session(engine) as session:
            seed_commodity_schemas(session)
            reseed_changed_schemas(session)
            series = session.query(CommoditySpecSchema).filter_by(commodity="connectors", spec_key="series").count()
            connectors_keys = {
                row[0] for row in session.query(CommoditySpecSchema.spec_key).filter_by(commodity="connectors").all()
            }
        assert series == 0  # retired row stays retired across boots
        assert {"orientation", "current_rating", "rows"} <= connectors_keys  # extensions seeded
