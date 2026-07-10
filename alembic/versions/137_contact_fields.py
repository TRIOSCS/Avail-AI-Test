"""Add first_name, last_name, contact_owner_id to site_contacts.

Revision ID: 137_contact_fields
Revises: 136_company_links
Create Date: 2026-06-23

Adds to site_contacts:
  first_name      String(120) nullable
  last_name       String(120) nullable
  contact_owner_id Integer FK → users.id ondelete=SET NULL nullable indexed

Backfills first_name/last_name from existing full_name by splitting on the first space:
  "Jane Doe"  → first_name="Jane", last_name="Doe"
  "Cher"      → first_name="Cher", last_name=NULL
  "Mary Jane Watson" → first_name="Mary", last_name="Jane Watson"
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "137_contact_fields"
down_revision: str | None = "136_company_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "site_contacts",
        sa.Column("first_name", sa.String(120), nullable=True),
    )
    op.add_column(
        "site_contacts",
        sa.Column("last_name", sa.String(120), nullable=True),
    )
    op.add_column(
        "site_contacts",
        sa.Column("contact_owner_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_site_contacts_contact_owner",
        "site_contacts",
        "users",
        ["contact_owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_site_contacts_contact_owner_id", "site_contacts", ["contact_owner_id"])

    # Backfill first_name / last_name from full_name.
    # Split on the FIRST space only: "A B C" → first="A", last="B C".
    # Single-token names → first_name only, last_name stays NULL.
    # Uses a Python loop over rows so it works identically on SQLite and PostgreSQL.
    bind = op.get_bind()
    rows = bind.execute(text("SELECT id, full_name FROM site_contacts")).fetchall()
    for row_id, full_name in rows:
        if not full_name:
            continue
        parts = full_name.strip().split(" ", 1)
        first = parts[0] or None
        last = parts[1].strip() if len(parts) > 1 else None
        bind.execute(
            text("UPDATE site_contacts SET first_name = :fn, last_name = :ln WHERE id = :rid"),
            {"fn": first, "ln": last or None, "rid": row_id},
        )


def downgrade() -> None:
    op.drop_index("ix_site_contacts_contact_owner_id", table_name="site_contacts")
    op.drop_constraint("fk_site_contacts_contact_owner", "site_contacts", type_="foreignkey")
    op.drop_column("site_contacts", "contact_owner_id")
    op.drop_column("site_contacts", "last_name")
    op.drop_column("site_contacts", "first_name")
