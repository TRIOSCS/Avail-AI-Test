"""Backfill pre-engine PENDING buy plans into the approval engine (data-only).

Owner decision D2 (Packet 3, resolved 2026-08-07): BACKFILL. A handful of buy
plans went PENDING before the approval engine owned the BUY_PLAN gate (QP
Phase C1), so they have no ApprovalRequest — the last pre-engine fallback (the
legacy ``approve_buy_plan`` dispatch in the approve router) existed only for
them. This migration stamps them into the engine so that fallback dies in the
same change. (2 such plans — ids 2 and 11 — on the simp prod copy 2026-08-07,
read-only check; Packet 3's earlier count of 4 predates the 08-06 DB refresh.)

What (DML only, no schema change) — for every buy_plans_v3 row WHERE
status='pending' AND no OPEN (status='requested') BUY_PLAN-subject
approval_requests row exists (the exact predicate the router fallback fired
on), create the same rows the modern submit path
(buyplan_approval._open_engine_request_for_plan → approvals.service.
create_request → routing.route_request) would have created:

  - approval_requests: gate_type='buy_plan', status='requested',
    amount=plan.total_cost, currency='USD',
    requested_by_id = owner_id = plan.submitted_by_id,
    subject_type='buy_plan', subject_id=plan.id,
    created_at = COALESCE(plan.submitted_at, now) — the queue shows true age.
  - approval_steps: one row — seq=1, rule='any', status='pending'.
  - approval_step_recipients: one 'pending' row per eligible approver
    COMPUTED AT MIGRATION TIME: active users with can_approve_buy_plans
    (the same per-user toggle routing.route_request reads; any-of step,
    first responder wins).
  - approval_events: the genesis 'submitted' audit row,
    actor_id = plan.submitted_by_id, payload tags the backfill.

All enum values are frozen string literals (house style: a migration must not
drift with app constants). No ActivityLog summary row is written: the engine's
own append-only ApprovalEvent is the audit; log_activity's timeline row is an
app-layer convenience that would land dated today for months-old submissions.

Idempotent + count-tolerant: the NOT-EXISTS guard makes a re-run a no-op, and
the owner wipes all non-customer data before group testing, so future DBs may
legitimately yield 0 rows. A plan with NO eligible approver configured is
skipped with a warning — exactly what the modern submit does
(NoEligibleApproverError → plan stays PENDING with no engine state; surfaced
in the UI by plan_needs_approver_reason).

Downgrade: documented no-op — data-only; the backfilled rows are
indistinguishable in effect from organically-submitted ones, and deleting them
after a decision landed would orphan that decision's audit trail.

Called by: alembic (upgrade/downgrade).
Depends on: buy_plans_v3, approval_requests/steps/step_recipients/events
            (engine tables), users.can_approve_buy_plans / is_active.

Revision ID: 210_backfill_pre_engine_bp_reqs
Revises: 209_req_status_derived
Create Date: 2026-08-07
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from loguru import logger

from alembic import op

revision = "210_backfill_pre_engine_bp_reqs"
down_revision = "209_req_status_derived"
branch_labels = None
depends_on = None


def backfill_pre_engine_plans(bind) -> tuple[int, int]:
    """Stamp every engine-less PENDING buy plan into the approval engine.

    Factored out of upgrade() (mirrors 193's remap_legacy_statuses) so a test can drive
    the identical code against a seeded session.

    Returns (stamped_count, skipped_no_approver_count).
    """
    now = datetime.now(UTC)

    # Eligibility AT MIGRATION TIME — the same per-user toggle
    # routing._eligible_approvers reads for the buy_plan gate (no amount limit).
    approver_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT id FROM users WHERE is_active IS TRUE AND can_approve_buy_plans IS TRUE ORDER BY id")
        )
    ]

    # The exact engine-less predicate the deleted router fallback fired on:
    # PENDING plan with no OPEN (requested) BUY_PLAN-subject request.
    plans = bind.execute(
        sa.text(
            """
            SELECT bp.id, bp.total_cost, bp.submitted_by_id, bp.submitted_at
            FROM buy_plans_v3 bp
            WHERE bp.status = 'pending'
              AND NOT EXISTS (
                SELECT 1 FROM approval_requests ar
                WHERE ar.subject_type = 'buy_plan'
                  AND ar.subject_id = bp.id
                  AND ar.status = 'requested'
              )
            ORDER BY bp.id
            """
        )
    ).fetchall()

    if plans and not approver_ids:
        # Mirrors the modern submit's NoEligibleApproverError: leave the plans
        # PENDING with no engine state (the UI surfaces the stall).
        logger.warning(
            "210 backfill: {} engine-less PENDING buy plan(s) found but NO active "
            "user holds can_approve_buy_plans — nothing stamped (plans stay "
            "PENDING; grant the right and re-run, the migration is idempotent)",
            len(plans),
        )
        return 0, len(plans)

    stamped = 0
    for plan_id, total_cost, submitted_by_id, submitted_at in plans:
        request_id = bind.execute(
            sa.text(
                "INSERT INTO approval_requests "
                "(gate_type, status, amount, currency, requested_by_id, owner_id, "
                " subject_type, subject_id, created_at) "
                "VALUES ('buy_plan', 'requested', :amount, 'USD', :uid, :uid, "
                "        'buy_plan', :plan_id, :created_at) "
                "RETURNING id"
            ),
            {
                "amount": total_cost,
                "uid": submitted_by_id,
                "plan_id": plan_id,
                "created_at": submitted_at or now,
            },
        ).scalar_one()

        step_id = bind.execute(
            sa.text(
                "INSERT INTO approval_steps (request_id, seq, rule, status, created_at) "
                "VALUES (:rid, 1, 'any', 'pending', :now) RETURNING id"
            ),
            {"rid": request_id, "now": now},
        ).scalar_one()

        for approver_id in approver_ids:
            bind.execute(
                sa.text(
                    "INSERT INTO approval_step_recipients (step_id, user_id, status, created_at) "
                    "VALUES (:sid, :uid, 'pending', :now)"
                ),
                {"sid": step_id, "uid": approver_id, "now": now},
            )

        bind.execute(
            sa.text(
                "INSERT INTO approval_events (request_id, actor_id, event_type, payload, created_at) "
                "VALUES (:rid, :actor, 'submitted', :payload, :now)"
            ),
            {
                "rid": request_id,
                "actor": submitted_by_id,
                "payload": json.dumps({"backfill": "210_backfill_pre_engine_bp_reqs", "decision": "packet3-D2"}),
                "now": now,
            },
        )
        stamped += 1

    logger.info(
        "210 backfill: stamped {} pre-engine PENDING buy plan(s) into the approval "
        "engine ({} eligible approver(s) per any-of step)",
        stamped,
        len(approver_ids),
    )
    return stamped, 0


def upgrade() -> None:
    backfill_pre_engine_plans(op.get_bind())


def downgrade() -> None:
    # Data-only backfill; documented no-op. The stamped rows are
    # indistinguishable in effect from organically-submitted requests, and
    # deleting them after a decision landed would orphan that decision's
    # append-only audit trail (mirrors 093/100/189/193/204/205/206).
    pass
