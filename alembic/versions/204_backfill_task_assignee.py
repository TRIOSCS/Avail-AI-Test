"""Backfill requisition_tasks.assigned_to_id from created_by (data-only).

What (DML only, no schema change):
  - UPDATE requisition_tasks SET assigned_to_id = created_by
    WHERE assigned_to_id IS NULL AND created_by IS NOT NULL

Why (2026-08-03 task-assignee-authz spec R1): the app layer now requires an
assignee on every task create surface, and the uniform mutation gate admits
creator | assignee | admin | account-owner. Existing NULL-assignee rows are
backfilled to their creator so they surface on that user's My Day; rows whose
creator is also NULL stay as-is (nothing to infer — admin-visible). The column
stays NULLABLE on purpose: assigned_to_id is FK ondelete=SET NULL, so user
deletion must keep working.

Downgrade: documented no-op — the pre-backfill NULL set is not recoverable.

Called by: alembic (upgrade/downgrade).
Depends on: requisition_tasks (created in 065_requisition_tasks).

Revision ID: 204_backfill_task_assignee
Revises: 203_outreach_recipient_email
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "204_backfill_task_assignee"
down_revision = "203_outreach_recipient_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE requisition_tasks SET assigned_to_id = created_by "
        "WHERE assigned_to_id IS NULL AND created_by IS NOT NULL"
    )


def downgrade() -> None:
    # Data-only backfill; the pre-backfill NULL set is not recoverable. No-op.
    pass
