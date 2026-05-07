"""Add customer_pn, need_by_date, spq, and column prefs columns.

Idempotent: uses column existence checks since these columns may already
exist from a prior migration attempt.

Revision ID: 8aad37e73b45
Revises: c19a184db289
Create Date: 2026-03-20 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "8aad37e73b45"
down_revision: Union[str, None] = "c19a184db289"
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
    # Requirements: customer part number and need-by date
    if not _column_exists("requirements", "customer_pn"):
        op.execute("ALTER TABLE requirements ADD COLUMN IF NOT EXISTS customer_pn VARCHAR(255)")
    if not _column_exists("requirements", "need_by_date"):
        op.execute("ALTER TABLE requirements ADD COLUMN IF NOT EXISTS need_by_date DATE")

    # Offers: standard pack quantity
    if not _column_exists("offers", "spq"):
        op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS spq INTEGER")

    # Users: column visibility preferences for requirements and offers tables
    if not _column_exists("users", "requirements_column_prefs"):
        op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS requirements_column_prefs JSON")
    if not _column_exists("users", "offers_column_prefs"):
        op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS offers_column_prefs JSON")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS offers_column_prefs")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS requirements_column_prefs")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS spq")
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS need_by_date")
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS customer_pn")
