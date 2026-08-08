"""AI part-equivalence verdicts for proactive matching (2026-08-08).

What:
  - NEW part_equivalences — one row per (key_a, key_b) pair of normalized
    part keys with an AI or human verdict (same | different | uncertain),
    confidence and reasoning. Matching pools supply/demand across pairs whose
    verdict is 'same' (color-coded in the UI as an AI guess to double-check);
    'different' and 'uncertain' never pool. Human verdicts outrank AI.

Additive/reversible (downgrade drops the table). Indexes declared in the
model's __table_args__ with identical names so the fresh-DB drift gate stays
green.

Called by: alembic (upgrade/downgrade).
Depends on: users.

Revision ID: 206_part_equivalences
Revises: 205_proactive_digest_tracking
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "206_part_equivalences"
down_revision = "205_proactive_digest_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "part_equivalences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key_a", sa.String(255), nullable=False),
        sa.Column("key_b", sa.String(255), nullable=False),
        sa.Column("example_a", sa.String(255), nullable=True),
        sa.Column("example_b", sa.String(255), nullable=True),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="ai"),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_peq_pair", "part_equivalences", ["key_a", "key_b"], unique=True)
    op.create_index("ix_peq_key_b", "part_equivalences", ["key_b"])


def downgrade() -> None:
    op.drop_index("ix_peq_key_b", table_name="part_equivalences")
    op.drop_index("ix_peq_pair", table_name="part_equivalences")
    op.drop_table("part_equivalences")
