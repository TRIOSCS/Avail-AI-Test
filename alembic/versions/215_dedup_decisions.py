"""215_dedup_decisions — persist dedup dismissals + human merge/delete audit.

CREATE dedup_decisions (dismissed candidate pairs, canonical (min,max) ids,
uq_dedup_decision_pair on (entity_type, id_a, id_b), decided_by_id FK users SET
NULL) + dedup_merge_audit (append-only human merge/delete-both audit: actor FK
users SET NULL, kept/removed id+name denormalized at action time,
ix_dedup_merge_audit_created_at). No FKs to entity tables by design — three
tables share the seam; stale decision rows are pruned app-side.

Additive + reversible (downgrade drops both tables). Round-tripped on throwaway PG.
Chains onto 214_manufacturer_aliases_pending.

Revision ID: 215_dedup_decisions
Revises: 214_manufacturer_aliases_pending
"""

import sqlalchemy as sa

from alembic import op

revision = "215_dedup_decisions"
down_revision = "214_manufacturer_aliases_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dedup_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("id_a", sa.Integer(), nullable=False),
        sa.Column("id_b", sa.Integer(), nullable=False),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("entity_type", "id_a", "id_b", name="uq_dedup_decision_pair"),
    )
    op.create_table(
        "dedup_merge_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("kept_id", sa.Integer(), nullable=True),
        sa.Column("kept_name", sa.String(length=255), nullable=True),
        sa.Column("removed_id", sa.Integer(), nullable=False),
        sa.Column("removed_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Name must match SQLAlchemy's auto-name for Column(..., index=True) on the model
    # (ix_<table>_<column>), or the fresh-DB schema-drift gate flags a diff.
    op.create_index("ix_dedup_merge_audit_created_at", "dedup_merge_audit", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_dedup_merge_audit_created_at", table_name="dedup_merge_audit")
    op.drop_table("dedup_merge_audit")
    op.drop_table("dedup_decisions")
