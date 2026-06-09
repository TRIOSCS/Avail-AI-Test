"""SP2 provenance & tier ladder — add facet + category provenance columns and backfill.

What: Adds the F2 provenance columns so every spec/category write can record where it came
      from and how it ranks on the F1 tier ladder:
        - material_spec_facets: source (String(50)), confidence (Float), tier (Integer)
        - material_cards: category_source (String(50)), category_confidence (Float),
          category_tier (Integer)
      All six are nullable (legacy rows pre-date provenance). On PostgreSQL it then backfills:
        (a) facet source/confidence copied from the matching material_cards.specs_structured
            JSONB entry, with tier computed via a CASE over a LITERAL copy of the F1 SOURCE_TIER
            map (migrations must not import app code that may drift); and
        (b) category provenance for rows with a non-NULL category but NULL category_source set
            to a deliberate mid-tier (legacy_backfill / 0.5 / 50) so a real future source
            (decode 85, vendor 90) overrides it but a stray AI guess (40) cannot silently flip it.
      The JSONB backfill is GUARDED to PostgreSQL only — on SQLite the add_column runs but the
      JSONB extract is skipped, so the schema migration stays testable on the in-memory engine
      (SQLite masks PG JSON ops; verify the backfill against live PG per feedback_sqlite_masks_postgres).
Called by: alembic (upgrade/downgrade).
Depends on: 091_cleanup_vague_descs (SP1, current single head); material_cards,
      material_spec_facets tables.

Revision ID: 092_spec_provenance
Revises: 091_cleanup_vague_descs
Create Date: 2026-06-09
"""

import sqlalchemy as sa
from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "092_spec_provenance"
down_revision = "091_cleanup_vague_descs"
branch_labels = None
depends_on = None

# Mid-tier marker for category rows that have a value but no provenance (see module docstring).
_LEGACY_CATEGORY_SOURCE = "legacy_backfill"
_LEGACY_CATEGORY_CONFIDENCE = 0.5
_LEGACY_CATEGORY_TIER = 50

# LITERAL copy of app.services.spec_tiers.SOURCE_TIER. Migrations must NOT import app code
# (it may drift); this snapshot is what the facet-tier backfill ranks against. Unknown
# sources fall through the CASE to tier 0.
_SOURCE_TIER_SQL_CASE = (
    "CASE c.specs_structured -> f.spec_key ->> 'source' "
    "WHEN 'manual' THEN 100 "
    "WHEN 'digikey_api' THEN 90 "
    "WHEN 'mouser_api' THEN 90 "
    "WHEN 'nexar_api' THEN 90 "
    "WHEN 'element14_api' THEN 90 "
    "WHEN 'oemsecrets_api' THEN 90 "
    "WHEN 'mpn_decode' THEN 85 "
    "WHEN 'partsurfer' THEN 80 "
    "WHEN 'psref' THEN 80 "
    "WHEN 'web_search' THEN 70 "
    "WHEN 'brokerbin' THEN 65 "
    "WHEN 'spec_extraction' THEN 60 "
    "WHEN 'ai_guess' THEN 40 "
    "WHEN 'claude_opus_inferred' THEN 40 "
    "ELSE 0 END"
)


def upgrade() -> None:
    # --- Schema: 6 nullable provenance columns ---
    op.add_column("material_spec_facets", sa.Column("source", sa.String(length=50), nullable=True))
    op.add_column("material_spec_facets", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column("material_spec_facets", sa.Column("tier", sa.Integer(), nullable=True))

    op.add_column("material_cards", sa.Column("category_source", sa.String(length=50), nullable=True))
    op.add_column("material_cards", sa.Column("category_confidence", sa.Float(), nullable=True))
    op.add_column("material_cards", sa.Column("category_tier", sa.Integer(), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (test engine) has no JSONB operators — schema columns are added above so the
        # migration round-trips, but the data backfill is PG-only.
        logger.info("092_spec_provenance: non-PostgreSQL dialect — skipping JSONB/category backfill")
        return

    # --- Backfill (a): facet provenance from the winning specs_structured JSONB entry ---
    result = bind.execute(
        text(
            "UPDATE material_spec_facets f "
            "SET source = c.specs_structured -> f.spec_key ->> 'source', "
            "    confidence = (c.specs_structured -> f.spec_key ->> 'confidence')::float, "
            f"    tier = {_SOURCE_TIER_SQL_CASE} "
            "FROM material_cards c "
            "WHERE c.id = f.material_card_id "
            "  AND c.specs_structured ? f.spec_key "
            "  AND f.source IS NULL"
        )
    )
    logger.info("092_spec_provenance: backfilled provenance on {} facet rows", result.rowcount)

    # --- Backfill (b): category provenance for valued-but-unprovenanced rows ---
    result = bind.execute(
        text(
            "UPDATE material_cards "
            "SET category_source = :src, category_confidence = :conf, category_tier = :tier "
            "WHERE category IS NOT NULL AND category_source IS NULL"
        ),
        {
            "src": _LEGACY_CATEGORY_SOURCE,
            "conf": _LEGACY_CATEGORY_CONFIDENCE,
            "tier": _LEGACY_CATEGORY_TIER,
        },
    )
    logger.info("092_spec_provenance: backfilled category provenance on {} card rows", result.rowcount)


def downgrade() -> None:
    # Additive columns — no data restore needed. Drop in reverse order.
    op.drop_column("material_cards", "category_tier")
    op.drop_column("material_cards", "category_confidence")
    op.drop_column("material_cards", "category_source")

    op.drop_column("material_spec_facets", "tier")
    op.drop_column("material_spec_facets", "confidence")
    op.drop_column("material_spec_facets", "source")
