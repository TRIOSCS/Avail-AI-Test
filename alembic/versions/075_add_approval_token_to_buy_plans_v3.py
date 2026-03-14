"""Add approval_token and token_expires_at columns to buy_plans_v3.

Enables token-based approval flow where managers can approve/reject
buy plans via emailed links without logging in.

Revision ID: 075
Revises: 074
Create Date: 2026-03-13
"""

import sqlalchemy as sa

from alembic import op

revision = "075"
down_revision = "074"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("buy_plans_v3", sa.Column("approval_token", sa.String(100), unique=True))
    op.add_column("buy_plans_v3", sa.Column("token_expires_at", sa.DateTime()))
    op.create_index("ix_bpv3_token", "buy_plans_v3", ["approval_token"])


def downgrade():
    op.drop_index("ix_bpv3_token", table_name="buy_plans_v3")
    op.drop_column("buy_plans_v3", "token_expires_at")
    op.drop_column("buy_plans_v3", "approval_token")
