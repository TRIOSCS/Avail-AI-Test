"""Add notification intelligence tables and columns.

Revision ID: 062
Revises: 061
Create Date: 2026-03-07
"""

import sqlalchemy as sa

from alembic import op

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade():
    # New table: notification_engagement
    op.create_table(
        "notification_engagement",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(100), nullable=False),
        sa.Column("delivery_method", sa.String(20), nullable=False, server_default="dm"),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("response_time_s", sa.Float, nullable=True),
        sa.Column("ai_priority", sa.String(20), nullable=True),
        sa.Column("ai_confidence", sa.Float, nullable=True),
        sa.Column("suppression_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_notif_engage_user_event", "notification_engagement", ["user_id", "event_type"])
    op.create_index("ix_notif_engage_created", "notification_engagement", ["created_at"])
    op.create_index("ix_notif_engage_user_action", "notification_engagement", ["user_id", "action"])

    # Extend teams_alert_config
    op.add_column(
        "teams_alert_config", sa.Column("priority_threshold", sa.String(20), nullable=False, server_default="medium")
    )
    op.add_column(
        "teams_alert_config", sa.Column("batch_digest_enabled", sa.Boolean, nullable=False, server_default="true")
    )
    op.add_column("teams_alert_config", sa.Column("quiet_hours_start", sa.Time, nullable=True))
    op.add_column("teams_alert_config", sa.Column("quiet_hours_end", sa.Time, nullable=True))

    # Extend teams_notification_log
    op.add_column("teams_notification_log", sa.Column("user_id", sa.Integer, nullable=True))
    op.add_column("teams_notification_log", sa.Column("ai_priority", sa.String(20), nullable=True))
    op.add_column("teams_notification_log", sa.Column("ai_decision", sa.String(20), nullable=True))
    op.add_column("teams_notification_log", sa.Column("batch_id", sa.String(50), nullable=True))


def downgrade():
    op.drop_column("teams_notification_log", "batch_id")
    op.drop_column("teams_notification_log", "ai_decision")
    op.drop_column("teams_notification_log", "ai_priority")
    op.drop_column("teams_notification_log", "user_id")

    op.drop_column("teams_alert_config", "quiet_hours_end")
    op.drop_column("teams_alert_config", "quiet_hours_start")
    op.drop_column("teams_alert_config", "batch_digest_enabled")
    op.drop_column("teams_alert_config", "priority_threshold")

    op.drop_index("ix_notif_engage_user_action", "notification_engagement")
    op.drop_index("ix_notif_engage_created", "notification_engagement")
    op.drop_index("ix_notif_engage_user_event", "notification_engagement")
    op.drop_table("notification_engagement")
