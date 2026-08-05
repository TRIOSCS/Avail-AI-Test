"""Buy-plan W3 lifecycle trim: retire BuyPlanStatus.INBOUND (data-only, no DDL).

Spec: SIMPLIFICATION_SPEC §5.2 / §10-W3 — "plan lifecycle collapses 7 statuses → 5
(drop INBOUND and the never-fired EXPIRED path); the Wave 3 migration ships a remap
table for existing rows." Recon reconciliation (owner-packet erratum): the spec's
"7→5" arithmetic double-counted — the EXPIRED half lived in ApprovalRequestStatus and
was ALREADY removed in W1.9 (zero rows, no expiry job ever built), and BuyPlanStatus
never contained EXPIRED. Only INBOUND dies here; HALTED is live code (the standalone
halt/resume safety off-ramp, 1 live row) and stays. BuyPlanStatus lands at 6 members,
not 5 — the plan-lifecycle scope still loses two statuses overall, one per enum.

DML remap table (buy_plans_v3.status is plain String(20), no check constraint — pure
data UPDATE on LOWER(TRIM(status))). Live row counts from availai-simp-db-1 (fresh
prod copy), 2026-08-04 (17 rows):

    | old (rows)    | new       | rationale                                        |
    |---------------|-----------|--------------------------------------------------|
    | draft (3)     | draft     | identity                                         |
    | pending (2)   | pending   | identity                                         |
    | active (9)    | active    | identity                                         |
    | inbound (0)   | active    | retired SP-3 holding state: migration 176 already|
    |               |           | remapped every row inbound->active and no writer |
    |               |           | remains — this UPDATE is belt-and-braces for     |
    |               |           | stragglers (the old startup completion sweep was |
    |               |           | deleted in the W2 backfill-graveyard cleanup;    |
    |               |           | buyplan_state.transition() is the only mover)    |
    | halted (1)    | halted    | KEPT — live standalone halt/resume               |
    | completed (1) | completed | identity                                         |
    | cancelled (1) | cancelled | identity                                         |

DB-shape note (verified during the round-trip, 2026-08-05): a CHAIN-BUILT PG carries
``ck_buy_plans_status`` (draft/pending/active/halted/completed/cancelled — 'inbound'
already excluded since the 176-era tightening), so there this UPDATE provably matches
zero rows; the LIVE database carries NO check constraint on buy_plans_v3.status
(verified on the availai-simp-db-1 prod copy, read-only), which is exactly why the
belt-and-braces UPDATE ships. Live status counts (2026-08-05 copy): active 4,
pending 4, draft 2, halted 1, completed 1, cancelled 1, inbound 0.

Reference cleanup riding this commit (non-workflow surfaces only):
  - app/dependencies.py PREPAYMENT_BLOCKED_PLAN_STATUSES (INBOUND removed)
  - app/routers/htmx/approvals_hub.py _LIVE_PLAN_STATUSES + the kanban gate
  - approvals/buy-plan templates (_workspace_list tones, _pane_sales_order gates,
    _detail_stepper branch)

Enum member removal: the W3 transition() consolidation (buyplan_state.py, same
branch) deleted BuyPlanStatus.INBOUND from app/constants.py and removed the
buyplan_lines.py _MANAGER_ONLY_EDIT_STATUSES guard reference alongside it.

Downgrade: many-to-one (an ex-inbound row is indistinguishable from active — exactly
as migration 176 already left prod) -> documented no-op, mirrors 176/193/205.

Called by: alembic (upgrade/downgrade).
Depends on: buy_plans_v3 (created in 106_buy_plan_v3 chain).

Revision ID: 208_buyplan_drop_inbound
Revises: 207_resell_status_ladder
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "208_buyplan_drop_inbound"
down_revision = "207_resell_status_ladder"
branch_labels = None
depends_on = None


def remap_statuses(connection) -> None:
    """The INBOUND retirement UPDATE (dialect-neutral; driven directly by tests)."""
    connection.execute(sa.text("UPDATE buy_plans_v3 SET status = 'active' WHERE LOWER(TRIM(status)) = 'inbound'"))


def upgrade() -> None:
    remap_statuses(op.get_bind())


def downgrade() -> None:
    # Many-to-one: an ex-inbound row cannot be told apart from a genuinely active one
    # (migration 176 already flattened prod this way) -> documented no-op.
    pass
