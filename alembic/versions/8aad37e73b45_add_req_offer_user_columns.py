"""Add customer_pn, need_by_date, spq, and column prefs columns.

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


def upgrade() -> None:
    # Requirements: customer part number and need-by date
    op.add_column("requirements", sa.Column("customer_pn", sa.String(255), nullable=True))
    op.add_column("requirements", sa.Column("need_by_date", sa.Date(), nullable=True))

    # Offers: standard pack quantity
    op.add_column("offers", sa.Column("spq", sa.Integer(), nullable=True))

    # Users: column visibility preferences for requirements and offers tables
    op.add_column("users", sa.Column("requirements_column_prefs", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("offers_column_prefs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "offers_column_prefs")
    op.drop_column("users", "requirements_column_prefs")
    op.drop_column("offers", "spq")
    op.drop_column("requirements", "need_by_date")
    op.drop_column("requirements", "customer_pn")
