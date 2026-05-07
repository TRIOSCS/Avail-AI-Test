"""Teams Q&A routing — Phase 2 schema changes.

Adds nudged_at, delivered_at, answered_via to knowledge_entries.
Adds knowledge_digest_hour to teams_alert_config.
Creates knowledge_config table with daily_question_cap seed.

Revision ID: 064
Revises: 063
Create Date: 2026-03-07
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "064"
down_revision: Union[str, None] = "063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add columns to knowledge_entries
    op.execute("ALTER TABLE knowledge_entries ADD COLUMN IF NOT EXISTS nudged_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE knowledge_entries ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE knowledge_entries ADD COLUMN IF NOT EXISTS answered_via VARCHAR(10)")

    # Add digest hour to teams_alert_config
    op.execute(
        "ALTER TABLE teams_alert_config ADD COLUMN IF NOT EXISTS knowledge_digest_hour INTEGER NOT NULL DEFAULT '14'"
    )

    # Create knowledge_config table
    op.create_table(
        "knowledge_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(50), unique=True, nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        if_not_exists=True,
    )

    # Seed default question cap
    op.execute("INSERT INTO knowledge_config (key, value) VALUES ('daily_question_cap', '10')")


def downgrade() -> None:
    op.drop_table("knowledge_config", if_exists=True)
    op.execute("ALTER TABLE teams_alert_config DROP COLUMN IF EXISTS knowledge_digest_hour")
    op.execute("ALTER TABLE knowledge_entries DROP COLUMN IF EXISTS answered_via")
    op.execute("ALTER TABLE knowledge_entries DROP COLUMN IF EXISTS delivered_at")
    op.execute("ALTER TABLE knowledge_entries DROP COLUMN IF EXISTS nudged_at")
