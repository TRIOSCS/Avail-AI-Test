"""Remove column picker preference fields from users table.

Column picker feature was abandoned in favor of showing all columns
with horizontal scrollbar. These fields are unused.

Revision ID: d4e7f2a19b83
Revises: 885342959628
Create Date: 2026-03-29 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d4e7f2a19b83"
down_revision: Union[str, None] = "885342959628"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check whether a column already exists (PostgreSQL)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.columns WHERE table_name = :table AND column_name = :column"),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if _column_exists("users", "parts_column_prefs"):
        op.drop_column("users", "parts_column_prefs")
    if _column_exists("users", "requirements_column_prefs"):
        op.drop_column("users", "requirements_column_prefs")
    if _column_exists("users", "offers_column_prefs"):
        op.drop_column("users", "offers_column_prefs")


def downgrade() -> None:
    op.add_column("users", sa.Column("parts_column_prefs", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("requirements_column_prefs", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("offers_column_prefs", sa.JSON(), nullable=True))
