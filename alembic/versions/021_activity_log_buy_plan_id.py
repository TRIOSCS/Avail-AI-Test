"""Add buy_plan_id column to activity_log and dismiss stale vendor_reply_review.

Adds a nullable FK to buy_plans_v3 so buy plan notifications can link directly
to the plan. Also dismisses all existing unread vendor_reply_review notifications
since that notification type has been removed.

Revision ID: 021_activity_buy_plan_id
Revises: 020_buy_plan_v3
Create Date: 2026-02-27
"""

import sqlalchemy as sa

from alembic import op

revision = "021_activity_buy_plan_id"
down_revision = "020_buy_plan_v3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "activity_log",
        sa.Column(
            "buy_plan_id",
            sa.Integer(),
            sa.ForeignKey("buy_plans_v3.id"),
            nullable=True,
        ),
    )
    # Dismiss all existing unread vendor_reply_review notifications
    op.execute(
        "UPDATE activity_log SET dismissed_at = NOW() "
        "WHERE activity_type = 'vendor_reply_review' AND dismissed_at IS NULL"
    )


def downgrade():
    op.drop_column("activity_log", "buy_plan_id")
