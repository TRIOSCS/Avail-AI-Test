"""add_requirement_last_searched_at.

Revision ID: c4e8f2a71b03
Revises: 885342959628
Create Date: 2026-03-29 20:00:00.000000

Called by: alembic upgrade head
Depends on: requirements table, requisitions table
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8f2a71b03"
down_revision: Union[str, None] = "885342959628"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("requirements", sa.Column("last_searched_at", sa.DateTime(), nullable=True))
    # Backfill from parent requisition's last_searched_at
    op.execute("""
        UPDATE requirements
        SET last_searched_at = (
            SELECT last_searched_at FROM requisitions
            WHERE requisitions.id = requirements.requisition_id
        )
        WHERE last_searched_at IS NULL
    """)


def downgrade() -> None:
    op.drop_column("requirements", "last_searched_at")
