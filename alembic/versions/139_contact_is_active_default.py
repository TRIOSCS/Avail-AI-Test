"""Add server_default=true to site_contacts.is_active and backfill NULLs.

Revision ID: 139_contact_is_active_default
Revises: 138_general_tasks
Create Date: 2026-06-23

Root cause fix: SiteContact.is_active had only a Python-side default=True with no
DB server_default.  Raw/seed inserts bypassed the ORM default, leaving is_active=NULL.
Because company_contact_rows (and ~8 other call sites) filtered is_active IS TRUE,
every seeded contact was silently invisible to all users — they only saw read-only
legacy site.contact_* cards.

Changes:
  - site_contacts.is_active: set server_default='true'
  - Backfill: UPDATE site_contacts SET is_active=true WHERE is_active IS NULL

Downgrade:
  Drop the server_default only.  NULL rows are intentionally kept as true after
  backfill — a downgrade does NOT null them out again.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "139_contact_is_active_default"
down_revision = "138_general_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill existing NULLs before setting the server_default so all rows are
    # consistent from this point forward.
    op.execute("UPDATE site_contacts SET is_active = true WHERE is_active IS NULL")

    op.alter_column(
        "site_contacts",
        "is_active",
        server_default=sa.text("true"),
        existing_type=sa.Boolean(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Drop the server_default only; do NOT null out the backfilled data.
    op.alter_column(
        "site_contacts",
        "is_active",
        server_default=None,
        existing_type=sa.Boolean(),
        existing_nullable=True,
    )
