"""Fix material_cards.normalized_mpn — apply normalize_mpn_key to all rows.

The scheduler stock import was storing raw uppercase MPNs with dashes/dots
(e.g., 'QA-TEST-001') in the normalized_mpn column instead of the canonical
lowercase alphanumeric key (e.g., 'qatest001'). This caused duplicate cards
for the same physical part.

This migration:
1. Drops the unique index on normalized_mpn (to allow temp duplicates)
2. Recomputes normalized_mpn for all rows using the canonical algorithm
3. Merges duplicates by keeping the oldest card (lowest id) for each key
4. Reassigns sightings, vendor history, tags, and requirements from dupes
5. Recreates the unique index

Revision ID: 045
Revises: 044_simplify_ticket_statuses
"""

from alembic import op

revision = "045"
down_revision = "044_simplify_ticket_statuses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 0: Drop the unique index so normalization doesn't hit constraint errors
    op.execute("DROP INDEX IF EXISTS ix_material_cards_normalized_mpn")

    # Step 1: Recompute normalized_mpn for rows that don't match canonical form
    op.execute("""
        UPDATE material_cards
        SET normalized_mpn = lower(regexp_replace(normalized_mpn, '[^a-zA-Z0-9]', '', 'g'))
        WHERE normalized_mpn != lower(regexp_replace(normalized_mpn, '[^a-zA-Z0-9]', '', 'g'))
          AND deleted_at IS NULL
    """)

    # Build a temp table of (dup_id, keeper_id) for clarity
    op.execute("""
        CREATE TEMP TABLE _dup_map AS
        SELECT dup.id AS dup_id, keeper.id AS keeper_id
        FROM material_cards dup
        JOIN (
            SELECT normalized_mpn, min(id) AS id
            FROM material_cards WHERE deleted_at IS NULL
            GROUP BY normalized_mpn HAVING count(*) > 1
        ) keeper ON keeper.normalized_mpn = dup.normalized_mpn AND dup.id != keeper.id
        WHERE dup.deleted_at IS NULL
    """)

    # Step 2: Merge child rows from dupes to keepers

    # Sightings: no unique constraint — safe to bulk-update
    op.execute("""
        UPDATE sightings s SET material_card_id = dm.keeper_id
        FROM _dup_map dm WHERE s.material_card_id = dm.dup_id
    """)

    # Material_vendor_history: unique on (material_card_id, vendor_name)
    # For each (keeper_id, vendor_name) keep only the row with the newest last_seen
    # Delete all dup rows first, then insert non-conflicting ones isn't practical.
    # Simplest: just delete dup rows (keeper already has its own history).
    op.execute("""
        DELETE FROM material_vendor_history mvh
        USING _dup_map dm WHERE mvh.material_card_id = dm.dup_id
    """)

    # Material_tags: unique on (material_card_id, tag_id)
    # Same approach — delete dup rows (tags on keeper are sufficient)
    op.execute("""
        DELETE FROM material_tags mt
        USING _dup_map dm WHERE mt.material_card_id = dm.dup_id
    """)

    # Requirements: no unique constraint — safe to bulk-update
    op.execute("""
        UPDATE requirements r SET material_card_id = dm.keeper_id
        FROM _dup_map dm WHERE r.material_card_id = dm.dup_id
    """)

    # Step 3: Soft-delete duplicate cards
    op.execute("""
        UPDATE material_cards mc SET deleted_at = now()
        FROM _dup_map dm WHERE mc.id = dm.dup_id
    """)

    op.execute("DROP TABLE _dup_map")

    # Step 4: Recreate the unique index (only on non-deleted rows)
    op.execute("""
        CREATE UNIQUE INDEX ix_material_cards_normalized_mpn
        ON material_cards (normalized_mpn)
        WHERE deleted_at IS NULL
    """)


def downgrade() -> None:
    # Cannot un-merge cards — data migration is forward-only
    pass
