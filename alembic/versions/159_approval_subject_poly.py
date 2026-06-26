"""Approvals engine foundation: polymorphic subject, channel default, drop note.

Cleans the just-shipped approvals engine before QP Phase C builds on it (covers
changes 1, 2, 7 of the engine-cleanup brief):

  1. approval_requests: replace the two nullable subject FK columns
     (subject_quality_plan_id, subject_prepayment_id) + their indexes with a
     polymorphic (subject_type, subject_id) pair (plain columns, NO cross-table FK —
     mirrors MaterialCardAudit.material_card_id) + one composite index
     ix_approval_req_subject. Backfilled from whichever old FK was non-null.
  2. approval_outbox.channel: flip server_default from 'email' to 'in_app' so the
     channel is never implicit-and-wrong (the decide() path now enqueues both).
  7. approval_events: drop the dead `note` column (payload is the comment sink).

Additive/reversible: the downgrade reconstructs the FK columns + indexes (backfilled
from subject_type/subject_id), reverts the channel default to 'email', and re-adds
`note`. Approval tables are near-empty on staging so the backfill is trivial.

Called by: alembic. Depends on: 158_req_pipeline_hotlist.

Revision ID: 159_approval_subject_poly
Revises: 158_req_pipeline_hotlist
Create Date: 2026-06-26
"""

import sqlalchemy as sa

from alembic import op

revision = "159_approval_subject_poly"
down_revision = "158_req_pipeline_hotlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. Polymorphic subject on approval_requests ────────────────────────────
    op.add_column("approval_requests", sa.Column("subject_type", sa.String(50), nullable=True))
    op.add_column("approval_requests", sa.Column("subject_id", sa.Integer(), nullable=True))

    # Backfill from whichever old FK was non-null (quality plan takes precedence only
    # if both were somehow set, which the create path never does).
    bind.execute(
        sa.text(
            "UPDATE approval_requests "
            "SET subject_type = 'prepayment', subject_id = subject_prepayment_id "
            "WHERE subject_prepayment_id IS NOT NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE approval_requests "
            "SET subject_type = 'quality_plan', subject_id = subject_quality_plan_id "
            "WHERE subject_quality_plan_id IS NOT NULL"
        )
    )

    op.create_index(
        "ix_approval_req_subject",
        "approval_requests",
        ["subject_type", "subject_id"],
    )

    # Drop the old FK columns + their indexes (the FK constraints drop with the columns).
    op.drop_index("ix_approval_req_subject_pp", table_name="approval_requests")
    op.drop_index("ix_approval_req_subject_qp", table_name="approval_requests")
    op.drop_column("approval_requests", "subject_prepayment_id")
    op.drop_column("approval_requests", "subject_quality_plan_id")

    # ── 2. approval_outbox.channel default 'email' → 'in_app' ──────────────────
    op.alter_column(
        "approval_outbox",
        "channel",
        existing_type=sa.String(50),
        existing_nullable=False,
        server_default="in_app",
    )

    # ── 7. Drop the dead approval_events.note column ───────────────────────────
    op.drop_column("approval_events", "note")


def downgrade() -> None:
    bind = op.get_bind()

    # ── 7. Re-add approval_events.note ─────────────────────────────────────────
    op.add_column("approval_events", sa.Column("note", sa.Text(), nullable=True))

    # ── 2. Revert approval_outbox.channel default to 'email' ───────────────────
    op.alter_column(
        "approval_outbox",
        "channel",
        existing_type=sa.String(50),
        existing_nullable=False,
        server_default="email",
    )

    # ── 1. Reconstruct the subject FK columns + indexes ────────────────────────
    op.add_column(
        "approval_requests",
        sa.Column(
            "subject_quality_plan_id",
            sa.Integer(),
            sa.ForeignKey("quality_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "approval_requests",
        sa.Column(
            "subject_prepayment_id",
            sa.Integer(),
            sa.ForeignKey("prepayments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Backfill the FK columns from the polymorphic pair. Only restore subject_ids that
    # still exist in the target table — the polymorphic column carries no FK, so a
    # subject may have been deleted out from under it; restoring an orphan id would
    # violate the reconstructed FK. Such rows downgrade to a NULL subject FK (lossless
    # for the engine, which keys off subject_type/subject_id, not the bridge FKs).
    bind.execute(
        sa.text(
            "UPDATE approval_requests SET subject_prepayment_id = subject_id "
            "WHERE subject_type = 'prepayment' "
            "AND EXISTS (SELECT 1 FROM prepayments p WHERE p.id = approval_requests.subject_id)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE approval_requests SET subject_quality_plan_id = subject_id "
            "WHERE subject_type = 'quality_plan' "
            "AND EXISTS (SELECT 1 FROM quality_plans q WHERE q.id = approval_requests.subject_id)"
        )
    )

    op.create_index("ix_approval_req_subject_qp", "approval_requests", ["subject_quality_plan_id"])
    op.create_index("ix_approval_req_subject_pp", "approval_requests", ["subject_prepayment_id"])

    # Drop the polymorphic pair + its composite index.
    op.drop_index("ix_approval_req_subject", table_name="approval_requests")
    op.drop_column("approval_requests", "subject_id")
    op.drop_column("approval_requests", "subject_type")
