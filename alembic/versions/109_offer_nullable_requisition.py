"""Make Offer.requisition_id nullable (keep CASCADE FK unchanged).

What:
  Relaxes the NOT NULL constraint on ``offers.requisition_id``.  The existing
  CASCADE foreign-key rule is left untouched.  This allows
  ``_auto_create_offers_from_parse`` to create Offer rows for unsolicited vendor
  emails (VendorResponse.requisition_id = NULL) so they can flow into the
  proactive-matching pipeline (run_proactive_scan only needs
  ``offer.material_card_id``).

  Existing rows are unaffected: they already have non-NULL requisition_id values.

Downgrade: restores NOT NULL.  Any NULL-requisition Offer rows will be deleted
  first (they cannot satisfy the re-added NOT NULL constraint).

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
    op.alter_column("offers", "requisition_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM offers WHERE requisition_id IS NULL")
    op.alter_column("offers", "requisition_id", existing_type=sa.Integer(), nullable=False)
