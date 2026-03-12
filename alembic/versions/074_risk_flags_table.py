"""Create risk_flags table for structured deal intelligence.

Tracks risk flags attached to buy plan lines and offers, replacing ad-hoc
JSON ai_flags with queryable, auditable records. Enables surfacing risk
signals during offer review.

Revision ID: 074
Revises: 073
Create Date: 2026-03-11
"""

import sqlalchemy as sa

from alembic import op

revision = "074"
down_revision = "073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "buy_plan_line_id", sa.Integer(), sa.ForeignKey("buy_plan_lines.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("source_offer_id", sa.Integer(), sa.ForeignKey("offers.id", ondelete="CASCADE"), nullable=True),
        sa.Column("requisition_id", sa.Integer(), sa.ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("source", sa.String(50), server_default="ai"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_risk_flags_line", "risk_flags", ["buy_plan_line_id"])
    op.create_index("ix_risk_flags_offer", "risk_flags", ["source_offer_id"])
    op.create_index("ix_risk_flags_req", "risk_flags", ["requisition_id"])
    op.create_index("ix_risk_flags_severity", "risk_flags", ["severity"])
    op.create_index("ix_risk_flags_type", "risk_flags", ["type"])


def downgrade() -> None:
    op.drop_index("ix_risk_flags_type", table_name="risk_flags")
    op.drop_index("ix_risk_flags_severity", table_name="risk_flags")
    op.drop_index("ix_risk_flags_req", table_name="risk_flags")
    op.drop_index("ix_risk_flags_offer", table_name="risk_flags")
    op.drop_index("ix_risk_flags_line", table_name="risk_flags")
    op.drop_table("risk_flags")
