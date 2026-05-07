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
    op.execute("ALTER TABLE requirements ADD COLUMN IF NOT EXISTS manufacturer VARCHAR(255)")
    op.execute("UPDATE requirements SET manufacturer = COALESCE(brand, '') WHERE manufacturer IS NULL")
    op.alter_column("requirements", "manufacturer", nullable=False, server_default=sa.text("''"))
    op.create_index("ix_requirements_manufacturer", "requirements", ["manufacturer"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_requirements_manufacturer", if_exists=True)
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS manufacturer")
