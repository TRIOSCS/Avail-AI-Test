"""Unify error_reports into trouble_tickets — add columns for screenshot, browser
context, AI prompt, admin notes, and source tracking.

Migrates existing error_reports data into trouble_tickets. Keeps
error_reports table for rollback safety (dropped in a future migration).

Revision ID: 043_unify_ticket_tables
Revises: 042_add_tagging_tables
"""

from alembic import op

revision = "043_unify_ticket_tables"
down_revision = "042_add_tagging_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add new columns to trouble_tickets
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS screenshot_b64 TEXT")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS browser_info VARCHAR(512)")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS screen_size VARCHAR(50)")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS page_state TEXT")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS ai_prompt TEXT")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS admin_notes TEXT")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS resolved_by_id INTEGER")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS legacy_error_report_id INTEGER")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS console_errors TEXT")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS current_view VARCHAR(100)")

    # 2. Index on source for filtered queries
    op.create_index("ix_trouble_tickets_source", "trouble_tickets", ["source"], if_not_exists=True)

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
    op.drop_index("ix_trouble_tickets_source", table_name="trouble_tickets", if_exists=True)
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS current_view")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS console_errors")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS legacy_error_report_id")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS source")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS resolved_by_id")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS admin_notes")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS ai_prompt")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS page_state")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS screen_size")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS browser_info")
    op.execute("ALTER TABLE trouble_tickets DROP COLUMN IF EXISTS screenshot_b64")
