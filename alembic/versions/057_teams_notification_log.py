"""Teams notification audit log table.

Revision ID: 057
Revises: 056
Create Date: 2026-03-07

Tracks every Teams Adaptive Card post: event type, entity, channel,
success/failure, and error details for admin troubleshooting.
"""

from alembic import op
import sqlalchemy as sa

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "teams_notification_log",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("event_type", sa.String(50), nullable=False, index=True),
        sa.Column("entity_id", sa.String(100), nullable=False),
        sa.Column("entity_name", sa.String(200), nullable=True),
        sa.Column("channel_id", sa.String(200), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, default=False),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    op.drop_table("teams_notification_log")
