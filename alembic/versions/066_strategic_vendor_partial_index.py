"""Replace lifetime unique with partial unique index for active vendor claims.

Only one buyer can hold a given vendor at a time (released_at IS NULL).
After release, the same vendor can be claimed again by anyone.

Revision ID: 066
Revises: 065
Create Date: 2026-03-08
"""

from alembic import op

revision = "066_strategic_vendor_partial_index"
down_revision = "065_requisition_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_user_vendor_strategic", "strategic_vendors", type_="unique")
    op.execute(
        "CREATE UNIQUE INDEX uq_active_vendor_claim "
        "ON strategic_vendors (vendor_card_id) "
        "WHERE released_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_active_vendor_claim")
    op.create_unique_constraint(
        "uq_user_vendor_strategic", "strategic_vendors", ["user_id", "vendor_card_id"]
    )
