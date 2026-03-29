"""merge_heads_after_manual_search.

Revision ID: a2a095f252db
Revises: c4e8f2a71b03, d4e7f2a19b83
Create Date: 2026-03-29 22:27:21.609919
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "a2a095f252db"
down_revision: Union[str, None] = ("c4e8f2a71b03", "d4e7f2a19b83")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
