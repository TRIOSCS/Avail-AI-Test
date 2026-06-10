"""Normalize legacy material_cards.category strings onto canonical COMMODITY_TREE keys.

What: One-off DATA-ONLY migration with two steps.
      (1) Rewrites legacy ``material_cards.category`` values through a FROZEN snapshot
          of the category alias map (case-insensitive on ``LOWER(TRIM(category))``), so
          rows like "Integrated Circuits (ICs)" or TRIO SFDC codes like "Hard Drive"
          land on canonical tree keys ("ics_other", "hdd"). A second pass lowercases
          capitalized variants of already-canonical keys ("Capacitors" -> "capacitors").
          Soft-deleted rows are normalized as well — restoring a card must yield a
          canonical category.
      (2) Deletes the retired ``(connectors, series)`` row from
          ``commodity_spec_schemas``: the 2026-06-09 taxonomy expansion replaced it with ``rows`` —
          ``series`` was an open vocabulary with no canonical list, so it could only
          render as a noisy high-cardinality typeahead — and the boot seeder is
          insert-only/reconcile-only so removals can never reach an existing DB without
          a migration. Existing ``material_spec_facets`` rows with spec_key='series' are
          intentionally LEFT in place (real extracted data; without a schema row the UI
          simply stops rendering the facet — same precedent as migration 091).

Downgrade: step (1) is a documented NO-OP — normalization is many-to-one, the original
variant strings are unrecoverable. Step (2) IS deterministically reversible, so the
series schema row is re-inserted exactly as originally seeded.

Called by: alembic (upgrade/downgrade).
Depends on: material_cards, commodity_spec_schemas tables;
            app/services/category_normalizer.py (snapshot source, NOT imported — the
            map below is frozen so this migration's behaviour never drifts as the
            runtime alias map evolves).

Revision ID: 093_normalize_legacy_categories
Revises: 091_cleanup_vague_descs
Create Date: 2026-06-09

NOTE: 092 is intentionally skipped — that number is reserved by a concurrent branch.
"""

from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "093_normalize_legacy_categories"
down_revision = "091_cleanup_vague_descs"
branch_labels = None
depends_on = None

# FROZEN snapshot of the alias vocabulary at the time this migration was written:
# app/services/category_normalizer.py::CATEGORY_ALIASES PLUS the source-scoped
# TRIO_SFDC_COMMODITY_CODES ("memory" -> "dram" — safe in this one-off pass because
# every existing material_cards row carries TRIO part-master provenance; the forward
# hooks keep that entry out of the global path). Keys are lower/trimmed variants,
# values are canonical tree keys.
_CATEGORY_ALIASES: dict[str, str] = {
    "connectors, interconnects": "connectors",
    "cable assemblies": "cables",
    "cables, wires": "cables",
    "cables, wires - management": "cables",
    "battery products": "batteries",
    "laptop battery / notebook primary battery": "batteries",
    "laptop battery / power": "batteries",
    "microprocessors - mpu": "microprocessors",
    "arm microcontrollers - mcu": "microcontrollers",
    "solid state drives - ssd": "ssd",
    "office & computer & networking products > computer products > drives > disk drives": "hdd",
    "emmc": "flash",
    "memory - modules, cards": "dram",
    "linear voltage regulators": "voltage_regulators",
    "switching voltage regulators": "voltage_regulators",
    "inductors, coils, chokes": "inductors",
    "crystals, oscillators, resonators": "oscillators",
    "counter shift registers": "logic_ic",
    "bipolar transistors - bjt": "transistors",
    "sensors, transducers": "sensors",
    "networking solutions": "networking",
    "fans, blowers, thermal management": "fans_cooling",
    "system board / motherboard": "motherboards",
    "laptop motherboard / system board": "motherboards",
    "system board / motherboard (laptop spare part)": "motherboards",
    "cpu - central processing units": "cpu",
    "tools": "tools_accessories",
    "soldering, desoldering, rework products": "tools_accessories",
    "main board": "motherboards",
    "hard drive": "hdd",
    "memory": "dram",
    "lcd": "displays",
    "lcd assy": "displays",
    "psu": "power_supplies",
    "graphics card": "gpu",
    "tape drive": "tape_drives",
    "ic": "ics_other",
    "oem assy": "oem_assemblies",
    "integrated circuits (ics)": "ics_other",
}

# FROZEN snapshot of the canonical COMMODITY_TREE sub-category keys at the time this
# migration was written. Used to lowercase capitalized variants of canonical values
# (e.g. "Capacitors" -> "capacitors").
_CANONICAL_KEYS: tuple[str, ...] = (
    "capacitors", "resistors", "inductors", "transformers", "fuses", "oscillators", "filters",
    "diodes", "transistors", "mosfets", "thyristors",
    "analog_ic", "logic_ic", "power_ic", "ics_other",
    "dram", "flash",
    "microcontrollers", "cpu", "microprocessors", "dsp", "fpga", "asic", "gpu",
    "ssd", "hdd", "tape_drives",
    "power_supplies", "voltage_regulators", "batteries",
    "connectors", "cables", "sockets",
    "relays", "switches", "motors",
    "leds", "displays", "optoelectronics",
    "sensors", "rf",
    "motherboards", "network_cards", "raid_controllers", "server_chassis",
    "fans_cooling", "networking", "oem_assemblies",
    "enclosures", "tools_accessories", "other",
)  # fmt: skip


def upgrade() -> None:
    conn = op.get_bind()

    # (1a) Alias variants -> canonical keys (case-insensitive, trimmed).
    normalized = 0
    for raw, target in sorted(_CATEGORY_ALIASES.items()):
        result = conn.execute(
            text(
                "UPDATE material_cards SET category = :target "
                "WHERE category IS NOT NULL AND LOWER(TRIM(category)) = :raw AND category != :target"
            ),
            {"target": target, "raw": raw},
        )
        normalized += result.rowcount or 0

    # (1b) Capitalized/padded variants of already-canonical keys -> lowercase key.
    lowercased = 0
    for key in _CANONICAL_KEYS:
        result = conn.execute(
            text(
                "UPDATE material_cards SET category = :key "
                "WHERE category IS NOT NULL AND LOWER(TRIM(category)) = :key AND category != :key"
            ),
            {"key": key},
        )
        lowercased += result.rowcount or 0

    logger.info(
        "093: normalized {} aliased + {} case-variant material_cards.category values",
        normalized,
        lowercased,
    )

    # (2) Retire the open-vocab connectors/series spec (replaced by 'rows' in the seeds;
    # the insert-only seeder can never remove it from an existing DB).
    result = conn.execute(
        text("DELETE FROM commodity_spec_schemas WHERE commodity = 'connectors' AND spec_key = 'series'")
    )
    logger.info("093: deleted {} retired connectors/series schema row(s)", result.rowcount or 0)


def downgrade() -> None:
    # Step (1) is intentionally a NO-OP: category normalization is many-to-one
    # ("Hard Drive", "hard drive  ", and "HARD DRIVE" all become "hdd"), so the original
    # variant strings are unrecoverable by design. Canonical keys remain valid categories
    # on older code, so leaving them in place is safe.
    #
    # Step (2) is deterministically reversible — restore the connectors/series schema row
    # exactly as originally seeded (enum with no enum_values = open vocabulary).
    conn = op.get_bind()
    conn.execute(
        text(
            "INSERT INTO commodity_spec_schemas "
            "(commodity, spec_key, display_name, data_type, sort_order, is_filterable, is_primary) "
            "SELECT 'connectors', 'series', 'Series', 'enum', 6, true, false "
            "WHERE NOT EXISTS (SELECT 1 FROM commodity_spec_schemas "
            "WHERE commodity = 'connectors' AND spec_key = 'series')"
        )
    )
    logger.info("093 downgrade: restored connectors/series schema row (category normalization is not reversible)")
