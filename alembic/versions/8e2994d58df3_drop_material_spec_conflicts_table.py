"""Drop material_spec_conflicts table.

Revision ID: 8e2994d58df3
Revises: 81fc4ce30028
Create Date: 2026-03-19 20:38:57.319430
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e2994d58df3"
down_revision: Union[str, None] = "81fc4ce30028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("material_spec_conflicts")


def downgrade() -> None:
    op.create_table(
        "material_spec_conflicts",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("material_card_id", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("spec_key", sa.VARCHAR(length=100), autoincrement=False, nullable=False),
        sa.Column("existing_value", sa.VARCHAR(length=255), autoincrement=False, nullable=True),
        sa.Column("existing_source", sa.VARCHAR(length=50), autoincrement=False, nullable=True),
        sa.Column("existing_confidence", sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
        sa.Column("incoming_value", sa.VARCHAR(length=255), autoincrement=False, nullable=True),
        sa.Column("incoming_source", sa.VARCHAR(length=50), autoincrement=False, nullable=True),
        sa.Column("incoming_confidence", sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
        sa.Column("resolution", sa.VARCHAR(length=20), autoincrement=False, nullable=False),
        sa.Column("resolved_by", sa.VARCHAR(length=50), autoincrement=False, nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
        sa.ForeignKeyConstraint(
            ["material_card_id"],
            ["material_cards.id"],
            name=op.f("material_spec_conflicts_material_card_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("material_spec_conflicts_pkey")),
    )
