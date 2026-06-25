"""Vendor parity P1: VendorContact.is_primary + VendorCard.custom_fields (migration 145).

Revision ID: 145_vendor_parity_p1
Revises: 144_contact_fields
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "145_vendor_parity_p1"
down_revision = "144_contact_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendor_contacts",
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "vendor_cards",
        sa.Column(
            "custom_fields",
            JSONB(),
            nullable=True,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("vendor_cards", "custom_fields")
    op.drop_column("vendor_contacts", "is_primary")
