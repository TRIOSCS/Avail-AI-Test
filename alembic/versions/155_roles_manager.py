"""Replace the legacy 'ops' user role with 'manager' + add per-user buy-plan approval
right.

Two changes, both reversible:

1. Data migration — UPDATE users SET role='manager' WHERE role='ops'. The UserRole enum
   has long since been renamed (ops -> manager); this sweeps any legacy DB rows still
   carrying the retired 'ops' value so the data matches the code's single source of truth.
   Downgrade reverses it (manager -> ops), which over-reaches by definition (it cannot
   tell migration-created managers from native ones), so it is a best-effort rollback of
   the data step only — acceptable because 'ops' is a dead value no live code reads.

2. Schema — add users.can_approve_buy_plans (Boolean NOT NULL server_default false): the
   per-user buy-plan approval right toggled in Users settings and enforced by
   dependencies.require_buyplan_approver. Downgrade drops the column.

Ordering: the column is added in upgrade BEFORE nothing depends on role; downgrade drops
the column first, then reverses the data sweep.

Revision ID: 155_roles_manager
Revises: 153_quote_graph_ids
Create Date: 2026-06-25
"""

import sqlalchemy as sa

from alembic import op

revision = "155_roles_manager"
down_revision = "153_quote_graph_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Sweep any legacy 'ops' role rows to 'manager' (enum renamed long ago).
    op.execute(sa.text("UPDATE users SET role = 'manager' WHERE role = 'ops'"))
    # 2. Per-user buy-plan approval right.
    op.add_column(
        "users",
        sa.Column(
            "can_approve_buy_plans",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "can_approve_buy_plans")
    # Best-effort reversal of the data sweep (cannot distinguish swept rows; see header).
    op.execute(sa.text("UPDATE users SET role = 'ops' WHERE role = 'manager'"))
