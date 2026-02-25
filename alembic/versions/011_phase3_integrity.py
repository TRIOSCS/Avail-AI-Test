"""Phase 3 data assurance: normalized_mpn on sightings/offers, soft-delete on material cards, audit table.

Revision ID: 011_phase3_integrity
Revises: 010_add_material_card_fk
Create Date: 2026-02-25
"""

from alembic import op
import sqlalchemy as sa

revision = "011_phase3_integrity"
down_revision = "010_add_material_card_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Redundant normalized_mpn on sightings and offers (backup linkage key)
    op.add_column("sightings", sa.Column("normalized_mpn", sa.String(255), nullable=True))
    op.create_index("ix_sightings_normalized_mpn", "sightings", ["normalized_mpn"])

    op.add_column("offers", sa.Column("normalized_mpn", sa.String(255), nullable=True))
    op.create_index("ix_offers_normalized_mpn", "offers", ["normalized_mpn"])

    # 2. Soft-delete on material cards
    op.add_column("material_cards", sa.Column("deleted_at", sa.DateTime(), nullable=True))

    # 3. Audit log table
    op.create_table(
        "material_card_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("material_card_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("old_card_id", sa.Integer(), nullable=True),
        sa.Column("new_card_id", sa.Integer(), nullable=True),
        sa.Column("normalized_mpn", sa.String(255), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
    )
    op.create_index("ix_mca_material_card_id", "material_card_audit", ["material_card_id"])
    op.create_index("ix_mca_normalized_mpn", "material_card_audit", ["normalized_mpn"])
    op.create_index("ix_mca_card_action", "material_card_audit", ["material_card_id", "action"])


def downgrade() -> None:
    op.drop_table("material_card_audit")
    op.drop_column("material_cards", "deleted_at")
    op.drop_index("ix_offers_normalized_mpn", "offers")
    op.drop_column("offers", "normalized_mpn")
    op.drop_index("ix_sightings_normalized_mpn", "sightings")
    op.drop_column("sightings", "normalized_mpn")
