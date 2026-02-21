"""Add ai_prompt column to error_reports

Revision ID: 005_ai_prompt
Revises: 004_warranty_coo
Create Date: 2026-02-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_ai_prompt"
down_revision: Union[str, None] = "004_warranty_coo"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("error_reports", sa.Column("ai_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("error_reports", "ai_prompt")
