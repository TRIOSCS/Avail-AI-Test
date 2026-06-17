"""CRM cadence: two-clock columns (last_outbound_at/last_reply_at) + account tier.

What: adds last_outbound_at + last_reply_at to companies, customer_sites,
      site_contacts, vendor_cards, vendor_contacts; adds last_activity_at to
      site_contacts; adds tier to companies. Indexes the company-level clocks
      and tier (left-list sort/filter).
Downgrade: drops the added columns/indexes (reversible).
"""

import sqlalchemy as sa

from alembic import op

revision = "108_crm_cadence_clocks"
down_revision = "107_is_scratch_requisitions"
branch_labels = None
depends_on = None

_CLOCKS = ("last_outbound_at", "last_reply_at")


def upgrade() -> None:
    for col in _CLOCKS:
        op.add_column("companies", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("customer_sites", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("site_contacts", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("vendor_cards", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("vendor_contacts", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
    op.add_column("site_contacts", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("tier", sa.String(length=20), nullable=True))
    op.create_index("ix_companies_last_outbound_at", "companies", ["last_outbound_at"])
    op.create_index("ix_companies_last_reply_at", "companies", ["last_reply_at"])
    op.create_index("ix_companies_tier", "companies", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_companies_tier", table_name="companies")
    op.drop_index("ix_companies_last_reply_at", table_name="companies")
    op.drop_index("ix_companies_last_outbound_at", table_name="companies")
    op.drop_column("companies", "tier")
    op.drop_column("site_contacts", "last_activity_at")
    for col in _CLOCKS:
        op.drop_column("vendor_contacts", col)
        op.drop_column("vendor_cards", col)
        op.drop_column("site_contacts", col)
        op.drop_column("customer_sites", col)
        op.drop_column("companies", col)
