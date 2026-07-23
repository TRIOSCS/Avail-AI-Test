"""Resell outreach retry fidelity: persist the recipient address actually used at send.

What (DDL, reversible):
  - ADD excess_outreach.recipient_email (Text, NULLABLE) — the buyer email address
    ACTUALLY used when the row was sent/enqueued.

Why (deep-review #2, finding B5): before this, the row never persisted who it went to —
retry_outreach_send and the Sent-folder reconcile re-resolved the card's CURRENT primary
email at retry time. If the card's emails JSON changed between the original send and a
later retry (an enrichment/merge routinely prepends a new address), the reconcile queried
the WRONG mailbox: an actually-delivered message on the ORIGINAL address is never found,
so the row is never reconciled to ``sent`` and the retry RESENDS to a stale/replaced
address (or worse, misses a genuine delivery entirely). Persisting the send-time address
lets retry/reconcile match against the mailbox the message was really sent to; a legacy
row (NULL — pre-migration) falls back to the card's current email as before.

Downgrade: fully reversible — drops the column (additive-reverse, no data to restore).

Called by: alembic (upgrade/downgrade).
Depends on: excess_outreach (created in 133_resell_outreach_schema).

Revision ID: 203_outreach_recipient_email
Revises: 202_restore_trgm_indexes
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "203_outreach_recipient_email"
down_revision = "202_restore_trgm_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("excess_outreach", sa.Column("recipient_email", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("excess_outreach", "recipient_email")
