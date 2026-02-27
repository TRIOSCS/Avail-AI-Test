"""Create reactivation_signals table for churn/reactivation tracking.

Revision ID: 028_reactivation_signals
Revises: 027_quote_lines
Create Date: 2026-02-27
"""

import sqlalchemy as sa
from alembic import op

revision = "028_reactivation_signals"
down_revision = "027_quote_lines"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reactivation_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("material_card_id", sa.Integer(), sa.ForeignKey("material_cards.id", ondelete="SET NULL")),
        sa.Column("signal_type", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("dismissed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_reactivation_company", "reactivation_signals", ["company_id"])
    op.create_index("ix_reactivation_type", "reactivation_signals", ["signal_type"])


def downgrade():
    op.drop_table("reactivation_signals")
