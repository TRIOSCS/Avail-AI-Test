"""Offers overhaul: ChangeLog table, audit fields on Offer/Requisition, approval workflow.

Revision ID: 006_offers_overhaul
Revises: 005_ai_prompt
Create Date: 2026-02-23
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "006_offers_overhaul"
down_revision: Union[str, None] = "005_ai_prompt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create change_log table
    op.create_table(
        "change_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("field_name", sa.String(100), nullable=False),
        sa.Column("old_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
        sa.Column("created_at", sa.DateTime()),
        if_not_exists=True,
    )
    op.create_index("ix_changelog_entity", "change_log", ["entity_type", "entity_id"], if_not_exists=True)
    op.create_index("ix_changelog_user", "change_log", ["user_id"], if_not_exists=True)

    # Add audit fields to offers
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS updated_by_id INTEGER")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS approved_by_id INTEGER")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP WITHOUT TIME ZONE")

    # Add audit fields to requisitions
    op.execute("ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS updated_by_id INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE requisitions DROP COLUMN IF EXISTS updated_by_id")
    op.execute("ALTER TABLE requisitions DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS approved_at")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS approved_by_id")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS updated_by_id")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS updated_at")
    op.drop_index("ix_changelog_user", "change_log", if_exists=True)
    op.drop_index("ix_changelog_entity", "change_log", if_exists=True)
    op.drop_table("change_log", if_exists=True)
