"""Collapse requisition_tasks.status to the two-state model (data-only).

What (DML only, no schema change — status is a plain String(20), no CHECK
constraint, so no DDL is needed):
  - UPDATE requisition_tasks SET status = 'open'
    WHERE status IN ('todo', 'in_progress')

Why (simplification spec §5.4, v1.1 §0.6 — W1.13): TaskStatus trims from
todo/in_progress/done to open/done. 'done' rows are untouched; both open-ish
ladder states map to 'open'.

Downgrade: maps 'open' → 'todo' (the pre-collapse default/entry state). The
todo-vs-in_progress distinction is not recoverable (many-to-one remap), so a
downgraded row lands on 'todo' — documented, mirrors 093/100/189/193.

Called by: alembic (upgrade/downgrade).
Depends on: requisition_tasks (created in 065_requisition_tasks).

Revision ID: 205_task_status_two_state
Revises: 204_backfill_task_assignee
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "205_task_status_two_state"
down_revision = "204_backfill_task_assignee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE requisition_tasks SET status = 'open' WHERE status IN ('todo', 'in_progress')")


def downgrade() -> None:
    # Many-to-one remap: todo-vs-in_progress is not recoverable; both restore as
    # 'todo' (the pre-collapse default/entry state).
    op.execute("UPDATE requisition_tasks SET status = 'todo' WHERE status = 'open'")
