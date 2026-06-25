"""Add win_probability (Integer, nullable, 0-100) to requisitions (migration 146).

Revision ID: 146_req_win_probability
Revises: 145_vendor_parity_p1
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "146_req_win_probability"
down_revision = "145_vendor_parity_p1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "requisitions",
        sa.Column("win_probability", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("requisitions", "win_probability")
