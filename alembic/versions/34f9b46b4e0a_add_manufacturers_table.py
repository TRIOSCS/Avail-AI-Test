"""Add manufacturers table.

Revision ID: 34f9b46b4e0a
Revises: 9c7e1ed1db3e
Create Date: 2026-03-22 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "34f9b46b4e0a"
down_revision: Union[str, None] = "9c7e1ed1db3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "manufacturers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_index(op.f("ix_manufacturers_canonical_name"), "manufacturers", ["canonical_name"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_manufacturers_canonical_name"), table_name="manufacturers")
    op.drop_table("manufacturers")
