"""Per-rep manager: add users.reports_to_id (self-FK).

What (DDL, reversible):
  - ADD users.reports_to_id (Integer, NULLABLE) — the rep's manager / supervisor.
    Self-referential FK to users.id (fk_users_reports_to) with ondelete=SET NULL so
    deleting a manager detaches their reports rather than cascading.

Powers per-rep manager routing: when a rep has reports_to_id set, the account-park
(auto-sweep) alert targets THAT specific manager instead of fanning out to every
active MANAGER/ADMIN user; when unset the all-managers fallback is preserved so
nothing regresses. Distinct from users.invited_by_id (the other users.id self-FK).

Downgrade: fully reversible — drops the FK then the column.

Called by: alembic (upgrade/downgrade).
Depends on: users (exists since the initial schema).

Revision ID: 184_user_reports_to
Revises: 183_customer_bid_lifecycle
Create Date: 2026-07-04
"""

import sqlalchemy as sa

from alembic import op

revision = "184_user_reports_to"
down_revision = "183_customer_bid_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("reports_to_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_users_reports_to",
        "users",
        "users",
        ["reports_to_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_reports_to", "users", type_="foreignkey")
    op.drop_column("users", "reports_to_id")
