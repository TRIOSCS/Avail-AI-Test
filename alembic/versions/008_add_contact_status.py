"""Add contact_status column to site_contacts table.

Revision ID: 008_add_contact_status
Revises: 007_contact_notes_archive
Create Date: 2026-02-23
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "008_add_contact_status"
down_revision: Union[str, None] = "007_contact_notes_archive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "site_contacts",
        sa.Column("contact_status", sa.String(20), server_default="new", nullable=True),
    )


def downgrade() -> None:
    op.drop_column("site_contacts", "contact_status")
