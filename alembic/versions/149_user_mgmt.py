"""User-management foundation: users.{last_login_at,access_overrides,invited_by_id}
+ user_admin_audit table (migration 149; renumbered 148→149 on collision).

Revision ID: 149_user_mgmt
Revises: 148_site_dnc
Create Date: 2026-06-24

Phase 1 (Foundation) of the user-management feature — purely additive:
- users.last_login_at (timestamptz, nullable)
- users.access_overrides (JSON, server_default '{}') — explicit per-user access
  overrides {access_key: bool}; absent key => role default
- users.invited_by_id (Integer FK users.id ON DELETE SET NULL, nullable)
- user_admin_audit table: append-only admin-action trail (actor SET NULL, target
  CASCADE) with indexes on target_user_id and created_at.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "149_user_mgmt"
down_revision = "148_site_dnc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("access_overrides", sa.JSON(), nullable=True, server_default=sa.text("'{}'")),
    )
    op.add_column("users", sa.Column("invited_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_users_invited_by",
        "users",
        "users",
        ["invited_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "user_admin_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("target_user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_user_admin_audit_actor", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["users.id"],
            name="fk_user_admin_audit_target",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_user_admin_audit_target_user_id", "user_admin_audit", ["target_user_id"])
    op.create_index("ix_user_admin_audit_created_at", "user_admin_audit", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_user_admin_audit_created_at", table_name="user_admin_audit")
    op.drop_index("ix_user_admin_audit_target_user_id", table_name="user_admin_audit")
    op.drop_table("user_admin_audit")

    op.drop_constraint("fk_users_invited_by", "users", type_="foreignkey")
    op.drop_column("users", "invited_by_id")
    op.drop_column("users", "access_overrides")
    op.drop_column("users", "last_login_at")
