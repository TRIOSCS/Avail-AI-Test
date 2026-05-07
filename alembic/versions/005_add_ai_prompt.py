"""Add ai_prompt column to error_reports.

Revision ID: 005_ai_prompt
Revises: 004_warranty_coo
Create Date: 2026-02-21
"""

from typing import Sequence, Union

from alembic import op

revision: str = "005_ai_prompt"
down_revision: Union[str, None] = "004_warranty_coo"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE error_reports ADD COLUMN IF NOT EXISTS ai_prompt TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE error_reports DROP COLUMN IF EXISTS ai_prompt")
