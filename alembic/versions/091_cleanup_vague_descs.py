"""SP1 data cleanup — null out hallucinated descriptions and untrustworthy spec stamps.

What: One-off DATA-ONLY backfill that undoes the damage from the now-removed automated
      Haiku enrichment path. (1) Nulls hallucinated free-text descriptions on never-sourced
      not_found cards (descriptions containing hedging tokens like "likely"/"possibly"),
      and (2) clears specs_enriched_at on any card whose status is NOT trustworthy
      (verified/web_sourced/oem_sourced) so the status-gated spec reader re-evaluates it
      cleanly. Both affected sets are snapshotted into _sp1_desc_backup so downgrade is exact.
Called by: alembic (upgrade/downgrade).
Depends on: material_cards table; SP1 reader status gate in spec_enrichment_service.py.

KNOWN ORDERING DEPENDENCY (intentional, NOT a silent hole): clearing
``specs_enriched_at`` on the untrustworthy cards does NOT delete the stale
``material_spec_facets`` rows those cards previously produced. After the stamp is
cleared the status gate prevents those cards from being re-processed, so the orphaned
facets are intentionally left in place for SP2 (provenance rework). SP2 adds
``source``/``tier`` columns to MaterialSpecFacet, which lets it distinguish
guess-derived ``spec_extraction`` facets from deterministic ``mpn_decode`` ones and
purge/re-rank them accordingly. We leave them here rather than blind-delete because this
migration cannot tell which facets came from a guess vs. a deterministic decode.

Revision ID: 091_cleanup_vague_descs
Revises: 090_add_condition_mc
Create Date: 2026-06-09
"""

from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "091_cleanup_vague_descs"
down_revision = "090_add_condition_mc"
branch_labels = None
depends_on = None

# Hedging tokens that betray a hallucinated/guessed description. A never-sourced
# (enrichment_source IS NULL) not_found card carrying any of these is junk, not data.
_VAGUE_DESC_WHERE = (
    "deleted_at IS NULL "
    "AND enrichment_status = 'not_found' "
    "AND enrichment_source IS NULL "
    "AND description IS NOT NULL "
    "AND (description ILIKE '%likely%' "
    "OR description ILIKE '%possibly%' "
    "OR description ILIKE '%may be%' "
    "OR description ILIKE '%proprietary%' "
    "OR description ILIKE '%appears to be%' "
    "OR description ILIKE '%could be%')"
)

# Any card stamped as spec-enriched whose status is NOT trustworthy was seeded from a
# guess/orphan description before the reader gate existed — clear the stamp so it re-evaluates.
_UNTRUSTWORTHY_STAMP_WHERE = (
    "deleted_at IS NULL "
    "AND specs_enriched_at IS NOT NULL "
    "AND enrichment_status NOT IN ('verified', 'web_sourced', 'oem_sourced')"
)


def upgrade() -> None:
    conn = op.get_bind()

    # (a) Snapshot every row this migration will touch (either branch) so downgrade is exact.
    conn.execute(text("DROP TABLE IF EXISTS _sp1_desc_backup"))
    conn.execute(
        text(
            "CREATE TABLE _sp1_desc_backup AS "
            "SELECT id, description, specs_enriched_at FROM material_cards "
            f"WHERE ({_VAGUE_DESC_WHERE}) OR ({_UNTRUSTWORTHY_STAMP_WHERE})"
        )
    )

    # (b) Null hallucinated descriptions on never-sourced not_found cards.
    result = conn.execute(text(f"UPDATE material_cards SET description = NULL WHERE {_VAGUE_DESC_WHERE}"))
    logger.info("SP1 cleanup: nulled {} hallucinated not_found descriptions", result.rowcount)

    # (c) Clear spec stamps that were set from untrustworthy descriptions.
    result = conn.execute(
        text(f"UPDATE material_cards SET specs_enriched_at = NULL WHERE {_UNTRUSTWORTHY_STAMP_WHERE}")
    )
    logger.info("SP1 cleanup: cleared {} untrustworthy specs_enriched_at stamps", result.rowcount)


def downgrade() -> None:
    conn = op.get_bind()
    # There is NO existence guard here by design: this UPDATE ... FROM _sp1_desc_backup
    # references the snapshot table directly, so if the snapshot is missing the statement
    # ERRORS LOUD. That is the safe behaviour — without the snapshot we cannot restore the
    # original descriptions/stamps, and silently swallowing the missing-backup error would
    # be a quiet data-loss path (downgrade reporting success while restoring nothing). We
    # deliberately do NOT suppress it.
    conn.execute(
        text(
            "UPDATE material_cards m "
            "SET description = b.description, specs_enriched_at = b.specs_enriched_at "
            "FROM _sp1_desc_backup b WHERE m.id = b.id"
        )
    )
    conn.execute(text("DROP TABLE IF EXISTS _sp1_desc_backup"))
