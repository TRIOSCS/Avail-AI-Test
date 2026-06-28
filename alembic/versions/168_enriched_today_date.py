"""Persist the enrichment worker's daily-cap date (enriched_today_date).

Adds ``enrichment_worker_status.enriched_today_date`` (nullable Date). The worker's
``enriched_today`` counter was already persisted, but the worker held its running total
purely in memory and never read it back on startup, so every container restart reset the
daily-cap budget to 0. Tagging the persisted count with the UTC date it belongs to lets
the worker RESUME the count on a same-day restart (the daily cap stays enforced) and
reset only when the stored date != today.

Nullable so the existing singleton row upgrades cleanly without a backfill; the worker
sets it on its first write. Additive + fully reversible.

Revision ID: 168_enriched_today_date
Revises: 164_sp2_qp_sales_rename

NOTE: revision id is <= 32 chars — Alembic's ``alembic_version.version_num`` column is
``VARCHAR(32)`` on PostgreSQL. See tests/test_migration_revision_ids.py.
Create Date: 2026-06-28
"""

import sqlalchemy as sa

from alembic import op

revision = "168_enriched_today_date"
down_revision = "165_vendor_is_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "enrichment_worker_status",
        sa.Column("enriched_today_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("enrichment_worker_status", "enriched_today_date")
