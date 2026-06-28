"""Add vendor_cards.is_active for vendor soft-archive (CRM P5 slice).

What: adds vendor_cards.is_active (Boolean NOT NULL, server_default true) + an index
      so vendors can be soft-archived (is_active=False) — hidden from the default vendor
      list/search but never deleted. Mirrors the customer/company soft-archive
      (Company.is_active, migrations 139/149) and SiteContact.is_active.

      The server_default guarantees every existing row becomes is_active=true the moment
      the column is added, so no separate backfill is needed.

Downgrade: drop the index, then the column.

Revision ID: 165_vendor_is_active
Revises: 164_sp2_qp_sales_rename
Create Date: 2026-06-28
"""

import sqlalchemy as sa

from alembic import op

revision = "165_vendor_is_active"
down_revision = "164_sp2_qp_sales_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendor_cards",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index("ix_vendor_cards_is_active", "vendor_cards", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_vendor_cards_is_active", table_name="vendor_cards")
    op.drop_column("vendor_cards", "is_active")
