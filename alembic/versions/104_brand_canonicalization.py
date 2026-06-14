"""Brand canonicalization — restructure the HPE family + Texas Instruments aliases.

What: One-off DATA-ONLY migration on the ``manufacturers`` lookup table (the
      normalize_brand_name vocabulary). The live brand facet wasted 7 of its top-20
      slots on duplicates: the HPE family split four ways (Hewlett Packard Enterprise
      4,046 / HP 3,834 / HPE 206 / HEWLETT PACKARD 148 — selecting "HP" silently missed
      the 4,400 HPE-labeled cards) and "Texas Instruments (TI)" (63) duplicated
      "Texas Instruments" (83). This migration:

      1. Renames the canonical "Hewlett Packard Enterprise" row to "HPE" and merges its
         alias list to ["Hewlett Packard Enterprise", "HP", "Hewlett Packard",
         "Hewlett-Packard"] — matching the updated startup seed
         (app/startup.py::_seed_manufacturers). Defensive against seed/migration
         ordering: if a separate "HPE" row already exists (a fresh-seeded process raced
         this migration), the old long-name row is DELETED instead and the "HPE" row's
         aliases are reasserted.
      2. Adds the "Texas Instruments (TI)" alias to the "Texas Instruments" row.

      The Dell family (Dell Technologies / DELL / Dell) needs no table change — the
      existing "Dell Technologies" row's "Dell" alias already folds both case variants
      (the lookup is case-insensitive); the live duplicates are unprovenanced values
      written before writers routed through set_manufacturer, and are folded by the
      one-shot CLI ``app/management/normalize_manufacturers.py`` (run separately —
      card-value rewrites are an operator action with a dry-run gate, not a migration).

Downgrade: restores the previous canonical name + alias lists exactly.

Called by: alembic (upgrade/downgrade).
Depends on: manufacturers table (canonical_name unique, aliases JSON).

Revision ID: 104_brand_canonicalization
Revises: 103_unavail_policy_columns
Create Date: 2026-06-12
"""

import json

from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "104_brand_canonicalization"
down_revision = "103_unavail_policy_columns"
branch_labels = None
depends_on = None

_OLD_HPE_CANONICAL = "Hewlett Packard Enterprise"
_NEW_HPE_CANONICAL = "HPE"
_NEW_HPE_ALIASES = ["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]
_OLD_HPE_ALIASES = ["HPE", "HP"]

_TI_CANONICAL = "Texas Instruments"
_NEW_TI_ALIASES = ["TI", "Texas Inst", "Texas Instruments (TI)"]
_OLD_TI_ALIASES = ["TI", "Texas Inst"]


def _exists(conn, canonical: str) -> bool:
    return bool(conn.execute(text("SELECT 1 FROM manufacturers WHERE canonical_name = :c"), {"c": canonical}).first())


def upgrade() -> None:
    conn = op.get_bind()

    if _exists(conn, _NEW_HPE_CANONICAL):
        # A fresh-seeded "HPE" row already exists — drop the legacy long-name row (if
        # any) and reassert the merged alias list on the survivor.
        conn.execute(text("DELETE FROM manufacturers WHERE canonical_name = :old"), {"old": _OLD_HPE_CANONICAL})
        conn.execute(
            text("UPDATE manufacturers SET aliases = :aliases WHERE canonical_name = :new"),
            {"aliases": json.dumps(_NEW_HPE_ALIASES), "new": _NEW_HPE_CANONICAL},
        )
        logger.info("104: 'HPE' row already present — removed legacy '{}' row, reasserted aliases", _OLD_HPE_CANONICAL)
    else:
        result = conn.execute(
            text("UPDATE manufacturers SET canonical_name = :new, aliases = :aliases WHERE canonical_name = :old"),
            {"new": _NEW_HPE_CANONICAL, "aliases": json.dumps(_NEW_HPE_ALIASES), "old": _OLD_HPE_CANONICAL},
        )
        logger.info("104: renamed '{}' -> 'HPE' ({} row)", _OLD_HPE_CANONICAL, result.rowcount or 0)

    conn.execute(
        text("UPDATE manufacturers SET aliases = :aliases WHERE canonical_name = :c"),
        {"aliases": json.dumps(_NEW_TI_ALIASES), "c": _TI_CANONICAL},
    )
    logger.info("104: 'Texas Instruments' aliases now include 'Texas Instruments (TI)'")


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("UPDATE manufacturers SET canonical_name = :old, aliases = :aliases WHERE canonical_name = :new"),
        {"old": _OLD_HPE_CANONICAL, "aliases": json.dumps(_OLD_HPE_ALIASES), "new": _NEW_HPE_CANONICAL},
    )
    conn.execute(
        text("UPDATE manufacturers SET aliases = :aliases WHERE canonical_name = :c"),
        {"aliases": json.dumps(_OLD_TI_ALIASES), "c": _TI_CANONICAL},
    )
    logger.info("104: restored '{}' canonical + previous alias lists", _OLD_HPE_CANONICAL)
