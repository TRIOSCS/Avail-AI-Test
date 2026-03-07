"""Add teams_alert_config table for per-user Teams DM alert settings.

Revision ID: 059
Revises: 058
Create Date: 2026-03-07
"""

from alembic import op
import sqlalchemy as sa

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "teams_alert_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("teams_webhook_url", sa.Text(), nullable=True),
        sa.Column("alerts_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_teams_alert_config_user", "teams_alert_config", ["user_id"])


def downgrade():
    op.drop_index("ix_teams_alert_config_user")
    op.drop_table("teams_alert_config")
