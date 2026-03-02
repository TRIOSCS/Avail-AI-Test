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
    # Canonical form = lowercase + strip all non-alphanumeric chars
    op.execute("""
        UPDATE material_cards
        SET normalized_mpn = lower(regexp_replace(normalized_mpn, '[^a-zA-Z0-9]', '', 'g'))
        WHERE normalized_mpn != lower(regexp_replace(normalized_mpn, '[^a-zA-Z0-9]', '', 'g'))
          AND deleted_at IS NULL
    """)

    # Step 2: Identify and merge duplicates (keep lowest id per normalized_mpn)
    # Reassign sightings from duplicate cards to keeper
    op.execute("""
        UPDATE sightings s
        SET material_card_id = keeper.id
        FROM material_cards dup
        JOIN (
            SELECT normalized_mpn, min(id) AS id
            FROM material_cards
            WHERE deleted_at IS NULL
            GROUP BY normalized_mpn
            HAVING count(*) > 1
        ) keeper ON keeper.normalized_mpn = dup.normalized_mpn AND dup.id != keeper.id
        WHERE s.material_card_id = dup.id
    """)

    # Reassign material_vendor_history
    op.execute("""
        UPDATE material_vendor_history mvh
        SET material_card_id = keeper.id
        FROM material_cards dup
        JOIN (
            SELECT normalized_mpn, min(id) AS id
            FROM material_cards
            WHERE deleted_at IS NULL
            GROUP BY normalized_mpn
            HAVING count(*) > 1
        ) keeper ON keeper.normalized_mpn = dup.normalized_mpn AND dup.id != keeper.id
        WHERE mvh.material_card_id = dup.id
    """)

    # Reassign material_tags (skip if already pointing to keeper to avoid unique violations)
    op.execute("""
        UPDATE material_tags mt
        SET material_card_id = keeper.id
        FROM material_cards dup
        JOIN (
            SELECT normalized_mpn, min(id) AS id
            FROM material_cards
            WHERE deleted_at IS NULL
            GROUP BY normalized_mpn
            HAVING count(*) > 1
        ) keeper ON keeper.normalized_mpn = dup.normalized_mpn AND dup.id != keeper.id
        WHERE mt.material_card_id = dup.id
          AND NOT EXISTS (
              SELECT 1 FROM material_tags mt2
              WHERE mt2.material_card_id = keeper.id AND mt2.tag_id = mt.tag_id
          )
    """)

    # Delete orphaned material_tags that already exist on keeper
    op.execute("""
        DELETE FROM material_tags mt
        USING material_cards dup
        JOIN (
            SELECT normalized_mpn, min(id) AS id
            FROM material_cards
            WHERE deleted_at IS NULL
            GROUP BY normalized_mpn
            HAVING count(*) > 1
        ) keeper ON keeper.normalized_mpn = dup.normalized_mpn AND dup.id != keeper.id
        WHERE mt.material_card_id = dup.id
    """)

    # Reassign requirements
    op.execute("""
        UPDATE requirements r
        SET material_card_id = keeper.id
        FROM material_cards dup
        JOIN (
            SELECT normalized_mpn, min(id) AS id
            FROM material_cards
            WHERE deleted_at IS NULL
            GROUP BY normalized_mpn
            HAVING count(*) > 1
        ) keeper ON keeper.normalized_mpn = dup.normalized_mpn AND dup.id != keeper.id
        WHERE r.material_card_id = dup.id
    """)

    # Step 3: Soft-delete duplicate cards (keep oldest)
    op.execute("""
        UPDATE material_cards dup
        SET deleted_at = now()
        FROM (
            SELECT normalized_mpn, min(id) AS keep_id
            FROM material_cards
            WHERE deleted_at IS NULL
            GROUP BY normalized_mpn
            HAVING count(*) > 1
        ) keeper
        WHERE dup.normalized_mpn = keeper.normalized_mpn
          AND dup.id != keeper.keep_id
          AND dup.deleted_at IS NULL
    """)

    # Step 4: Recreate the unique index (only on non-deleted rows)
    op.execute("""
        CREATE UNIQUE INDEX ix_material_cards_normalized_mpn
        ON material_cards (normalized_mpn)
        WHERE deleted_at IS NULL
    """)


def downgrade() -> None:
    # Cannot un-merge cards — data migration is forward-only
    pass
