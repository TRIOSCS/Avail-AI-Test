"""Unified file attachments — rename onedrive_* → library_*, add 3 new attachment
tables.

What:
  - Renames 6 columns across 3 existing attachment tables:
      offer_attachments:         onedrive_item_id → library_item_id, onedrive_url → library_web_url
      requisition_attachments:   onedrive_item_id → library_item_id, onedrive_url → library_web_url
      requirement_attachments:   onedrive_item_id → library_item_id, onedrive_url → library_web_url
  - Adds library_drive_id (String 200, nullable) to the same 3 tables.
    NULL = OneDrive-fallback row; non-NULL = company SharePoint library row.
  - Creates 3 new attachment tables (unified schema):
      company_attachments        (FK → companies.id CASCADE)
      site_contact_attachments   (FK → site_contacts.id CASCADE)
      material_card_attachments  (FK → material_cards.id CASCADE)

Downgrade: drops the 3 new tables, drops library_drive_id ×3,
           renames library_item_id → onedrive_item_id and library_web_url → onedrive_url ×3.

Revision ID: 126_unified_attachments
Revises: 125_enrichment_provenance
Create Date: 2026-06-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "126_unified_attachments"
down_revision: Union[str, None] = "125_enrichment_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Rename onedrive_* → library_* on the 3 existing tables ──────────────────

    # offer_attachments
    op.alter_column("offer_attachments", "onedrive_item_id", new_column_name="library_item_id")
    op.alter_column("offer_attachments", "onedrive_url", new_column_name="library_web_url")

    # requisition_attachments
    op.alter_column("requisition_attachments", "onedrive_item_id", new_column_name="library_item_id")
    op.alter_column("requisition_attachments", "onedrive_url", new_column_name="library_web_url")

    # requirement_attachments
    op.alter_column("requirement_attachments", "onedrive_item_id", new_column_name="library_item_id")
    op.alter_column("requirement_attachments", "onedrive_url", new_column_name="library_web_url")

    # ── Add library_drive_id to the 3 existing tables ────────────────────────────
    op.add_column("offer_attachments", sa.Column("library_drive_id", sa.String(200), nullable=True))
    op.add_column("requisition_attachments", sa.Column("library_drive_id", sa.String(200), nullable=True))
    op.add_column("requirement_attachments", sa.Column("library_drive_id", sa.String(200), nullable=True))

    # ── Create 3 new attachment tables ───────────────────────────────────────────

    op.create_table(
        "company_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("library_item_id", sa.String(500), nullable=True),
        sa.Column("library_drive_id", sa.String(200), nullable=True),
        sa.Column("library_web_url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_company_attachments_company", "company_attachments", ["company_id"])

    op.create_table(
        "site_contact_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_contact_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("library_item_id", sa.String(500), nullable=True),
        sa.Column("library_drive_id", sa.String(200), nullable=True),
        sa.Column("library_web_url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["site_contact_id"], ["site_contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_site_contact_attachments_contact", "site_contact_attachments", ["site_contact_id"])

    op.create_table(
        "material_card_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("material_card_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("library_item_id", sa.String(500), nullable=True),
        sa.Column("library_drive_id", sa.String(200), nullable=True),
        sa.Column("library_web_url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["material_card_id"], ["material_cards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_material_card_attachments_card", "material_card_attachments", ["material_card_id"])


def downgrade() -> None:
    # ── Drop the 3 new tables ────────────────────────────────────────────────────
    op.drop_index("ix_material_card_attachments_card", table_name="material_card_attachments")
    op.drop_table("material_card_attachments")

    op.drop_index("ix_site_contact_attachments_contact", table_name="site_contact_attachments")
    op.drop_table("site_contact_attachments")

    op.drop_index("ix_company_attachments_company", table_name="company_attachments")
    op.drop_table("company_attachments")

    # ── Drop library_drive_id from the 3 existing tables ─────────────────────────
    op.drop_column("requirement_attachments", "library_drive_id")
    op.drop_column("requisition_attachments", "library_drive_id")
    op.drop_column("offer_attachments", "library_drive_id")

    # ── Rename library_* → onedrive_* on the 3 existing tables ──────────────────

    # requirement_attachments
    op.alter_column("requirement_attachments", "library_web_url", new_column_name="onedrive_url")
    op.alter_column("requirement_attachments", "library_item_id", new_column_name="onedrive_item_id")

    # requisition_attachments
    op.alter_column("requisition_attachments", "library_web_url", new_column_name="onedrive_url")
    op.alter_column("requisition_attachments", "library_item_id", new_column_name="onedrive_item_id")

    # offer_attachments
    op.alter_column("offer_attachments", "library_web_url", new_column_name="onedrive_url")
    op.alter_column("offer_attachments", "library_item_id", new_column_name="onedrive_item_id")
