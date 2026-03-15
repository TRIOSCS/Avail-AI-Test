"""Contact note log (site_contact_id on activity_log) + archive contacts (is_active on
site_contacts).

Revision ID: 007_contact_notes_archive
Revises: 006_offers_overhaul
Create Date: 2026-02-23
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "007_contact_notes_archive"
down_revision: Union[str, None] = "006_offers_overhaul"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Archive contacts: soft-delete flag
    op.add_column(
        "site_contacts",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=True),
    )
    op.execute("UPDATE site_contacts SET is_active = TRUE WHERE is_active IS NULL")

    # Contact note log: link activity_log to site_contacts
    op.add_column(
        "activity_log",
        sa.Column(
            "site_contact_id",
            sa.Integer(),
            sa.ForeignKey("site_contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_activity_site_contact",
        "activity_log",
        ["site_contact_id", "created_at"],
        postgresql_where=sa.text("site_contact_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_activity_site_contact", table_name="activity_log")
    op.drop_column("activity_log", "site_contact_id")
    op.drop_column("site_contacts", "is_active")
