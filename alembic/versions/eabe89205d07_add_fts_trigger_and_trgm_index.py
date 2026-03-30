"""add_fts_trigger_and_trgm_index.

Revision ID: eabe89205d07
Revises: c77eece81029
Create Date: 2026-03-30 05:45:38.172244
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eabe89205d07"
down_revision: Union[str, None] = "c77eece81029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Enable pg_trgm extension (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. Create trigger function for search_vector maintenance
    op.execute("""
        CREATE OR REPLACE FUNCTION material_cards_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.display_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.normalized_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.manufacturer, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'C') ||
                setweight(to_tsvector('english', coalesce(NEW.category, '')), 'C');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)

    # 3. Create trigger
    op.execute("""
        CREATE TRIGGER trig_material_cards_search_vector
        BEFORE INSERT OR UPDATE OF display_mpn, normalized_mpn, manufacturer, description, category
        ON material_cards
        FOR EACH ROW
        EXECUTE FUNCTION material_cards_search_vector_update();
    """)

    # 4. Backfill search_vector for all existing rows
    op.execute("""
        UPDATE material_cards SET
            search_vector =
                setweight(to_tsvector('english', coalesce(display_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(normalized_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(manufacturer, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(description, '')), 'C') ||
                setweight(to_tsvector('english', coalesce(category, '')), 'C');
    """)

    # 5. GIN index on search_vector (for FTS queries)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_material_cards_search_vector
        ON material_cards USING gin(search_vector)
    """)

    # 6. pg_trgm index on display_mpn (for typo-tolerant search)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_material_cards_trgm_mpn
        ON material_cards USING gin(display_mpn gin_trgm_ops)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_material_cards_trgm_mpn")
    op.execute("DROP INDEX IF EXISTS ix_material_cards_search_vector")
    op.execute("DROP TRIGGER IF EXISTS trig_material_cards_search_vector ON material_cards")
    op.execute("DROP FUNCTION IF EXISTS material_cards_search_vector_update()")
