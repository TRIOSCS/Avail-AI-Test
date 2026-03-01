"""Vendor email health scoring columns.

Revision ID: 035_vendor_email_health
Revises: 034_thread_summaries
Create Date: 2026-02-28

Adds email_health_score, email_health_computed_at, response_rate,
and quote_quality_rate columns to vendor_cards table.
"""

import sqlalchemy as sa

from alembic import op

revision = "035_vendor_email_health"
down_revision = "034_thread_summaries"


def upgrade() -> None:
    op.add_column("vendor_cards", sa.Column("email_health_score", sa.Float(), nullable=True))
    op.add_column("vendor_cards", sa.Column("email_health_computed_at", sa.DateTime(), nullable=True))
    op.add_column("vendor_cards", sa.Column("response_rate", sa.Float(), nullable=True))
    op.add_column("vendor_cards", sa.Column("quote_quality_rate", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("vendor_cards", "quote_quality_rate")
    op.drop_column("vendor_cards", "response_rate")
    op.drop_column("vendor_cards", "email_health_computed_at")
    op.drop_column("vendor_cards", "email_health_score")
