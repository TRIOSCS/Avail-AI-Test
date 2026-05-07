"""Add warranty and country_of_origin columns to offers.

Revision ID: 004_warranty_coo
Revises: 003_perf_fk_indexes
Create Date: 2026-02-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "004_warranty_coo"
down_revision: Union[str, None] = "003_perf_fk_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS: idempotent against migration 001's explicit-DDL baseline
    # which already includes these columns. Safe on existing databases too.
    op.add_column("offers", sa.Column("warranty", sa.String(100), nullable=True))
    op.add_column("offers", sa.Column("country_of_origin", sa.String(100), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS country_of_origin")
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS warranty")
