"""Add avatar_path column to users (migration 156).

Revision ID: 156_user_avatar
Revises: 153_quote_graph_ids
Create Date: 2026-06-25

Adds a nullable String(255) ``avatar_path`` column to users for the Profile-tab
photo uploader. Holds the stored basename of the uploaded image under
``avatars.AVATARS_DIR`` (e.g. "user_12_a1b2c3d4.png"); NULL falls back to the
initials avatar rendered by the shared ``user_avatar`` macro.

NOTE: parallel branches claimed 154/155 onto this same head — the integrator
re-chains this revision's down_revision at merge time per
MIGRATION_NUMBERS_IN_FLIGHT.txt; the claimed number 156 stays the same.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "156_user_avatar"
down_revision = "155_roles_manager"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("avatar_path", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "avatar_path")
