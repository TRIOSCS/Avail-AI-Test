"""Add warranty and country_of_origin columns to offers.

Revision ID: 004_warranty_coo
Revises: 003_perf_fk_indexes
Create Date: 2026-02-21
"""

from typing import Sequence, Union

from alembic import op

revision: str = "004_warranty_coo"
down_revision: Union[str, None] = "003_perf_fk_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS: idempotent against migration 001's explicit-DDL baseline
    # which already includes these columns. Safe on existing databases too.
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS warranty VARCHAR(100)")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS country_of_origin VARCHAR(100)")


def downgrade() -> None:
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS country_of_origin")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS warranty")
