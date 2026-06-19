"""Add quotes.source column for proactive revenue attribution.

What: adds quotes.source (String 50, nullable) to mark quotes that originated from
      proactive selling. NULL = manual/unknown; 'proactive' = proactive-sell flow.
Downgrade: drops the column.

Revision ID: 113_quote_source
Revises: 112_offer_qualification
Create Date: 2026-06-18
"""

import sqlalchemy as sa

from alembic import op

revision = "113_quote_source"
down_revision = "112_offer_qualification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("source", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("quotes", "source")
