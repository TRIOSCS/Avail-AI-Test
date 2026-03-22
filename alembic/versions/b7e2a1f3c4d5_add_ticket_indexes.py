"""Add composite index on trouble_tickets and title index on root_cause_groups.

Revision ID: b7e2a1f3c4d5
Revises: 5c6736d6381f
Create Date: 2026-03-21 03:45:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b7e2a1f3c4d5"
down_revision: Union[str, None] = "5c6736d6381f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_trouble_tickets_source_status_created",
        "trouble_tickets",
        ["source", "status", "created_at"],
    )
    op.create_index(
        "ix_root_cause_groups_title",
        "root_cause_groups",
        ["title"],
    )


def downgrade() -> None:
    op.drop_index("ix_root_cause_groups_title", table_name="root_cause_groups")
    op.drop_index("ix_trouble_tickets_source_status_created", table_name="trouble_tickets")
