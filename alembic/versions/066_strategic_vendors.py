"""Strategic Vendors — per-buyer vendor assignments with 39-day TTL.

Each buyer can claim up to 10 strategic vendors. Auto-expires if no
offer received within 39 days.

Revision ID: 066
Revises: 065
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "strategic_vendors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "vendor_card_id",
            sa.Integer(),
            sa.ForeignKey("vendor_cards.id"),
            nullable=False,
        ),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_offer_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(20), nullable=True),
        sa.UniqueConstraint("user_id", "vendor_card_id", name="uq_user_vendor_strategic"),
    )
    op.create_index(
        "ix_strategic_user_released", "strategic_vendors", ["user_id", "released_at"]
    )
    op.create_index(
        "ix_strategic_expires_released", "strategic_vendors", ["expires_at", "released_at"]
    )
    op.create_index(
        "ix_strategic_vendor_released", "strategic_vendors", ["vendor_card_id", "released_at"]
    )


def downgrade():
    op.drop_index("ix_strategic_vendor_released", table_name="strategic_vendors")
    op.drop_index("ix_strategic_expires_released", table_name="strategic_vendors")
    op.drop_index("ix_strategic_user_released", table_name="strategic_vendors")
    op.drop_table("strategic_vendors")
