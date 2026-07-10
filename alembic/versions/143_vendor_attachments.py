"""Add vendor_card_attachments and vendor_contact_attachments tables (migration 143).

Revision ID: 143_vendor_attachments
Revises: 142_vendor_task_cols
Create Date: 2026-06-24

Changes:
  - CREATE TABLE vendor_card_attachments (mirrors company_attachments shape)
  - CREATE TABLE vendor_contact_attachments (mirrors site_contact_attachments shape)

Downgrade: drops both tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "143_vendor_attachments"
down_revision: str | None = "142_vendor_task_cols"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # vendor_card_attachments
    op.create_table(
        "vendor_card_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_card_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("library_item_id", sa.String(500), nullable=True),
        sa.Column("library_drive_id", sa.String(200), nullable=True),
        sa.Column("library_web_url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["vendor_card_id"], ["vendor_cards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vendor_card_attachments_card", "vendor_card_attachments", ["vendor_card_id"])
    op.create_index("ix_vendor_card_attachments_item", "vendor_card_attachments", ["library_item_id"])

    # vendor_contact_attachments
    op.create_table(
        "vendor_contact_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_contact_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("library_item_id", sa.String(500), nullable=True),
        sa.Column("library_drive_id", sa.String(200), nullable=True),
        sa.Column("library_web_url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["vendor_contact_id"], ["vendor_contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vendor_contact_attachments_contact", "vendor_contact_attachments", ["vendor_contact_id"])
    op.create_index("ix_vendor_contact_attachments_item", "vendor_contact_attachments", ["library_item_id"])


def downgrade() -> None:
    op.drop_index("ix_vendor_contact_attachments_item", table_name="vendor_contact_attachments")
    op.drop_index("ix_vendor_contact_attachments_contact", table_name="vendor_contact_attachments")
    op.drop_table("vendor_contact_attachments")

    op.drop_index("ix_vendor_card_attachments_item", table_name="vendor_card_attachments")
    op.drop_index("ix_vendor_card_attachments_card", table_name="vendor_card_attachments")
    op.drop_table("vendor_card_attachments")
