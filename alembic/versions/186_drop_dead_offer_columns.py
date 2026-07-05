"""Drop dead QP/Approval "Task-2" additive columns from offers.

What (DDL, reversible):
  - DROP offers.is_primary     (Boolean, server_default 'false')
  - DROP offers.sourcing_type  (String(50))
  - DROP offers.vendor_rating  (Numeric(3, 1))
  - DROP offers.terms          (JSON)
  - DROP offers.location       (String(255))
  - DROP offers.specifics      (Text)

Why: all six were added by migration 157 (QP/Approvals Phase 1, "Task 2") as an offer
extension that was never wired to any writer or reader. No router, service, template, or
schema reads or writes any of them (verified by grep across app/ + templates/ + schemas/;
the one template reference to "terms" is the qualification-JSON key o.qualification['terms'],
not this column). is_primary always held its 'false' server_default; the other five were
always NULL. The SourcingType enum backing sourcing_type is likewise removed as dead code.

Downgrade: fully reversible — recreates all six columns as originally defined by 157 (no
data to restore, since nothing ever populated them beyond the is_primary default).

Called by: alembic (upgrade/downgrade).
Depends on: offers (table + these columns exist since migration 157_qp_approvals).

Revision ID: 186_drop_dead_offer_columns
Revises: 185_req_outcome_reason
Create Date: 2026-07-05
"""

import sqlalchemy as sa

from alembic import op

revision = "186_drop_dead_offer_columns"
down_revision = "185_req_outcome_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("offers", "specifics")
    op.drop_column("offers", "location")
    op.drop_column("offers", "terms")
    op.drop_column("offers", "vendor_rating")
    op.drop_column("offers", "sourcing_type")
    op.drop_column("offers", "is_primary")


def downgrade() -> None:
    op.add_column(
        "offers",
        sa.Column("is_primary", sa.Boolean(), nullable=True, server_default="false"),
    )
    op.add_column("offers", sa.Column("sourcing_type", sa.String(length=50), nullable=True))
    op.add_column("offers", sa.Column("vendor_rating", sa.Numeric(precision=3, scale=1), nullable=True))
    op.add_column("offers", sa.Column("terms", sa.JSON(), nullable=True))
    op.add_column("offers", sa.Column("location", sa.String(length=255), nullable=True))
    op.add_column("offers", sa.Column("specifics", sa.Text(), nullable=True))
