"""Phase 1: Per-part sourcing status, buyer claim, and sales context fields.

Adds sourcing_status to requirements (per-part tracking),
claimed_by_id/claimed_at to requisitions (buyer ownership),
urgency/opportunity_value to requisitions (sales context for buyers).

Revision ID: 068
Revises: 067
Create Date: 2026-03-10
"""

import sqlalchemy as sa

from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- Requisition: buyer claim + sales context --
    op.add_column("requisitions", sa.Column("claimed_by_id", sa.Integer(), nullable=True))
    op.add_column("requisitions", sa.Column("claimed_at", sa.DateTime(), nullable=True))
    op.add_column("requisitions", sa.Column("urgency", sa.String(20), server_default="normal", nullable=True))
    op.add_column("requisitions", sa.Column("opportunity_value", sa.Numeric(12, 2), nullable=True))

    op.create_foreign_key(
        "fk_requisitions_claimed_by",
        "requisitions",
        "users",
        ["claimed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_requisitions_claimed_by", "requisitions", ["claimed_by_id"])
    op.create_index("ix_requisitions_urgency", "requisitions", ["urgency"])

    # -- Requirement: per-part sourcing status --
    op.add_column("requirements", sa.Column("sourcing_status", sa.String(20), server_default="open", nullable=True))
    op.create_index("ix_requirements_sourcing_status", "requirements", ["sourcing_status"])


def downgrade() -> None:
    op.drop_index("ix_requirements_sourcing_status", table_name="requirements")
    op.drop_column("requirements", "sourcing_status")

    op.drop_index("ix_requisitions_urgency", table_name="requisitions")
    op.drop_index("ix_requisitions_claimed_by", table_name="requisitions")
    op.drop_constraint("fk_requisitions_claimed_by", "requisitions", type_="foreignkey")
    op.drop_column("requisitions", "opportunity_value")
    op.drop_column("requisitions", "urgency")
    op.drop_column("requisitions", "claimed_at")
    op.drop_column("requisitions", "claimed_by_id")
