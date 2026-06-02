"""Add activity_digest table.

Caches AI-generated digests of entity activity timelines. One row per
(entity_type, entity_id); regenerated lazily when the timeline basis changes.
See app/models/intelligence.py (ActivityDigest) and
app/services/activity_digest_service.py.

Revision ID: 086_add_activity_digest
Revises: cf06dcdb7839
Create Date: 2026-06-02
"""

import sqlalchemy as sa

from alembic import op

revision = "086_add_activity_digest"
down_revision = "cf06dcdb7839"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_digest",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(length=300), nullable=True),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("highlights", sa.JSON(), nullable=True),
        sa.Column("next_step", sa.String(length=500), nullable=True),
        sa.Column("status_signal", sa.String(length=20), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("basis_last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("basis_activity_count", sa.Integer(), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_type", "entity_id", name="uq_activity_digest_entity"),
    )


def downgrade() -> None:
    op.drop_table("activity_digest")
