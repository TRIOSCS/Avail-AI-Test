"""Stop silent admin re-promotion: add users.admin_bootstrap_opted_out.

What (DDL, reversible):
  - ADD users.admin_bootstrap_opted_out (Boolean, NOT NULL, server_default false).
    Latches True when an ADMIN_EMAILS bootstrap admin is explicitly demoted via the
    admin Users tab (routers/admin/users.change_user_role); the login bootstrap in
    routers/auth.py then skips re-promotion while it is set, so a demoted admin stays
    demoted across logins. Cleared (set False) when an admin re-promotes them to admin.

Why: without this latch a user whose email is in ADMIN_EMAILS is silently re-promoted to
admin on their very next login, so an admin can never actually demote a bootstrap admin.
The server_default backfills every existing row to False during the NOT NULL add, so no
current user is affected.

Downgrade: fully reversible — drops the column.

Called by: alembic (upgrade/downgrade).
Depends on: users (exists since the initial schema).

Revision ID: 190_admin_bootstrap_optout
Revises: 189_category_residue_backfill
Create Date: 2026-07-16
"""

import sqlalchemy as sa

from alembic import op

revision = "190_admin_bootstrap_optout"
down_revision = "189_category_residue_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "admin_bootstrap_opted_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "admin_bootstrap_opted_out")
