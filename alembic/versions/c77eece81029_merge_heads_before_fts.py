"""merge_heads_before_fts.

Revision ID: c77eece81029
Revises: 083_crm_indexes, a1b2c3d4e5f6
Create Date: 2026-03-30 05:45:33.219089
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "c77eece81029"
down_revision: Union[str, None] = ("083_crm_indexes", "a1b2c3d4e5f6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
