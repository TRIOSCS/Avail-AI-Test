"""Simplify ticket statuses to 4: open, in_progress, resolved, escalated.

Maps old → new:
  submitted, triaging → open
  diagnosed, prompt_ready, fix_in_progress, awaiting_verification → in_progress
  rejected → resolved
  resolved → resolved (no change)
  escalated → escalated (no change)

Revision ID: 044
Revises: 043
"""

from alembic import op

revision = "044_simplify_ticket_statuses"
down_revision = "043_unify_ticket_tables"


def upgrade():
    # Map old statuses to new simplified set
    op.execute("""
        UPDATE trouble_tickets SET status = 'open'
        WHERE status IN ('submitted', 'triaging')
    """)
    op.execute("""
        UPDATE trouble_tickets SET status = 'in_progress'
        WHERE status IN ('diagnosed', 'prompt_ready', 'fix_in_progress', 'awaiting_verification')
    """)
    op.execute("""
        UPDATE trouble_tickets SET status = 'resolved'
        WHERE status = 'rejected'
    """)


def downgrade():
    # Best-effort reverse: open → submitted, in_progress → diagnosed
    op.execute("UPDATE trouble_tickets SET status = 'submitted' WHERE status = 'open'")
    op.execute("UPDATE trouble_tickets SET status = 'diagnosed' WHERE status = 'in_progress'")
