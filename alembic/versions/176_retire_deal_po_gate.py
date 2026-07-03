"""Retire the deal-level PURCHASE_ORDER approval gate (Phase-3 stage-3, data-only).

What: Two one-off DATA-ONLY updates that must precede the stage-3 code on deploy
      (deploy.sh runs ``alembic upgrade head`` before swapping the container):

  (a) Cancel stale deal-level PO approval requests. Stage 3 removes the deal-level
      PURCHASE_ORDER gate (per-PO PENDING_VERIFY sign-off replaces it), so any
      still-``requested`` approval_requests row with ``gate_type='purchase_order'``
      AND ``subject_type='buy_plan'`` has no surviving approve/reject route — without
      this update it would sit in approvers' queues forever. Stamp those rows
      ``cancelled`` with ``resolved_at=now()`` and an explanatory resolution_note.
      QP-purchasing requests are untouched (166 already moved them to
      ``gate_type='qp_purchasing'``), as are resolved deal-level rows (history keeps
      the ``purchase_order`` value per constants.py).

  (b) Release gated buy plans. Plans parked in ``status='inbound'`` (the SP-3
      deal-level-PO-approved holding state) flow back to ``'active'`` so the new
      per-PO flow picks them up; no other status is touched.

Downgrade: documented NO-OP. Both updates are irreversible — we cannot know which
cancelled requests were originally ``requested`` (others were cancelled by users),
nor which active plans were previously ``inbound``. Mirrors the no-op-downgrade
contract of the other data-only backfills (e.g. migrations 100 and 173).

Called by: alembic (upgrade/downgrade).
Depends on: approval_requests (gate_type/subject_type/status/resolved_at/
            resolution_note columns, 157+159), buy_plans_v3 (status column).

Revision ID: 176_retire_deal_po_gate
Revises: 175_add_quote_requisitions
Create Date: 2026-07-03
"""

from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "176_retire_deal_po_gate"
down_revision = "175_add_quote_requisitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # (a) Cancel stale deal-level PO approval requests (no surviving resolve route).
    reqs = conn.execute(
        text(
            "UPDATE approval_requests SET status = 'cancelled', resolved_at = now(), "
            "resolution_note = 'retired: deal-level PO gate removed 2026-07 — "
            "see per-PO PENDING_VERIFY sign-off instead' "
            "WHERE gate_type = 'purchase_order' AND subject_type = 'buy_plan' "
            "AND status = 'requested'"
        )
    )
    logger.info("176: cancelled {} stale deal-level PO approval request(s)", reqs.rowcount or 0)

    # (b) Release plans parked in the retired 'inbound' holding state back to 'active'.
    plans = conn.execute(text("UPDATE buy_plans_v3 SET status = 'active' WHERE status = 'inbound'"))
    logger.info("176: released {} inbound buy plan(s) back to active", plans.rowcount or 0)


def downgrade() -> None:
    # Intentionally a NO-OP: both updates are irreversible. We cannot recover which
    # cancelled requests were originally 'requested' (vs user-cancelled) nor which
    # active plans were previously 'inbound' — re-pending either would resurrect a
    # gate the stage-3 code no longer serves.
    logger.info("176: downgrade is a documented no-op (irreversible data updates)")
