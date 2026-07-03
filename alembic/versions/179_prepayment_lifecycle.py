"""Prepayment: payment lifecycle (status + approved/paid/void columns) — migration 179.

What (DDL, reversible):
  - ADD prepayments.status (String(20) NOT NULL server_default 'requested') — the
    requested → approved → paid | void lifecycle source of truth. Indexed
    (ix_prepayment_status).
  - ADD the approve stamps: approved_by_id (Integer FK users.id ondelete SET NULL,
    nullable) + approved_at (UTCDateTime).
  - ADD pay_token (String(64), nullable) with a UNIQUE index (ix_prepayment_pay_token) —
    the single-use secrets.token_urlsafe(32) minted on approve and cleared on paid/void.
  - ADD the paid group: paid_at (UTCDateTime), paid_by_id (Integer FK users.id ondelete
    SET NULL, nullable — accounting has no User row), paid_by_label (String(120)),
    paid_via (String(20): accounting_email | in_app), wire_reference (String(120)),
    paid_amount (Numeric(12,2)).
  - ADD the void group: voided_at (UTCDateTime), voided_by_id (Integer FK users.id
    ondelete SET NULL, nullable), void_reason (String(255)).
  - BACKFILL status from each prepayment's PREPAYMENT ApprovalRequest (polymorphic
    subject_type='prepayment', subject_id=prepayment.id): approved → 'approved' (stamp
    approved_at from the request's resolved_at); rejected → 'void' (stamp voided_at from
    resolved_at, void_reason='rejected by approver'). Everything else stays 'requested'.
    NOTE: approval_requests has no resolver-user column (only resolved_at) under today's
    single-step ANY model, so approved_by_id / voided_by_id are left NULL on backfill.

Downgrade: fully reversible — drops the two indexes then the 13 columns.

Called by: alembic (upgrade/downgrade).
Depends on: prepayments (from 157_qp_approvals + 178_prepayment_line_link), approval_requests
            (backfill source), users (FK target).

Revision ID: 179_prepayment_lifecycle
Revises: 178_prepayment_line_link
Create Date: 2026-07-03
"""

import sqlalchemy as sa

import app.database
from alembic import op

revision = "179_prepayment_lifecycle"
down_revision = "178_prepayment_line_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── status lifecycle column (NOT NULL, backfilled 'requested' on existing rows)
    op.add_column(
        "prepayments",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="requested"),
    )
    op.create_index("ix_prepayment_status", "prepayments", ["status"])

    # ── approve stamps
    op.add_column(
        "prepayments",
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "prepayments",
        sa.Column("approved_at", app.database.UTCDateTime(), nullable=True),
    )

    # ── single-use pay_token (unique)
    op.add_column(
        "prepayments",
        sa.Column("pay_token", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_prepayment_pay_token", "prepayments", ["pay_token"], unique=True)

    # ── paid group
    op.add_column("prepayments", sa.Column("paid_at", app.database.UTCDateTime(), nullable=True))
    op.add_column("prepayments", sa.Column("paid_by_id", sa.Integer(), nullable=True))
    op.add_column("prepayments", sa.Column("paid_by_label", sa.String(length=120), nullable=True))
    op.add_column("prepayments", sa.Column("paid_via", sa.String(length=20), nullable=True))
    op.add_column("prepayments", sa.Column("wire_reference", sa.String(length=120), nullable=True))
    op.add_column("prepayments", sa.Column("paid_amount", sa.Numeric(precision=12, scale=2), nullable=True))

    # ── void group
    op.add_column("prepayments", sa.Column("voided_at", app.database.UTCDateTime(), nullable=True))
    op.add_column("prepayments", sa.Column("voided_by_id", sa.Integer(), nullable=True))
    op.add_column("prepayments", sa.Column("void_reason", sa.String(length=255), nullable=True))

    # ── FK constraints (ondelete SET NULL — audit rows outlive a deleted user)
    op.create_foreign_key(
        "fk_prepayment_approved_by",
        "prepayments",
        "users",
        ["approved_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_prepayment_paid_by",
        "prepayments",
        "users",
        ["paid_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_prepayment_voided_by",
        "prepayments",
        "users",
        ["voided_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── Backfill status from each prepayment's PREPAYMENT ApprovalRequest.
    conn = op.get_bind()
    # approved → approved (stamp approved_at from the request's resolved_at)
    conn.execute(
        sa.text(
            "UPDATE prepayments p SET status='approved', approved_at=ar.resolved_at "
            "FROM approval_requests ar "
            "WHERE ar.subject_type='prepayment' AND ar.subject_id=p.id AND ar.status='approved'"
        )
    )
    # rejected → void (stamp voided_at from resolved_at)
    conn.execute(
        sa.text(
            "UPDATE prepayments p SET status='void', voided_at=ar.resolved_at, "
            "void_reason='rejected by approver' "
            "FROM approval_requests ar "
            "WHERE ar.subject_type='prepayment' AND ar.subject_id=p.id AND ar.status='rejected'"
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_prepayment_voided_by", "prepayments", type_="foreignkey")
    op.drop_constraint("fk_prepayment_paid_by", "prepayments", type_="foreignkey")
    op.drop_constraint("fk_prepayment_approved_by", "prepayments", type_="foreignkey")

    op.drop_column("prepayments", "void_reason")
    op.drop_column("prepayments", "voided_by_id")
    op.drop_column("prepayments", "voided_at")

    op.drop_column("prepayments", "paid_amount")
    op.drop_column("prepayments", "wire_reference")
    op.drop_column("prepayments", "paid_via")
    op.drop_column("prepayments", "paid_by_label")
    op.drop_column("prepayments", "paid_by_id")
    op.drop_column("prepayments", "paid_at")

    op.drop_index("ix_prepayment_pay_token", table_name="prepayments")
    op.drop_column("prepayments", "pay_token")

    op.drop_column("prepayments", "approved_at")
    op.drop_column("prepayments", "approved_by_id")

    op.drop_index("ix_prepayment_status", table_name="prepayments")
    op.drop_column("prepayments", "status")
