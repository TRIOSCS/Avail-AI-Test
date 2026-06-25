"""Add do_not_contact flag to customer_sites (migration 148).

Revision ID: 148_site_dnc
Revises: 147_crm_audit_trail
Create Date: 2026-06-24

Adds do_not_contact (Boolean NOT NULL server_default false) to customer_sites.
When True, the site is excluded from call-list surfaces and call cadence queues.
This replaces the Delete Site action — a DNC site is hidden from call lists
but preserved in the database.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "148_site_dnc"
down_revision = "147_crm_audit_trail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_sites",
        sa.Column(
            "do_not_contact",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("customer_sites", "do_not_contact")
