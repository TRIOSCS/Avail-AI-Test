"""Simplify ticket statuses to open/resolved.

Maps all non-resolved statuses to 'open' and 'rejected' to 'resolved'.
Also updates default from 'submitted' to 'open'.

Revision ID: 054
Revises: 053
"""

from alembic import op

revision = "054"
down_revision = "053"


def upgrade():
    # Map all old statuses to open/resolved
    op.execute("""
        UPDATE trouble_tickets
        SET status = 'open'
        WHERE status IN ('submitted', 'diagnosed', 'escalated', 'in_progress',
                         'fix_queued', 'fix_proposed', 'fix_in_progress',
                         'fix_applied', 'awaiting_verification', 'prompt_ready')
    """)
    op.execute("""
        UPDATE trouble_tickets
        SET status = 'resolved'
        WHERE status = 'rejected'
    """)
    # Update default
    op.execute("""
        ALTER TABLE trouble_tickets
        ALTER COLUMN status SET DEFAULT 'open'
    """)


def downgrade():
    # Revert default
    op.execute("""
        ALTER TABLE trouble_tickets
        ALTER COLUMN status SET DEFAULT 'submitted'
    """)
    # Can't perfectly reverse the status mapping, but set open -> submitted
    op.execute("""
        UPDATE trouble_tickets
        SET status = 'submitted'
        WHERE status = 'open'
    """)
