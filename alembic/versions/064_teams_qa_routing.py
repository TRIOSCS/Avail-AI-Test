"""Teams Q&A routing — Phase 2 schema changes.

Adds nudged_at, delivered_at, answered_via to knowledge_entries.
Adds knowledge_digest_hour to teams_alert_config.
Creates knowledge_config table with daily_question_cap seed.

Revision ID: 064
Revises: 063
Create Date: 2026-03-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "064"
down_revision: Union[str, None] = "063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add columns to knowledge_entries
    op.add_column("knowledge_entries", sa.Column("nudged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge_entries", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge_entries", sa.Column("answered_via", sa.String(10), nullable=True))

    # Add digest hour to teams_alert_config
    op.add_column("teams_alert_config", sa.Column("knowledge_digest_hour", sa.Integer(), nullable=False, server_default="14"))

    # Create knowledge_config table
    op.create_table(
        "knowledge_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(50), unique=True, nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
    )

    # Seed default question cap
    op.execute("INSERT INTO knowledge_config (key, value) VALUES ('daily_question_cap', '10')")


def downgrade() -> None:
    op.drop_table("knowledge_config")
    op.drop_column("teams_alert_config", "knowledge_digest_hour")
    op.drop_column("knowledge_entries", "answered_via")
    op.drop_column("knowledge_entries", "delivered_at")
    op.drop_column("knowledge_entries", "nudged_at")
