"""Phase 3 data assurance: normalized_mpn on sightings/offers, soft-delete on material cards, audit table.

Revision ID: 011_phase3_integrity
Revises: 010_add_material_card_fk
Create Date: 2026-02-25
"""

from alembic import op
from sqlalchemy import text

revision = "011_phase3_integrity"
down_revision = "010_add_material_card_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Redundant normalized_mpn on sightings and offers (idempotent: column may exist from startup backfill)
    conn.execute(text("ALTER TABLE sightings ADD COLUMN IF NOT EXISTS normalized_mpn VARCHAR(255)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sightings_normalized_mpn ON sightings (normalized_mpn)"))

    conn.execute(text("ALTER TABLE offers ADD COLUMN IF NOT EXISTS normalized_mpn VARCHAR(255)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_offers_normalized_mpn ON offers (normalized_mpn)"))

    # 2. Soft-delete on material cards
    conn.execute(text("ALTER TABLE material_cards ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP"))

    # 3. Audit log table (idempotent: table may exist from prior partial run or create_all)
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS material_card_audit (
            id SERIAL PRIMARY KEY,
            material_card_id INTEGER,
            action VARCHAR(50) NOT NULL,
            entity_type VARCHAR(50),
            entity_id INTEGER,
            old_card_id INTEGER,
            new_card_id INTEGER,
            normalized_mpn VARCHAR(255),
            details JSON,
            created_at TIMESTAMP,
            created_by VARCHAR(255)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mca_material_card_id ON material_card_audit (material_card_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mca_normalized_mpn ON material_card_audit (normalized_mpn)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mca_card_action ON material_card_audit (material_card_id, action)"))


def downgrade() -> None:
    op.drop_table("material_card_audit")
    op.drop_column("material_cards", "deleted_at")
    op.drop_index("ix_offers_normalized_mpn", "offers")
    op.drop_column("offers", "normalized_mpn")
    op.drop_index("ix_sightings_normalized_mpn", "sightings")
    op.drop_column("sightings", "normalized_mpn")
