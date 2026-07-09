"""Backfill discovery_batches status complete to completed.

Data-only migration: app/services/prospect_scheduler.py renamed the persisted
DiscoveryBatch status literal "complete" -> "completed" (DiscoveryBatchStatus.COMPLETED).
Existing rows written before that rename still hold "complete" and would silently stop
matching get_next_discovery_slice's rotation filter (DiscoveryBatch.status ==
DiscoveryBatchStatus.COMPLETED), resetting the Explorium rotation. No schema change is
required, so this mirrors the op.get_bind() + raw-SQL text() pattern used by
70b4dce3cf67_backfill_vendor_sighting_summary_pre_.py rather than autogenerate.

Called by: alembic (upgrade/downgrade).
Depends on: discovery_batches table (status column, since migration
009_prospect_accounts_discovery_batches).

Revision ID: a431c202afa4
Revises: 186_drop_dead_offer_columns
Create Date: 2026-07-09 05:35:40.720913
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a431c202afa4"
down_revision: str | None = "186_drop_dead_offer_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE discovery_batches SET status = 'completed' WHERE status = 'complete'"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE discovery_batches SET status = 'complete' WHERE status = 'completed'"))
