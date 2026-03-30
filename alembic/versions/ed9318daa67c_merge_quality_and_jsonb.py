"""merge_quality_and_jsonb.

Revision ID: ed9318daa67c
Revises: 081_quality, 082
Create Date: 2026-03-30 00:34:36.173996
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "ed9318daa67c"
down_revision: Union[str, None] = ("081_quality", "082")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
