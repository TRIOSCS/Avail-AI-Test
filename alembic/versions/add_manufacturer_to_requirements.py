"""Add manufacturer column to requirements table.

Revision ID: add_manufacturer_to_requirements
Revises: 34f9b46b4e0a
Create Date: 2026-03-22 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_manufacturer_to_requirements"
down_revision: Union[str, None] = "34f9b46b4e0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("requirements", sa.Column("manufacturer", sa.String(255), nullable=True))
    op.execute("UPDATE requirements SET manufacturer = COALESCE(brand, '') WHERE manufacturer IS NULL")
    op.alter_column("requirements", "manufacturer", nullable=False, server_default=sa.text("''"))
    op.create_index("ix_requirements_manufacturer", "requirements", ["manufacturer"])


def downgrade() -> None:
    op.drop_index("ix_requirements_manufacturer")
    op.drop_column("requirements", "manufacturer")
