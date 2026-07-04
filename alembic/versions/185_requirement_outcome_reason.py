"""Per-part Won/Lost reason: add requirements.outcome_reason (Text).

What (DDL, reversible):
  - ADD requirements.outcome_reason (Text, NULLABLE) — the "why won / why lost"
    close reason captured when a part-line is marked Won or Lost from the Sales-Hub
    parts workspace (the per-part replacement for the removed bulk Archive action).

Nullable at the DB level; the app enforces a non-empty reason only on the transition
to WON/LOST (routers/htmx/parts.bulk_outcome), so non-closed parts stay valid. Mirrors
the requisition-level requisitions.outcome_reason (migration 158).

Downgrade: fully reversible — drops the column.

Called by: alembic (upgrade/downgrade).
Depends on: requirements (exists since the initial schema).

Revision ID: 185_req_outcome_reason
Revises: 184_user_reports_to
Create Date: 2026-07-04
"""

import sqlalchemy as sa

from alembic import op

revision = "185_req_outcome_reason"
down_revision = "184_user_reports_to"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requirements", sa.Column("outcome_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("requirements", "outcome_reason")
