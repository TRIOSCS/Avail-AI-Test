"""Approvals workflow fold (Phase D): SO-fold + PO-rights backfills (data-only).

What: Two one-off DATA-ONLY backfills that must precede the Phase D code on deploy
      (deploy.sh runs ``alembic upgrade head`` before swapping the container):

  (a) R2 — SO fold. Phase D makes the single manager approval absorb SO verification:
      ``_run_approve_side_effects`` now stamps ``so_status='approved'`` at approval time
      and ``check_completion`` gates on ``so_status == 'approved'``. Any plan that was
      already ACTIVE/INBOUND with ``so_status='pending'`` under the OLD two-step flow has
      no surviving verify-SO route to clear it, so without this backfill it could never
      auto-complete. Stamp those in-flight plans approved (table is ``buy_plans_v3``).

  (b) R3 — PO-rights grandfather. Phase D moves verify-PO off ops verification-group
      membership onto the per-user ``users.can_approve_purchase_orders`` right. Grant the
      flag to every CURRENT active ops verifier so no one who could verify POs yesterday
      loses the ability the moment the code ships. ``IS DISTINCT FROM TRUE`` is NULL-safe
      and idempotent (re-running touches nothing already TRUE).

Downgrade: documented NO-OP. Both are irreversible backfills — we cannot know which
ACTIVE/INBOUND plans were originally ``pending`` (and re-pending them would re-break
auto-completion under the new gate), nor can we distinguish a migration-granted PO right
from one an admin set deliberately. Mirrors the no-op-downgrade contract of the other
data-only backfills (e.g. migration 100).

Called by: alembic (upgrade/downgrade).
Depends on: buy_plans_v3 (status/so_status/so_verified_at columns), users
            (can_approve_purchase_orders column, added by 166), verification_group_members.

Revision ID: 173_approvals_workflow_fold
Revises: 172_drop_dup_req_company_idx
Create Date: 2026-06-30
"""

from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "173_approvals_workflow_fold"
down_revision = "172_drop_dup_req_company_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # (a) R2 — fold SO verification into the approval for in-flight plans.
    so = conn.execute(
        text(
            "UPDATE buy_plans_v3 SET so_status = 'approved', so_verified_at = now() "
            "WHERE status IN ('active', 'inbound') AND so_status = 'pending'"
        )
    )
    logger.info("173: SO-fold backfill stamped {} in-flight buy plan(s) so_status=approved", so.rowcount or 0)

    # (b) R3 — grandfather current active ops verifiers into the PO-approval right.
    po = conn.execute(
        text(
            "UPDATE users SET can_approve_purchase_orders = TRUE "
            "WHERE id IN (SELECT user_id FROM verification_group_members WHERE is_active = TRUE) "
            "AND can_approve_purchase_orders IS DISTINCT FROM TRUE"
        )
    )
    logger.info("173: PO-rights backfill granted can_approve_purchase_orders to {} ops verifier(s)", po.rowcount or 0)


def downgrade() -> None:
    # Intentionally a NO-OP: both backfills are irreversible. We cannot recover which
    # ACTIVE/INBOUND plans were originally so_status='pending' (and re-pending them would
    # re-break auto-completion under the new check_completion gate), nor distinguish a
    # migration-granted PO right from one an admin set deliberately.
    logger.info("173: downgrade is a documented no-op (irreversible data backfills)")
