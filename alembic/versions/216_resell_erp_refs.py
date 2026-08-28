"""216_resell_erp_refs — ERP reference columns on CustomerBid / ExcessOffer (C3).

Adds two nullable String(100) reference-only columns so a trader can record the
ERP document number tied to a resell transaction, without AVAIL owning that
document's lifecycle (nothing integrates with Acctivate; references only):
- ``customer_bids.po_number`` — the customer's PO number issued against the
  accepted bid-back, cut in the ERP.
- ``excess_offers.sales_order_number`` — the sales order number the accepted
  inbound offer was fulfilled under, cut in the ERP.

Additive + reversible (downgrade drops both). Round-tripped on throwaway PG.
Chains onto 215_dedup_decisions.

Revision ID: 216_resell_erp_refs
Revises: 215_dedup_decisions
"""

import sqlalchemy as sa

from alembic import op

revision = "216_resell_erp_refs"
down_revision = "215_dedup_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_bids", sa.Column("po_number", sa.String(length=100), nullable=True))
    op.add_column("excess_offers", sa.Column("sales_order_number", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("excess_offers", "sales_order_number")
    op.drop_column("customer_bids", "po_number")
