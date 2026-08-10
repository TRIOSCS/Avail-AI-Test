"""207_quote_customer_message — split quote notes into internal vs customer-facing.

QC 2026-08-10 P0-4: `quotes.notes` is labeled "Internal Notes" in the editor but
was rendered into the customer email — internal content (cost reminders,
push-back strategy) leaked to customers. Add a separate customer-facing
`customer_message` column; `notes` stays internal-only and is no longer emailed.

Additive + reversible (downgrade drops the column; no data to restore — existing
`notes` content stays put in `notes`, which is now internal-only). Chains onto
206_part_equivalences. Round-tripped upgrade->downgrade->upgrade on a throwaway
PG scratch DB.

Revision ID: 207_quote_customer_message
Revises: 206_part_equivalences
"""

import sqlalchemy as sa

from alembic import op

revision = "207_quote_customer_message"
down_revision = "206_part_equivalences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("customer_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("quotes", "customer_message")
