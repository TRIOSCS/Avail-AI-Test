"""Merge 049_pg_trgm and fb863358a701.

Revision ID: ba090b14bf74
Revises: 049_pg_trgm, fb863358a701
Create Date: 2026-03-20 20:31:19.730652
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "ba090b14bf74"
down_revision: Union[str, None] = ("049_pg_trgm", "fb863358a701")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
