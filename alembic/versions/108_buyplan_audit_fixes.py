"""Buy-plan audit fixes: drop dead token-approval columns, add line nudge tracking.

What:
  - Drops the orphaned token-approval surface on ``buy_plans_v3``: index
    ``ix_bpv3_token`` and columns ``approval_token`` + ``token_expires_at``. The external
    approve/reject endpoint was removed in the CRM redesign and nothing reads these.
  - Adds ``buy_plan_lines.last_nudge_at`` (UTC, nullable) + index ``ix_bpl_nudge_status``
    (status, last_nudge_at) backing the unconfirmed-instruction nudge job's idempotency.

Downgrade: re-adds the token columns + index and drops the nudge column + index (reversible).

Called by: alembic (upgrade/downgrade).
Depends on: buy_plans_v3, buy_plan_lines tables.

Revision ID: 108_buyplan_audit_fixes
Revises: bp_cph_recorded_at
Create Date: 2026-06-17
"""

import sqlalchemy as sa

from alembic import op

revision = "108_buyplan_audit_fixes"
# Re-chained onto bp_cph_recorded_at (#343 landed on main first; both originally chained
# off 107). Chain: 107 -> bp_cph_recorded_at -> 108. Single head; no column overlap.
down_revision = "bp_cph_recorded_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Drop dead token-approval surface ──────────────────────────────
    op.drop_index("ix_bpv3_token", table_name="buy_plans_v3")
    op.drop_column("buy_plans_v3", "approval_token")
    op.drop_column("buy_plans_v3", "token_expires_at")

    # ── Add nudge-tracking column + index ─────────────────────────────
    op.add_column(
        "buy_plan_lines",
        sa.Column("last_nudge_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_bpl_nudge_status",
        "buy_plan_lines",
        ["status", "last_nudge_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_bpl_nudge_status", table_name="buy_plan_lines")
    op.drop_column("buy_plan_lines", "last_nudge_at")

    op.add_column(
        "buy_plans_v3",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "buy_plans_v3",
        sa.Column("approval_token", sa.String(length=100), nullable=True),
    )
    # Non-unique, matching the original DDL (migrations 001/075 created it non-unique).
    op.create_index("ix_bpv3_token", "buy_plans_v3", ["approval_token"])
