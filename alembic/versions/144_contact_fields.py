"""Contact secondary email/phone + reports-to hierarchy (migration 144).

Revision ID: 144_contact_fields
Revises: 143_vendor_attachments
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "144_contact_fields"
down_revision = "143_vendor_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("site_contacts", sa.Column("secondary_email", sa.String(255), nullable=True))
    op.add_column("site_contacts", sa.Column("secondary_phone", sa.String(100), nullable=True))
    op.add_column(
        "site_contacts",
        sa.Column("reports_to_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_sc_reports_to", "site_contacts", ["reports_to_id"])
    op.create_foreign_key(
        "fk_sc_reports_to",
        "site_contacts",
        "site_contacts",
        ["reports_to_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_sc_reports_to", "site_contacts", type_="foreignkey")
    op.drop_index("ix_sc_reports_to", table_name="site_contacts")
    op.drop_column("site_contacts", "reports_to_id")
    op.drop_column("site_contacts", "secondary_phone")
    op.drop_column("site_contacts", "secondary_email")
