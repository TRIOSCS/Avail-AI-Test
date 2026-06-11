"""Add vendor_part_unavailability table — durable vendor+part "stock is gone" knowledge.

One row per (vendor_name_normalized, normalized_mpn) pair recording why the part is
unavailable from that vendor (UnavailabilityReason value), an optional note, and
provenance (created_by_id FK users SET NULL, created_at server default now). The
unique constraint on the pair backs the upsert semantics in
app/services/vendor_unavailability.py; the two single-column indexes serve the
vendor-row intel lookup and the RFQ-suggestion exclusion query respectively.

Generated via `alembic revision --autogenerate` against a scratch PG at 096 (renumbered 097->101 after the concurrent 097-100 chain landed on main), then
hand-reviewed: unrelated autogen noise stripped (runtime trgm/FTS indexes, dead-table
drops — none of it belongs to this change), table DDL kept verbatim except created_at,
which uses sa.DateTime(timezone=True) to match UTCDateTime's dialect impl
(TIMESTAMP WITH TIME ZONE) so future autogenerate runs see no type diff.

Downgrade drops the table (the activity timeline keeps the human-readable history).

Revision ID: 101_vendor_part_unavailability
Revises: 100_taxonomy_alias_backfill
Create Date: 2026-06-10
"""

import sqlalchemy as sa

from alembic import op

revision = "101_vendor_part_unavailability"
down_revision = "100_taxonomy_alias_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_part_unavailability",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_name_normalized", sa.String(length=255), nullable=False),
        sa.Column("normalized_mpn", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vendor_name_normalized", "normalized_mpn", name="uq_vendor_part_unavail_vendor_mpn"),
    )
    op.create_index(
        "ix_vendor_part_unavail_vendor", "vendor_part_unavailability", ["vendor_name_normalized"], unique=False
    )
    op.create_index("ix_vendor_part_unavail_mpn", "vendor_part_unavailability", ["normalized_mpn"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_vendor_part_unavail_mpn", table_name="vendor_part_unavailability")
    op.drop_index("ix_vendor_part_unavail_vendor", table_name="vendor_part_unavailability")
    op.drop_table("vendor_part_unavailability")
