"""Resell outreach send-truthfulness: add excess_outreach.send_error (Text).

What (DDL, reversible):
  - ADD excess_outreach.send_error (Text, NULLABLE) — the persisted send-failure reason
    stamped on the new ``failed`` / ``interrupted`` outreach statuses so the tracker can
    show WHY a send failed and the retry path has context. NULL on a clean send.

The two new ExcessOutreachStatus members (``failed`` / ``interrupted``) need NO DDL:
``excess_outreach.status`` is a plain ``String(20)`` with NO check constraint, validated
app-side by the model ``@validates`` hook, so the enum grows without a schema change.

Downgrade: fully reversible — drops the column.

Called by: alembic (upgrade/downgrade).
Depends on: excess_outreach (created in 133_resell_outreach_schema).

Revision ID: 194_excess_outreach_failed_states
Revises: 193_resell_legacy_status_remap
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "194_excess_outreach_failed_states"
down_revision = "193_resell_legacy_status_remap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("excess_outreach", sa.Column("send_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("excess_outreach", "send_error")
