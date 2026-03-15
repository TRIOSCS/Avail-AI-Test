"""Add company_id FK to requisitions and unique site owner constraint.

Revision ID: 078_add_company_id
Revises: 077_sourcing_lead_foundation
Create Date: 2026-03-15

Adds:
- company_id FK column on requisitions (nullable, with index)
- Backfills company_id from customer_name matching companies.name
- Unique constraint on customer_sites(owner_id) for one-owner-per-site rule
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "078_add_company_id"
down_revision = "077_sourcing_lead_foundation"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add company_id column to requisitions
    op.add_column(
        "requisitions",
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_requisitions_company_id", "requisitions", ["company_id"])

    # 2. Backfill company_id from customer_name
    # Match requisitions.customer_name to companies.name (case-insensitive)
    op.execute("""
        UPDATE requisitions r
        SET company_id = c.id
        FROM companies c
        WHERE r.company_id IS NULL
          AND r.customer_name IS NOT NULL
          AND LOWER(TRIM(r.customer_name)) = LOWER(TRIM(c.name))
    """)

    # Also backfill from customer_site -> company
    op.execute("""
        UPDATE requisitions r
        SET company_id = cs.company_id
        FROM customer_sites cs
        WHERE r.company_id IS NULL
          AND r.customer_site_id IS NOT NULL
          AND r.customer_site_id = cs.id
    """)

    # 3. Add unique constraint on customer_sites owner_id
    # Only one site per owner (a user can own one site per company at most)
    # Note: This is a partial unique index — only enforced when owner_id is not null
    op.execute("""
        CREATE UNIQUE INDEX uq_customer_sites_owner_id
        ON customer_sites (owner_id)
        WHERE owner_id IS NOT NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_customer_sites_owner_id")
    op.drop_index("ix_requisitions_company_id", table_name="requisitions")
    op.drop_column("requisitions", "company_id")
