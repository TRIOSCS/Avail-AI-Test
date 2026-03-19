"""Make proactive_match requirement_id requisition_id nullable.

Revision ID: c68ec71457b6
Revises: 29a41f5a248c
Create Date: 2026-03-19 22:58:53.263258
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c68ec71457b6"
down_revision: Union[str, None] = "29a41f5a248c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("proactive_matches", "requirement_id", existing_type=sa.INTEGER(), nullable=True)
    op.alter_column("proactive_matches", "requisition_id", existing_type=sa.INTEGER(), nullable=True)
    op.drop_constraint("proactive_matches_requisition_id_fkey", "proactive_matches", type_="foreignkey")
    op.drop_constraint("proactive_matches_requirement_id_fkey", "proactive_matches", type_="foreignkey")
    op.create_foreign_key(
        "proactive_matches_requirement_id_fkey",
        "proactive_matches",
        "requirements",
        ["requirement_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "proactive_matches_requisition_id_fkey",
        "proactive_matches",
        "requisitions",
        ["requisition_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("proactive_matches_requisition_id_fkey", "proactive_matches", type_="foreignkey")
    op.drop_constraint("proactive_matches_requirement_id_fkey", "proactive_matches", type_="foreignkey")
    op.create_foreign_key(
        "proactive_matches_requirement_id_fkey",
        "proactive_matches",
        "requirements",
        ["requirement_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "proactive_matches_requisition_id_fkey",
        "proactive_matches",
        "requisitions",
        ["requisition_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("proactive_matches", "requisition_id", existing_type=sa.INTEGER(), nullable=False)
    op.alter_column("proactive_matches", "requirement_id", existing_type=sa.INTEGER(), nullable=False)
