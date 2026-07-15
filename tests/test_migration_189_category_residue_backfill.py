"""Tests for migration 189 (2026-07 category residue backfill).

What: Structural checks on the revision metadata + frozen snapshots, drift gates pinning
      the snapshots against the RUNTIME category_normalizer map / commodity registry
      (a later retarget of any 189 alias must break CI — the POST_093_ALIASES gate only
      checks registration, not targets), AND an executable upgrade→downgrade→upgrade
      pass against a scratch in-memory SQLite engine exercising all three SQL passes.
      Like 093/100, this migration uses portable UPDATE statements only, so executing
      it here is honest coverage (every statement is valid on both engines).
Called by: pytest
Depends on: alembic/versions/189_category_residue_backfill.py,
            alembic/versions/093_normalize_legacy_categories.py,
            app.services.category_normalizer, app.services.commodity_registry,
            conftest.py (the SQLite type adapters it installs at import), app.models.
"""

import importlib.util
import os

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.models import Base, MaterialCard
from app.services.category_normalizer import CATEGORY_ALIASES
from app.services.commodity_registry import get_all_commodities

# Load the migration module directly since alembic/versions has no __init__.py.
_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "alembic", "versions", "189_category_residue_backfill.py"
)
_spec = importlib.util.spec_from_file_location("migration_189", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _migration_093():
    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "093_normalize_legacy_categories.py")
    spec = importlib.util.spec_from_file_location("migration_093_for_189_gates", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRevisionMetadata:
    """Revision identifiers and chain wiring."""

    def test_revision_id(self):
        assert _mod.revision == "189_category_residue_backfill"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores the
        # length so an over-long id would pass tests but crash-loop on deploy.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "188_canonical_offers_excess_fk"


class TestFrozenSnapshots:
    """Snapshot-internal invariants: the three passes must be well-formed and disjoint."""

    def test_snapshot_keys_are_lower_trimmed(self):
        # The UPDATEs match on LOWER(TRIM(category)) — a non-lower/trimmed key could
        # never match anything (silently dead weight).
        for raw in list(_mod._NEW_ALIASES) + list(_mod._REALIASED) + list(_mod._CANONICAL_KEYS):
            assert raw == raw.strip().lower(), f"snapshot key {raw!r} must be lower/trimmed"

    def test_snapshot_passes_are_disjoint(self):
        # A key present in two passes would be rewritten twice (second pass fighting the
        # first); an alias key that IS a canonical key would fight the lowercase pass.
        new_keys, realiased, canonical = set(_mod._NEW_ALIASES), set(_mod._REALIASED), set(_mod._CANONICAL_KEYS)
        assert not new_keys & realiased
        assert not (new_keys | realiased) & canonical

    def test_new_alias_snapshot_size(self):
        # The 2026-07 residue remap block is exactly 64 aliases — a changed count means
        # the FROZEN migration file itself was edited after it shipped.
        assert len(_mod._NEW_ALIASES) == 64
        assert len(_mod._REALIASED) == 8


class TestRuntimeDriftGates:
    """Frozen snapshots ↔ runtime map: a later retarget of any 189 alias breaks CI.

    The POST_093_ALIASES gate in tests/test_category_normalizer.py only checks that each
    189 alias is REGISTERED with a backfill reference — it does not pin the target, so a
    retarget to a different (still-valid) tree key would pass it while stranding every
    row 189 already rewrote. These gates pin the targets.
    """

    def test_new_aliases_match_runtime_map(self):
        for raw, target in _mod._NEW_ALIASES.items():
            assert CATEGORY_ALIASES.get(raw) == target, (
                f"alias {raw!r} was retargeted or removed ({target!r} -> {CATEGORY_ALIASES.get(raw)!r}) after "
                "migration 189 rewrote rows to the frozen target — ship a follow-up data migration for the "
                "stranded rows, never edit the frozen 189 snapshot"
            )

    def test_realiased_strings_match_runtime_map_and_093(self):
        # Pass (b)'s stated invariant: each re-run target equals BOTH the current
        # runtime target and 093's frozen target, so nothing strands on either side.
        frozen_093 = _migration_093()._CATEGORY_ALIASES
        for raw, target in _mod._REALIASED.items():
            assert CATEGORY_ALIASES.get(raw) == target, (
                f"re-aliased string {raw!r} was retargeted ({target!r} -> {CATEGORY_ALIASES.get(raw)!r}) after "
                "migration 189 re-ran it — ship a follow-up data migration for the stranded rows"
            )
            assert frozen_093.get(raw) == target, (
                f"re-aliased string {raw!r}: 189's frozen target {target!r} does not match 093's frozen "
                f"target {frozen_093.get(raw)!r} — pass (b) would strand the rows 093 already rewrote"
            )

    def test_canonical_keys_snapshot_is_still_canonical(self):
        tree_keys = set(get_all_commodities())
        stale = set(_mod._CANONICAL_KEYS) - tree_keys
        assert not stale, (
            f"COMMODITY_TREE keys {sorted(stale)!r} were renamed/removed after migration 189 froze them — "
            "fresh-chain replays of pass (c) would rewrite rows onto now-off-vocab values; ship a follow-up "
            "data migration for the renamed keys"
        )

    def test_alias_targets_are_canonical_keys(self):
        tree_keys = set(get_all_commodities())
        for raw, target in {**_mod._NEW_ALIASES, **_mod._REALIASED}.items():
            assert target in tree_keys, f"frozen alias {raw!r} -> {target!r} is not a COMMODITY_TREE key"


class TestExecution:
    """Upgrade → downgrade → upgrade on a scratch SQLite engine, all three passes."""

    def _scratch_engine(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine, tables=[Base.metadata.tables[MaterialCard.__tablename__]])
        with engine.begin() as conn:
            for mpn, cat in [
                # Pass (a): new 2026-07 residue aliases (incl. &-, /- and padding cases).
                ("newa1", "Schottky Diodes & Rectifiers"),
                ("newa2", "  8-Bit Microcontrollers - MCU  "),
                ("newa3", "MOSFET"),
                ("newa4", "Server Maintenance Consumable / Thermal Management Accessory"),
                # Pass (b): already-aliased strings that leaked back post-093/100.
                ("leak1", "Connectors, Interconnects"),
                ("leak2", "Solid State Drives - SSD"),
                ("leak3", "eMMC"),
                # Pass (c): case/padding variants of already-canonical keys.
                ("case1", "Capacitors"),
                ("case2", "  Diodes  "),
                ("ok1", "hdd"),  # already canonical — untouched
                ("none1", None),  # NULL — untouched
                ("amb1", "EEPROM"),  # ambiguous residue — deliberately unmapped, untouched
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
                    "VALUES ('del1', 'del1', 'Zener Diodes', '2026-07-01 00:00:00')"
                )
            )
            # Provenance-bearing row: the documented contract is that the category
            # provenance columns stay untouched (spelling canonicalized, source unchanged).
            conn.execute(
                text(
                    "INSERT INTO material_cards (normalized_mpn, display_mpn, category, category_source, "
                    "category_confidence, category_tier, category_updated_at) "
                    "VALUES ('prov1', 'prov1', 'Voltage Regulator', 'digikey_api', 0.9, 90, '2026-07-01 00:00:00')"
                )
            )
        return engine

    @staticmethod
    def _run(engine, fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    @staticmethod
    def _categories(engine):
        with engine.connect() as conn:
            return dict(conn.execute(text("SELECT normalized_mpn, category FROM material_cards")).fetchall())

    @staticmethod
    def _provenance(engine):
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT category_source, category_confidence, category_tier, category_updated_at "
                    "FROM material_cards WHERE normalized_mpn = 'prov1'"
                )
            ).one()

    def test_upgrade_downgrade_upgrade_runs_all_three_passes(self):
        engine = self._scratch_engine()

        expected = {
            "newa1": "diodes",
            "newa2": "microcontrollers",
            "newa3": "mosfets",
            "newa4": "tools_accessories",
            "leak1": "connectors",
            "leak2": "ssd",
            "leak3": "flash",
            "case1": "capacitors",
            "case2": "diodes",
            "ok1": "hdd",
            "none1": None,
            "amb1": "EEPROM",
            "unk1": "Totally Unknown Category",
            "del1": "diodes",  # soft-deleted — normalized too
            "prov1": "voltage_regulators",
        }
        expected_provenance = ("digikey_api", 0.9, 90, "2026-07-01 00:00:00")

        self._run(engine, _mod.upgrade)
        assert self._categories(engine) == expected
        assert self._provenance(engine) == expected_provenance

        # Downgrade is a documented no-op (many-to-one normalization is irreversible).
        self._run(engine, _mod.downgrade)
        assert self._categories(engine) == expected

        # Re-upgrade is idempotent.
        self._run(engine, _mod.upgrade)
        assert self._categories(engine) == expected
        assert self._provenance(engine) == expected_provenance
