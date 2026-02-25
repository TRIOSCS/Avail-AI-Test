"""Add vendor_name_normalized to material_vendor_history, offers, and contacts.

Phase 4 of Material Card Linkage: vendor name normalization for index-friendly lookups.
Replaces LOWER(TRIM(vendor_name)) patterns in queries.

Revision ID: 012_vendor_name_normalized
Revises: 011_phase3_integrity
Create Date: 2026-02-25
"""

from alembic import op
import sqlalchemy as sa

revision = "012_vendor_name_normalized"
down_revision = "011_phase3_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add nullable columns
    op.add_column(
        "material_vendor_history",
        sa.Column("vendor_name_normalized", sa.String(255), nullable=True),
    )
    op.add_column(
        "offers",
        sa.Column("vendor_name_normalized", sa.String(255), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("vendor_name_normalized", sa.String(255), nullable=True),
    )

    # 2. Backfill existing data
    # MVH: vendor_name already stores normalize_vendor_name() output — just copy
    op.execute(
        "UPDATE material_vendor_history SET vendor_name_normalized = vendor_name"
    )
    # Offers (small table): LOWER(TRIM()) is a close approximation
    op.execute(
        "UPDATE offers SET vendor_name_normalized = LOWER(TRIM(vendor_name))"
    )
    # Contacts: same
    op.execute(
        "UPDATE contacts SET vendor_name_normalized = LOWER(TRIM(vendor_name))"
    )

    # 3. Add indexes for fast lookups
    op.create_index(
        "ix_mvh_vendor_norm", "material_vendor_history", ["vendor_name_normalized"]
    )
    op.create_index(
        "ix_offers_vendor_norm", "offers", ["vendor_name_normalized"]
    )
    op.create_index(
        "ix_contacts_vendor_norm", "contacts", ["vendor_name_normalized"]
    )


def downgrade() -> None:
    op.drop_index("ix_contacts_vendor_norm", "contacts")
    op.drop_index("ix_offers_vendor_norm", "offers")
    op.drop_index("ix_mvh_vendor_norm", "material_vendor_history")
    op.drop_column("contacts", "vendor_name_normalized")
    op.drop_column("offers", "vendor_name_normalized")
    op.drop_column("material_vendor_history", "vendor_name_normalized")
