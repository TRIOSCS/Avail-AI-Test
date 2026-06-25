"""Add provenance + firmographic columns to companies and vendor_cards.

Adds ticker, naics, revenue_range (String) and enrichment_provenance (JSONB)
to both tables to support Explorium+Clay blending with per-field source tracking.

Revision ID: 125_enrichment_provenance
Revises: 124_offer_status_constraint
Create Date: 2026-06-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "125_enrichment_provenance"
down_revision: Union[str, None] = "124_offer_status_constraint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # companies: add ticker, naics, revenue_range, enrichment_provenance
    op.add_column("companies", sa.Column("ticker", sa.String(length=20), nullable=True))
    op.add_column("companies", sa.Column("naics", sa.String(length=20), nullable=True))
    op.add_column("companies", sa.Column("revenue_range", sa.String(length=50), nullable=True))
    op.add_column(
        "companies",
        sa.Column(
            "enrichment_provenance",
            JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=True,
        ),
    )

    # vendor_cards: mirror the same 4 columns
    op.add_column("vendor_cards", sa.Column("ticker", sa.String(length=20), nullable=True))
    op.add_column("vendor_cards", sa.Column("naics", sa.String(length=20), nullable=True))
    op.add_column("vendor_cards", sa.Column("revenue_range", sa.String(length=50), nullable=True))
    op.add_column(
        "vendor_cards",
        sa.Column(
            "enrichment_provenance",
            JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("vendor_cards", "enrichment_provenance")
    op.drop_column("vendor_cards", "revenue_range")
    op.drop_column("vendor_cards", "naics")
    op.drop_column("vendor_cards", "ticker")

    op.drop_column("companies", "enrichment_provenance")
    op.drop_column("companies", "revenue_range")
    op.drop_column("companies", "naics")
    op.drop_column("companies", "ticker")
