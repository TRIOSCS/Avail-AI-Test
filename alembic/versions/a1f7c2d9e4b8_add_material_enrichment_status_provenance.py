"""Add material_cards.enrichment_status and enrichment_provenance.

enrichment_status (VARCHAR 20, NOT NULL, server_default 'unenriched', indexed)
marks a card as unenriched | verified | ai_inferred | not_found.
enrichment_provenance (JSONB, nullable) records per-field source attribution.

Revision ID: a1f7c2d9e4b8
Revises: 087_add_specs_enriched_at
Create Date: 2026-06-04 22:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1f7c2d9e4b8"
down_revision: Union[str, None] = "087_add_specs_enriched_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "material_cards",
        sa.Column(
            "enrichment_status",
            sa.String(length=20),
            nullable=False,
            server_default="unenriched",
        ),
    )
    op.add_column(
        "material_cards",
        sa.Column("enrichment_provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_material_cards_enrichment_status",
        "material_cards",
        ["enrichment_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_material_cards_enrichment_status", table_name="material_cards", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS material_cards DROP COLUMN IF EXISTS enrichment_provenance")
    op.execute("ALTER TABLE IF EXISTS material_cards DROP COLUMN IF EXISTS enrichment_status")
