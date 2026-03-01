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
    )
    op.create_index("ix_changelog_entity", "change_log", ["entity_type", "entity_id"])
    op.create_index("ix_changelog_user", "change_log", ["user_id"])

    # Add audit fields to offers
    op.add_column("offers", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("offers", sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("offers", sa.Column("approved_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("offers", sa.Column("approved_at", sa.DateTime(), nullable=True))

    # Add audit fields to requisitions
    op.add_column("requisitions", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("requisitions", sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("requisitions", "updated_by_id")
    op.drop_column("requisitions", "updated_at")
    op.drop_column("offers", "approved_at")
    op.drop_column("offers", "approved_by_id")
    op.drop_column("offers", "updated_by_id")
    op.drop_column("offers", "updated_at")
    op.drop_index("ix_changelog_user", "change_log")
    op.drop_index("ix_changelog_entity", "change_log")
    op.drop_table("change_log")
