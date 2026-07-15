"""Backfill the 2026-07 material_cards.category residue onto canonical tree keys.

What: One-off DATA-ONLY migration with three passes, recovering the cards the startup
      residue check (app/startup.py::_warn_non_canonical_categories) reported stranded
      in non-canonical categories (139 cards / ~90 category strings on live staging,
      2026-07-15) — invisible to every commodity filter because the faceted sidebar
      matches ``lower(trim(category))`` against canonical COMMODITY_TREE keys only.

      (a) 64 NEW aliases added to the runtime map in the same PR
          (app/services/category_normalizer.py::CATEGORY_ALIASES, "2026-07 residue
          remap" block). The runtime map is only a FORWARD hook at write time, so the
          pre-existing rows need this one-off rewrite — exactly mirroring 093 step (1a)
          / migration 100.
      (b) Re-run of 8 aliases that ALREADY existed in 093/100's snapshots but leaked
          back onto live rows after those backfills ran (writers that bypassed the
          forward hook before the F1 ladder closed that hole on 2026-06-10). Targets
          are frozen from the CURRENT runtime map — each matches 093's frozen target,
          so no retarget drift.
      (c) Lowercase pass for capitalized/padded variants of already-canonical keys
          ("Capacitors" -> "capacitors"), exactly mirroring 093 step (1b).

      All passes rewrite case-insensitively on ``LOWER(TRIM(category))``, include
      soft-deleted rows (restoring a card must yield a canonical category), and leave
      the category provenance columns (category_source/confidence/tier/updated_at)
      untouched: the value's SOURCE did not change, only its spelling was canonicalized
      (same contract as 093/100). No facet purge is needed — off-vocab categories never
      had a spec schema, so no MaterialSpecFacet rows exist under these variant strings.

      The 17 residue strings with NO unambiguous canonical bucket (bare manufacturer
      names like "intel"/"nexperia", "circuit protection", "eeprom", "isolators",
      "varistors", ...) are intentionally NOT touched — they need product mapping calls,
      tracked in docs/superpowers/2026-07-03-master-requested-work-backlog.md.

Downgrade: documented NO-OP — normalization is many-to-one, the original variant
strings are unrecoverable (same contract as 093/100).

Called by: alembic (upgrade/downgrade).
Depends on: material_cards table; registered in
            tests/test_category_normalizer.py::POST_093_ALIASES (the alias↔backfill
            sync gate).

Revision ID: 189_category_residue_backfill
Revises: 188_canonical_offers_excess_fk
Create Date: 2026-07-15
"""

from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "189_category_residue_backfill"
down_revision = "188_canonical_offers_excess_fk"
branch_labels = None
depends_on = None

# FROZEN snapshot of the aliases this migration backfills (the 2026-07 residue-remap
# additions ONLY — never re-import the live map, this migration's behaviour must not
# drift as it evolves).
_NEW_ALIASES: dict[str, str] = {
    "power inductors - smd": "inductors",
    "common mode chokes / filters": "inductors",
    "aluminum electrolytic capacitors - radial leaded": "capacitors",
    "multilayer ceramic capacitors mlcc - smd/smt": "capacitors",
    "aluminum organic polymer capacitors": "capacitors",
    "current sense resistors - smd": "resistors",
    "trimmer resistors - through hole": "resistors",
    "crystals": "oscillators",
    "mems oscillators": "oscillators",
    "standard clock oscillators": "oscillators",
    "tcxo oscillators": "oscillators",
    "igbt modules": "transistors",
    "schottky diodes & rectifiers": "diodes",
    "esd protection diodes / tvs diodes": "diodes",
    "zener diodes": "diodes",
    "rectifiers": "diodes",
    "diode": "diodes",
    "mosfet": "mosfets",
    "power switch ics - power distribution": "power_ic",
    "switching controllers": "power_ic",
    "supervisory circuits": "power_ic",
    "motor / motion / ignition controllers & drivers": "power_ic",
    "audio amplifiers": "analog_ic",
    "precision amplifiers": "analog_ic",
    "analog to digital converters - adc": "analog_ic",
    "data converter (adc)": "analog_ic",
    "digital to analog converters - dac": "analog_ic",
    "logic ic": "logic_ic",
    "clock buffer": "ics_other",
    "rs-232 interface ic": "ics_other",
    "rs-422/rs-485 interface ic": "ics_other",
    "pci interface ic": "ics_other",
    "interface ic": "ics_other",
    "lin transceivers": "ics_other",
    "integrated circuit (timer)": "ics_other",
    "timers & support products": "ics_other",
    "8-bit microcontrollers - mcu": "microcontrollers",
    "microcontroller": "microcontrollers",
    "digital signal processors & controllers - dsp, dsc": "dsp",
    "fpga - field programmable gate array": "fpga",
    "hard disk drives - hdd": "hdd",
    "ldo voltage regulators": "voltage_regulators",
    "voltage regulator": "voltage_regulators",
    "power supplies - board mount": "power_supplies",
    "electronic battery": "batteries",
    "laptop battery": "batteries",
    "laptop battery (fru / cru replacement part)": "batteries",
    "storage controller battery": "batteries",
    "raid controller accessory / battery backup (bbwc battery module)": "batteries",
    "raid controller accessory / battery module": "batteries",
    "automotive connectors": "connectors",
    "board to board & mezzanine connectors": "connectors",
    "circular metric connectors": "connectors",
    "terminals": "connectors",
    "conduit fittings & accessories": "cables",
    "reed relays": "relays",
    "board mount current sensors": "sensors",
    "imus - inertial measurement units": "sensors",
    "bluetooth modules - 802.15.1": "rf",
    "multiprotocol modules": "rf",
    "rf transceiver": "rf",
    "rf/wireless module": "rf",
    "development boards, kits, programmers": "tools_accessories",
    "server maintenance consumable / thermal management accessory": "tools_accessories",
}

# FROZEN snapshot of the 8 aliases that already existed in 093/100's snapshots but
# whose variant strings leaked back onto live rows after those backfills ran (writers
# that bypassed the forward hook until the F1 ladder closed the hole on 2026-06-10).
# Targets read from the CURRENT runtime CATEGORY_ALIASES — each matches 093's frozen
# target, so this pass strands nothing.
_REALIASED: dict[str, str] = {
    "connectors, interconnects": "connectors",
    "switching voltage regulators": "voltage_regulators",
    "arm microcontrollers - mcu": "microcontrollers",
    "cpu - central processing units": "cpu",
    "emmc": "flash",
    "integrated circuits (ics)": "ics_other",
    "battery products": "batteries",
    "solid state drives - ssd": "ssd",
}

# FROZEN snapshot of the canonical COMMODITY_TREE sub-category keys at the time this
# migration was written. Used to lowercase capitalized variants of canonical values
# (e.g. "Capacitors" -> "capacitors"), mirroring 093 step (1b).
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

    # (a) NEW 2026-07 residue aliases -> canonical keys (case-insensitive, trimmed;
    # soft-deleted rows included; provenance columns untouched).
    new_normalized = 0
    for raw, target in sorted(_NEW_ALIASES.items()):
        result = conn.execute(
            text(
                "UPDATE material_cards SET category = :target "
                "WHERE category IS NOT NULL AND LOWER(TRIM(category)) = :raw AND category != :target"
            ),
            {"target": target, "raw": raw},
        )
        new_normalized += result.rowcount or 0

    # (b) Re-run of the 8 already-aliased strings that leaked back post-093/100.
    releaked = 0
    for raw, target in sorted(_REALIASED.items()):
        result = conn.execute(
            text(
                "UPDATE material_cards SET category = :target "
                "WHERE category IS NOT NULL AND LOWER(TRIM(category)) = :raw AND category != :target"
            ),
            {"target": target, "raw": raw},
        )
        releaked += result.rowcount or 0

    # (c) Capitalized/padded variants of already-canonical keys -> lowercase key.
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
        "189: normalized {} new-alias + {} leaked-back-alias + {} case-variant material_cards.category values",
        new_normalized,
        releaked,
        lowercased,
    )


def downgrade() -> None:
    # Intentionally a NO-OP: normalization is many-to-one ("Diode", "Zener Diodes" and
    # "Rectifiers" all become "diodes"), so the original variant strings are
    # unrecoverable by design (same contract as 093/100). Canonical keys remain valid
    # categories on older code, so leaving them in place is safe.
    logger.info("189: downgrade is a documented no-op (many-to-one normalization is irreversible)")
