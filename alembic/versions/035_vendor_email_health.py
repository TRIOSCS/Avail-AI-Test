"""Vendor email health scoring columns.

Revision ID: 035_vendor_email_health
Revises: 034_thread_summaries
Create Date: 2026-02-28

Adds email_health_score, email_health_computed_at, response_rate,
and quote_quality_rate columns to vendor_cards table.
"""

from alembic import op

revision = "035_vendor_email_health"
down_revision = "034_thread_summaries"


def upgrade() -> None:
    op.execute("ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS email_health_score DOUBLE PRECISION")
    op.execute("ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS email_health_computed_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS response_rate DOUBLE PRECISION")
    op.execute("ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS quote_quality_rate DOUBLE PRECISION")


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS vendor_cards DROP COLUMN IF EXISTS quote_quality_rate")
    op.execute("ALTER TABLE IF EXISTS vendor_cards DROP COLUMN IF EXISTS response_rate")
    op.execute("ALTER TABLE IF EXISTS vendor_cards DROP COLUMN IF EXISTS email_health_computed_at")
    op.execute("ALTER TABLE IF EXISTS vendor_cards DROP COLUMN IF EXISTS email_health_score")
