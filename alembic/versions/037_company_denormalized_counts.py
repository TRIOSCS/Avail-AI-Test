"""Add denormalized site_count and open_req_count to companies.

Revision ID: 037_company_denormalized_counts
Revises: 036_unified_score_snapshot
Create Date: 2026-03-01

Adds site_count and open_req_count columns to companies table,
then backfills from customer_sites and requisitions.
"""

from alembic import op

revision = "037_company_denormalized_counts"
down_revision = "036_unified_score_snapshot"


def upgrade() -> None:
    op.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS site_count INTEGER DEFAULT '0'")
    op.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS open_req_count INTEGER DEFAULT '0'")

    # Backfill site_count
    op.execute("""
        UPDATE companies c SET site_count = (
            SELECT COUNT(*) FROM customer_sites cs
            WHERE cs.company_id = c.id AND cs.is_active = TRUE
        )
    """)

    # Backfill open_req_count
    op.execute("""
        UPDATE companies c SET open_req_count = (
            SELECT COUNT(*) FROM requisitions r
            JOIN customer_sites cs ON r.customer_site_id = cs.id
            WHERE cs.company_id = c.id
              AND r.status NOT IN ('archived', 'won', 'lost')
        )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS companies DROP COLUMN IF EXISTS open_req_count")
    op.execute("ALTER TABLE IF EXISTS companies DROP COLUMN IF EXISTS site_count")
