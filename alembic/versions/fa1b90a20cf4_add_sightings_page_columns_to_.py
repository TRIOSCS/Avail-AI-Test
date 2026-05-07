"""Add sightings page columns to requirements and activity_log.

Revision ID: fa1b90a20cf4
Revises: add_ondelete_and_missing_indexes
Create Date: 2026-03-23 04:36:27.524692
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fa1b90a20cf4"
down_revision: Union[str, None] = "add_ondelete_and_missing_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add priority_score and assigned_buyer_id to requirements
    op.execute("ALTER TABLE requirements ADD COLUMN IF NOT EXISTS priority_score DOUBLE PRECISION")
    op.add_column(
        "requirements",
        sa.Column(
            "assigned_buyer_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Add requirement_id FK to activity_log
    op.add_column(
        "activity_log",
        sa.Column(
            "requirement_id",
            sa.Integer(),
            sa.ForeignKey("requirements.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Add index for activity_log.requirement_id
    op.create_index(
        "ix_activity_requirement",
        "activity_log",
        ["requirement_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("requirement_id IS NOT NULL"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_activity_requirement", table_name="activity_log", if_exists=True)
    op.execute("ALTER TABLE activity_log DROP COLUMN IF EXISTS requirement_id")
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS assigned_buyer_id")
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS priority_score")
