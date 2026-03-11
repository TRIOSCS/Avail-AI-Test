"""Replace lifetime unique with partial unique index for active vendor claims.

Only one buyer can hold a given vendor at a time (released_at IS NULL).
After release, the same vendor can be claimed again by anyone.

Revision ID: 066
Revises: 065
Create Date: 2026-03-08
"""

import sqlalchemy as sa

from alembic import op

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the table if it doesn't exist yet (new feature)
    conn = op.get_bind()
    if not conn.dialect.has_table(conn, "strategic_vendors"):
        op.create_table(
            "strategic_vendors",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("vendor_card_id", sa.Integer(), sa.ForeignKey("vendor_cards.id"), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_offer_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("release_reason", sa.String(20), nullable=True),
        )
        # Create with the partial unique index from the start
        op.execute(
            "CREATE UNIQUE INDEX uq_active_vendor_claim ON strategic_vendors (vendor_card_id) WHERE released_at IS NULL"
        )
        op.create_index("ix_strategic_user_released", "strategic_vendors", ["user_id", "released_at"])
        op.create_index("ix_strategic_expires_released", "strategic_vendors", ["expires_at", "released_at"])
        op.create_index("ix_strategic_vendor_released", "strategic_vendors", ["vendor_card_id", "released_at"])
    else:
        # Table exists — replace lifetime unique with partial unique
        op.drop_constraint("uq_user_vendor_strategic", "strategic_vendors", type_="unique")
        op.execute(
            "CREATE UNIQUE INDEX uq_active_vendor_claim ON strategic_vendors (vendor_card_id) WHERE released_at IS NULL"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_active_vendor_claim")
    conn = op.get_bind()
    if conn.dialect.has_table(conn, "strategic_vendors"):
        op.create_unique_constraint("uq_user_vendor_strategic", "strategic_vendors", ["user_id", "vendor_card_id"])
