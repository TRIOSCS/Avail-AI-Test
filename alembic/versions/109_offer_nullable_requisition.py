"""Make Offer.requisition_id nullable for unsolicited inbound vendor emails.

What:
  Drops the NOT NULL constraint on ``offers.requisition_id`` and switches the
  foreign-key DELETE rule from CASCADE to SET NULL.  This allows
  ``_auto_create_offers_from_parse`` to create Offer rows for unsolicited vendor
  emails (VendorResponse.requisition_id = NULL) so they can flow into the
  proactive-matching pipeline (run_proactive_scan only needs
  ``offer.material_card_id``).

  Existing rows are unaffected: they already have non-NULL requisition_id values.

Downgrade: restores NOT NULL + CASCADE.  Any NULL-requisition Offer rows will
  be deleted first (they cannot satisfy the re-added NOT NULL constraint).

Revision ID: 109_offer_nullable_requisition
Revises: 108_buyplan_audit_fixes
Create Date: 2026-06-18
"""

import sqlalchemy as sa

from alembic import op

revision = "109_offer_nullable_requisition"
down_revision = "108_buyplan_audit_fixes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("offers") as batch_op:
        # Drop the old FK (cascade delete) and NOT NULL, replace with SET NULL + nullable
        batch_op.alter_column(
            "requisition_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.drop_constraint("offers_requisition_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "offers_requisition_id_fkey",
            "requisitions",
            ["requisition_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    # Delete any null-requisition offers before re-adding NOT NULL
    op.execute("DELETE FROM offers WHERE requisition_id IS NULL")
    with op.batch_alter_table("offers") as batch_op:
        batch_op.drop_constraint("offers_requisition_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "offers_requisition_id_fkey",
            "requisitions",
            ["requisition_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.alter_column(
            "requisition_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
