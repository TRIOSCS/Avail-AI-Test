"""Add temporal-policy + provenance columns to vendor_part_unavailability.

Four nullable columns backing the adopted "Two Windows, Real Proof" policy
(docs/superpowers/specs/2026-06-10-unavailability-temporal-policy.md) and the
silent-failure hardening (design spec IMPORTANT-3):

- qty_at_mark: per-key qty snapshot at mark/re-mark — powers the O2 restock
  override. NO backfill: legacy records keep NULL, so O2 never fires for them
  and they ride the suppression windows (fail-closed).
- released_at / release_trigger: written ONLY by override O3 (buyer-routed
  vendor email → 'vendor_email') and the offer hook ('offer_received');
  non-NULL ⇒ the record is no longer active.
- requirement_id: clear-time provenance FK (SET NULL — knowledge outlives
  requirements), indexed for the clear_unavailability provenance arm.

Generated via `alembic revision --autogenerate` against a scratch PG at 097,
then hand-reviewed: unrelated autogen noise stripped (runtime trgm/FTS indexes,
dead-table drops — none of it belongs to this change). released_at uses
sa.DateTime(timezone=True) to match UTCDateTime's dialect impl (TIMESTAMP WITH
TIME ZONE) so future autogenerate runs see no type diff; the FK is explicitly
named so downgrade can drop it deterministically.

Downgrade drops the four columns (and the index/FK).

Revision ID: 098_unavail_policy_columns
Revises: 097_vendor_part_unavailability
Create Date: 2026-06-10
"""

import sqlalchemy as sa

from alembic import op

revision = "098_unavail_policy_columns"
down_revision = "097_vendor_part_unavailability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vendor_part_unavailability", sa.Column("qty_at_mark", sa.Integer(), nullable=True))
    op.add_column("vendor_part_unavailability", sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vendor_part_unavailability", sa.Column("release_trigger", sa.String(length=32), nullable=True))
    op.add_column("vendor_part_unavailability", sa.Column("requirement_id", sa.Integer(), nullable=True))
    op.create_index("ix_vendor_part_unavail_req", "vendor_part_unavailability", ["requirement_id"], unique=False)
    op.create_foreign_key(
        "fk_vendor_part_unavail_requirement",
        "vendor_part_unavailability",
        "requirements",
        ["requirement_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_vendor_part_unavail_requirement", "vendor_part_unavailability", type_="foreignkey")
    op.drop_index("ix_vendor_part_unavail_req", table_name="vendor_part_unavailability")
    op.drop_column("vendor_part_unavailability", "requirement_id")
    op.drop_column("vendor_part_unavailability", "release_trigger")
    op.drop_column("vendor_part_unavailability", "released_at")
    op.drop_column("vendor_part_unavailability", "qty_at_mark")
