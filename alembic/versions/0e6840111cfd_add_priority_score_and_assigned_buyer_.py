"""Add priority_score and assigned_buyer_id to requirements.

Revision ID: 0e6840111cfd
Revises: fa1b90a20cf4
Create Date: 2026-03-23 04:41:40.997652
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e6840111cfd"
down_revision: Union[str, None] = "fa1b90a20cf4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("requirements", sa.Column("priority_score", sa.Float(), nullable=True))
    op.add_column(
        "requirements",
        sa.Column(
            "assigned_buyer_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("requirements", "assigned_buyer_id")
    op.drop_column("requirements", "priority_score")
