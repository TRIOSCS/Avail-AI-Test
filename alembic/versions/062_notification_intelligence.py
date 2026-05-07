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
        if_not_exists=True,
    )
    op.create_index(
        "ix_notif_engage_user_event", "notification_engagement", ["user_id", "event_type"], if_not_exists=True
    )
    op.create_index("ix_notif_engage_created", "notification_engagement", ["created_at"], if_not_exists=True)
    op.create_index("ix_notif_engage_user_action", "notification_engagement", ["user_id", "action"], if_not_exists=True)

    # Extend teams_alert_config
    op.execute(
        "ALTER TABLE teams_alert_config ADD COLUMN IF NOT EXISTS priority_threshold VARCHAR(20) NOT NULL DEFAULT 'medium'"
    )
    op.add_column(
        "teams_alert_config", sa.Column("batch_digest_enabled", sa.Boolean, nullable=False, server_default="true")
    )
    op.add_column("teams_alert_config", sa.Column("quiet_hours_start", sa.Time, nullable=True))
    op.add_column("teams_alert_config", sa.Column("quiet_hours_end", sa.Time, nullable=True))

    # Extend teams_notification_log
    op.add_column("teams_notification_log", sa.Column("user_id", sa.Integer, nullable=True))
    op.execute("ALTER TABLE teams_notification_log ADD COLUMN IF NOT EXISTS ai_priority VARCHAR(20)")
    op.execute("ALTER TABLE teams_notification_log ADD COLUMN IF NOT EXISTS ai_decision VARCHAR(20)")
    op.execute("ALTER TABLE teams_notification_log ADD COLUMN IF NOT EXISTS batch_id VARCHAR(50)")


def downgrade():
    op.execute("ALTER TABLE IF EXISTS teams_notification_log DROP COLUMN IF EXISTS batch_id")
    op.execute("ALTER TABLE IF EXISTS teams_notification_log DROP COLUMN IF EXISTS ai_decision")
    op.execute("ALTER TABLE IF EXISTS teams_notification_log DROP COLUMN IF EXISTS ai_priority")
    op.execute("ALTER TABLE IF EXISTS teams_notification_log DROP COLUMN IF EXISTS user_id")

    op.execute("ALTER TABLE IF EXISTS teams_alert_config DROP COLUMN IF EXISTS quiet_hours_end")
    op.execute("ALTER TABLE IF EXISTS teams_alert_config DROP COLUMN IF EXISTS quiet_hours_start")
    op.execute("ALTER TABLE IF EXISTS teams_alert_config DROP COLUMN IF EXISTS batch_digest_enabled")
    op.execute("ALTER TABLE IF EXISTS teams_alert_config DROP COLUMN IF EXISTS priority_threshold")

    op.drop_index("ix_notif_engage_user_action", "notification_engagement", if_exists=True)
    op.drop_index("ix_notif_engage_created", "notification_engagement", if_exists=True)
    op.drop_index("ix_notif_engage_user_event", "notification_engagement", if_exists=True)
    op.drop_table("notification_engagement", if_exists=True)
