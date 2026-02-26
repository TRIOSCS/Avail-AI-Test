"""Add vendor_name_normalized to material_vendor_history, offers, and contacts.

Phase 4 of Material Card Linkage: vendor name normalization for index-friendly lookups.
Replaces LOWER(TRIM(vendor_name)) patterns in queries.
Idempotent: columns/indexes may already exist from startup _add_missing_columns.

Revision ID: 012_vendor_name_normalized
Revises: 011_phase3_integrity
Create Date: 2026-02-25
"""

from alembic import op
from sqlalchemy import text

revision = "012_vendor_name_normalized"
down_revision = "011_phase3_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Add nullable columns (idempotent)
    conn.execute(text("ALTER TABLE material_vendor_history ADD COLUMN IF NOT EXISTS vendor_name_normalized VARCHAR(255)"))
    conn.execute(text("ALTER TABLE offers ADD COLUMN IF NOT EXISTS vendor_name_normalized VARCHAR(255)"))
    conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS vendor_name_normalized VARCHAR(255)"))

    # 2. Backfill only where NULL (safe to re-run)
    conn.execute(text(
        "UPDATE material_vendor_history SET vendor_name_normalized = vendor_name WHERE vendor_name_normalized IS NULL AND vendor_name IS NOT NULL"
    ))
    conn.execute(text(
        "UPDATE offers SET vendor_name_normalized = LOWER(TRIM(vendor_name)) WHERE vendor_name IS NOT NULL AND vendor_name_normalized IS NULL"
    ))
    conn.execute(text(
        "UPDATE contacts SET vendor_name_normalized = LOWER(TRIM(vendor_name)) WHERE vendor_name IS NOT NULL AND vendor_name_normalized IS NULL"
    ))

    # 3. Indexes (idempotent)
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mvh_vendor_norm ON material_vendor_history (vendor_name_normalized)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_offers_vendor_norm ON offers (vendor_name_normalized)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_contacts_vendor_norm ON contacts (vendor_name_normalized)"))


def downgrade() -> None:
    op.drop_index("ix_contacts_vendor_norm", table_name="contacts")
    op.drop_index("ix_offers_vendor_norm", table_name="offers")
    op.drop_index("ix_mvh_vendor_norm", table_name="material_vendor_history")
    op.drop_column("contacts", "vendor_name_normalized")
    op.drop_column("offers", "vendor_name_normalized")
    op.drop_column("material_vendor_history", "vendor_name_normalized")
