"""Add Communication Intelligence columns to activity_log.

Adds direction, event_type, summary, source_url, details columns.
Backfills direction and event_type from existing activity_type values.

Revision ID: 058
Revises: 057
"""

from alembic import op
import sqlalchemy as sa

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity_log", sa.Column("direction", sa.String(20)))
    op.add_column("activity_log", sa.Column("event_type", sa.String(30)))
    op.add_column("activity_log", sa.Column("summary", sa.String(500)))
    op.add_column("activity_log", sa.Column("source_url", sa.String(500)))
    op.add_column("activity_log", sa.Column("details", sa.JSON))

    # Backfill direction from activity_type
    op.execute("""
        UPDATE activity_log SET direction = CASE
            WHEN activity_type IN ('email_sent', 'call_outbound', 'phone_call') THEN 'outbound'
            WHEN activity_type IN ('email_received', 'call_inbound') THEN 'inbound'
            ELSE NULL
        END
        WHERE direction IS NULL
    """)

    # Backfill event_type from activity_type
    op.execute("""
        UPDATE activity_log SET event_type = CASE
            WHEN activity_type IN ('email_sent', 'email_received') THEN 'email'
            WHEN activity_type IN ('call_outbound', 'call_inbound', 'phone_call') THEN 'call'
            WHEN activity_type = 'note' THEN 'note'
            ELSE NULL
        END
        WHERE event_type IS NULL
    """)

    op.create_index(
        "ix_activity_user_channel_created",
        "activity_log",
        ["user_id", "channel", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_activity_user_channel_created", table_name="activity_log")
    op.drop_column("activity_log", "details")
    op.drop_column("activity_log", "source_url")
    op.drop_column("activity_log", "summary")
    op.drop_column("activity_log", "event_type")
    op.drop_column("activity_log", "direction")
