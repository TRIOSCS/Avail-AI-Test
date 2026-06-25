"""Harden is_active on companies, customer_sites, users: backfill NULLs + NOT NULL +
server_default.

Revision ID: 149_is_active_defaults
Revises: 148_site_dnc
Create Date: 2026-06-24

Root cause fix (same defect class as migration 139 for site_contacts): companies.is_active,
customer_sites.is_active and users.is_active had only a Python-side default=True with no DB
server_default and were nullable. A raw/bulk/seed insert (or a \\copy / pg_restore) that omits
is_active leaves NULL → the row silently vanishes from every ``is_active IS TRUE`` filter, and
for users a NULL is_active triggers a permanent 403 lockout (app/dependencies.py require_user).
This hardens all three BEFORE the multi-user data import.

Changes (each of companies, customer_sites, users):
  - Backfill: UPDATE <tbl> SET is_active = true WHERE is_active IS NULL  (must precede NOT NULL)
  - is_active: server_default='true', nullable=False

Downgrade:
  Restore nullable=True and drop the server_default. Backfilled rows intentionally stay true
  (a downgrade does NOT re-null them).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "149_is_active_defaults"
down_revision = "148_site_dnc"
branch_labels = None
depends_on = None

_TABLES = ("companies", "customer_sites", "users")


def upgrade() -> None:
    for tbl in _TABLES:
        # Backfill existing NULLs BEFORE the NOT NULL alter, or the alter rejects them.
        op.execute(f"UPDATE {tbl} SET is_active = true WHERE is_active IS NULL")
        op.alter_column(
            tbl,
            "is_active",
            server_default=sa.text("true"),
            existing_type=sa.Boolean(),
            existing_nullable=True,
            nullable=False,
        )


def downgrade() -> None:
    for tbl in _TABLES:
        op.alter_column(
            tbl,
            "is_active",
            server_default=None,
            existing_type=sa.Boolean(),
            existing_nullable=False,
            nullable=True,
        )
