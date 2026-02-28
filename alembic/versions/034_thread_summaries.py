"""Thread summaries — no schema changes needed (columns already in 032).

Revision ID: 034_thread_summaries
Revises: 033_graph_intelligence
Create Date: 2026-02-28

The thread_summary JSON column and conversation_id index were already
created in 032_email_intelligence. This migration is a no-op placeholder
to keep the plan's migration sequence.
"""

revision = "034_thread_summaries"
down_revision = "033_graph_intelligence"


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
