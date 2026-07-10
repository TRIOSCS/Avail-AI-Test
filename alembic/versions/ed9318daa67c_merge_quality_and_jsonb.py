"""merge_quality_and_jsonb.

Revision ID: ed9318daa67c
Revises: 081_quality, 082
Create Date: 2026-03-30 00:34:36.173996
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "ed9318daa67c"
down_revision: str | None = ("081_quality", "082")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
