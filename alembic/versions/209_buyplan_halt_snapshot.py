"""209_buyplan_halt_snapshot — remember a buy plan's pre-halt state so resume restores
it.

QC 2026-08-10 P2/D2 (process-review dead-end): halting a buy plan overwrote
``status`` (-> HALTED) and ``so_status`` (-> REJECTED) with no record of the
prior values, and resume forced ``status`` back to ACTIVE without touching
``so_status``. Result: a resumed plan sat at so_status=REJECTED forever, so
``check_completion`` (which requires so_status=APPROVED) could never fire — the
plan could never complete. And a PENDING-halted plan came back ACTIVE with its
manager approval never granted.

Adds two nullable snapshot columns; halt fills them, resume restores + clears
them. Additive + reversible (downgrade drops both). Round-tripped on throwaway
PG. Chains onto 208_quote_delete_fk_guard.

Revision ID: 209_buyplan_halt_snapshot
Revises: 208_quote_delete_fk_guard
"""

import sqlalchemy as sa

from alembic import op

revision = "209_buyplan_halt_snapshot"
down_revision = "208_quote_delete_fk_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("buy_plans_v3", sa.Column("status_before_halt", sa.String(length=30), nullable=True))
    op.add_column("buy_plans_v3", sa.Column("so_status_before_halt", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("buy_plans_v3", "so_status_before_halt")
    op.drop_column("buy_plans_v3", "status_before_halt")
