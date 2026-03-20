"""Add requirement & offer fields + column picker prefs.

New columns:
- requirements.customer_pn (String 255) — customer's internal part number
- requirements.need_by_date (Date) — when customer needs the parts
- offers.spq (Integer) — standard pack quantity
- users.requirements_column_prefs (JSON) — visible column keys for requirements table
- users.offers_column_prefs (JSON) — visible column keys for offers table

Revision ID: a7b8c9d0e1f2
Revises: ba090b14bf74
Create Date: 2026-03-20 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "ba090b14bf74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("requirements", sa.Column("customer_pn", sa.String(255)))
    op.add_column("requirements", sa.Column("need_by_date", sa.Date()))
    op.add_column("offers", sa.Column("spq", sa.Integer()))
    op.add_column("users", sa.Column("requirements_column_prefs", sa.JSON()))
    op.add_column("users", sa.Column("offers_column_prefs", sa.JSON()))


def downgrade() -> None:
    op.drop_column("users", "offers_column_prefs")
    op.drop_column("users", "requirements_column_prefs")
    op.drop_column("offers", "spq")
    op.drop_column("requirements", "need_by_date")
    op.drop_column("requirements", "customer_pn")
