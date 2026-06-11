"""Backfill legacy material_cards.category rows for the post-093 distributor aliases.

What: One-off DATA-ONLY migration. The 2026-06-10 ladder-monopoly hardening added four
      distributor/OEM taxonomy aliases to the runtime category alias map
      (app/services/category_normalizer.py::CATEGORY_ALIASES):
      "hard drives" / "internal hard drives" -> hdd and
      "memory module" / "memory modules" -> dram.
      The runtime map is only a FORWARD hook at write time, and migration 093's one-off
      normalization ran from a frozen snapshot that predates these entries — so any
      pre-existing row holding one of these variant strings would stay invisible to
      every commodity filter forever. This migration rewrites those rows
      (case-insensitive on LOWER(TRIM(category)), soft-deleted rows included — restoring
      a card must yield a canonical category), exactly mirroring 093's step (1a).
      Category provenance columns are left untouched: the value's SOURCE did not change,
      only its spelling was canonicalized (same contract as 093 + 096's backfill order).
      No facet purge is needed — off-vocab categories never had a spec schema, so no
      MaterialSpecFacet rows exist under these variant strings.

Downgrade: documented NO-OP — normalization is many-to-one, the original variant
strings are unrecoverable (same contract as 093).

Called by: alembic (upgrade/downgrade).
Depends on: material_cards table; registered in
            tests/test_category_normalizer.py::POST_093_ALIASES (the alias↔backfill
            sync gate).

Revision ID: 100_taxonomy_alias_backfill
Revises: 099_on_add_enrich
Create Date: 2026-06-10
"""

from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "100_taxonomy_alias_backfill"
down_revision = "099_on_add_enrich"
branch_labels = None
depends_on = None

# FROZEN snapshot of the aliases this migration backfills (the post-093 additions ONLY —
# never re-import the live map, this migration's behaviour must not drift as it evolves).
_NEW_ALIASES: dict[str, str] = {
    "hard drives": "hdd",
    "internal hard drives": "hdd",
    "memory module": "dram",
    "memory modules": "dram",
}


def upgrade() -> None:
    conn = op.get_bind()
    normalized = 0
    for raw, target in sorted(_NEW_ALIASES.items()):
        result = conn.execute(
            text(
                "UPDATE material_cards SET category = :target "
                "WHERE category IS NOT NULL AND LOWER(TRIM(category)) = :raw AND category != :target"
            ),
            {"target": target, "raw": raw},
        )
        normalized += result.rowcount or 0
    logger.info("100: normalized {} material_cards.category values onto the new distributor aliases", normalized)


def downgrade() -> None:
    # Intentionally a NO-OP: normalization is many-to-one — the original variant strings
    # ("Hard Drives" vs "Internal Hard Drives") are unrecoverable after the rewrite.
    logger.info("100: downgrade is a documented no-op (many-to-one normalization is irreversible)")
