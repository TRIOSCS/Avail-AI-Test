"""Unify error_reports into trouble_tickets — add columns for screenshot,
browser context, AI prompt, admin notes, and source tracking.

Migrates existing error_reports data into trouble_tickets. Keeps
error_reports table for rollback safety (dropped in a future migration).

Revision ID: 043_unify_ticket_tables
Revises: 042_add_tagging_tables
"""

import sqlalchemy as sa

from alembic import op

revision = "043_unify_ticket_tables"
down_revision = "042_add_tagging_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add new columns to trouble_tickets
    op.add_column("trouble_tickets", sa.Column("screenshot_b64", sa.Text(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("browser_info", sa.String(512), nullable=True))
    op.add_column("trouble_tickets", sa.Column("screen_size", sa.String(50), nullable=True))
    op.add_column("trouble_tickets", sa.Column("page_state", sa.Text(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("ai_prompt", sa.Text(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("admin_notes", sa.Text(), nullable=True))
    op.add_column(
        "trouble_tickets",
        sa.Column("resolved_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("trouble_tickets", sa.Column("source", sa.String(20), nullable=True))
    op.add_column("trouble_tickets", sa.Column("legacy_error_report_id", sa.Integer(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("console_errors", sa.Text(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("current_view", sa.String(100), nullable=True))

    # 2. Index on source for filtered queries
    op.create_index("ix_trouble_tickets_source", "trouble_tickets", ["source"])

    # 3. Migrate data from error_reports into trouble_tickets
    #    Map statuses: open → submitted, in_progress → diagnosed, resolved → resolved, closed → rejected
    #    Skip rows that already have a linked ER- ticket (avoid duplicates)
    op.execute(
        """
        INSERT INTO trouble_tickets (
            ticket_number, submitted_by, status, title, description,
            current_page, user_agent,
            screenshot_b64, browser_info, screen_size, page_state,
            console_errors, current_view,
            ai_prompt, admin_notes, resolved_by_id, resolved_at,
            source, legacy_error_report_id, created_at
        )
        SELECT
            'ER-' || LPAD(er.id::text, 5, '0'),
            er.user_id,
            CASE er.status
                WHEN 'open' THEN 'submitted'
                WHEN 'in_progress' THEN 'diagnosed'
                WHEN 'resolved' THEN 'resolved'
                WHEN 'closed' THEN 'rejected'
                ELSE 'submitted'
            END,
            er.title,
            COALESCE(er.description, er.title),
            er.current_url,
            er.browser_info,
            er.screenshot_b64,
            er.browser_info,
            er.screen_size,
            er.page_state,
            er.console_errors,
            er.current_view,
            er.ai_prompt,
            er.admin_notes,
            er.resolved_by_id,
            er.resolved_at,
            'report_button',
            er.id,
            er.created_at
        FROM error_reports er
        WHERE NOT EXISTS (
            SELECT 1 FROM trouble_tickets tt
            WHERE tt.ticket_number = 'ER-' || LPAD(er.id::text, 5, '0')
        )
        """
    )

    # 4. Backfill source='ticket_form' for existing trouble_tickets without a source
    op.execute(
        """
        UPDATE trouble_tickets
        SET source = 'ticket_form'
        WHERE source IS NULL AND legacy_error_report_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_trouble_tickets_source", table_name="trouble_tickets")
    op.drop_column("trouble_tickets", "current_view")
    op.drop_column("trouble_tickets", "console_errors")
    op.drop_column("trouble_tickets", "legacy_error_report_id")
    op.drop_column("trouble_tickets", "source")
    op.drop_column("trouble_tickets", "resolved_by_id")
    op.drop_column("trouble_tickets", "admin_notes")
    op.drop_column("trouble_tickets", "ai_prompt")
    op.drop_column("trouble_tickets", "page_state")
    op.drop_column("trouble_tickets", "screen_size")
    op.drop_column("trouble_tickets", "browser_info")
    op.drop_column("trouble_tickets", "screenshot_b64")
