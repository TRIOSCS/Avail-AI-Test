"""Add do_not_contact flag to site_contacts.

What: adds site_contacts.do_not_contact (Boolean NOT NULL, server_default false)
      so DNC contacts are persisted durably and can be enforced server-side.
Downgrade: drops the column.

Revision ID: 116_site_contact_dnc
Revises: 115_subscription_health
Create Date: 2026-06-18
"""

import sqlalchemy as sa

from alembic import op

revision = "116_site_contact_dnc"
down_revision = "115_subscription_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "site_contacts",
        sa.Column(
            "do_not_contact",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("site_contacts", "do_not_contact")
