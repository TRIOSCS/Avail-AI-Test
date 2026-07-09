"""merge_heads_after_manual_search.

Revision ID: a2a095f252db
Revises: c4e8f2a71b03, d4e7f2a19b83
Create Date: 2026-03-29 22:27:21.609919
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a2a095f252db"
down_revision: str | None = ("c4e8f2a71b03", "d4e7f2a19b83")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
