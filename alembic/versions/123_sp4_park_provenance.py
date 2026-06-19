"""SP4 park provenance columns on prospect_accounts.

Adds swept_from_owner_id (FK users SET NULL), swept_at (UTCDateTime),
parked_by_id (FK users SET NULL) — all nullable.

Revision ID: 123_sp4_park_provenance
Revises: 122_prospect_ai_scores
Create Date: 2026-06-19
"""

import sqlalchemy as sa

from alembic import op

revision = "123_sp4_park_provenance"
down_revision = "122_prospect_ai_scores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prospect_accounts",
        sa.Column("swept_from_owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "prospect_accounts",
        sa.Column("swept_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "prospect_accounts",
        sa.Column("parked_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prospect_accounts", "parked_by_id")
    op.drop_column("prospect_accounts", "swept_at")
    op.drop_column("prospect_accounts", "swept_from_owner_id")
