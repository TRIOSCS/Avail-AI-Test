"""increase notification title to 500 chars

Revision ID: 050
Revises: 049
Create Date: 2026-03-03
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "050"
down_revision: Union[str, None] = "049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "notifications",
        "title",
        existing_type=sa.String(200),
        type_=sa.String(500),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "notifications",
        "title",
        existing_type=sa.String(500),
        type_=sa.String(200),
        existing_nullable=False,
    )
