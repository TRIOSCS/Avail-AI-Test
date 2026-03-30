"""Add missing indexes on Company and CustomerSite for performance.

Indexes added:
- companies.is_active — heavily filtered in list views
- companies.last_activity_at — used in ORDER BY for main list
- companies.is_strategic — used in ORDER BY
- customer_sites.is_active — filtered frequently

Revision ID: 083_crm_indexes
Revises: ed9318daa67c
"""

from alembic import op

revision = "083_crm_indexes"
down_revision = "ed9318daa67c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_companies_is_active", "companies", ["is_active"])
    op.create_index("ix_companies_last_activity_at", "companies", ["last_activity_at"])
    op.create_index("ix_companies_is_strategic", "companies", ["is_strategic"])
    op.create_index("ix_customer_sites_is_active", "customer_sites", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_customer_sites_is_active", table_name="customer_sites")
    op.drop_index("ix_companies_is_strategic", table_name="companies")
    op.drop_index("ix_companies_last_activity_at", table_name="companies")
    op.drop_index("ix_companies_is_active", table_name="companies")
