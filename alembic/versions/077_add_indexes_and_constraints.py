"""Add missing indexes and constraints from code review.

- Index on material_cards.deleted_at for soft-delete query performance
- Unique constraint on site_contacts (customer_site_id, email) to prevent duplicates
- NOT NULL on Company denormalized count columns (site_count, open_req_count)

Revision ID: 077
Revises: 076
Create Date: 2026-03-14
"""

import sqlalchemy as sa

from alembic import op

revision = "077"
down_revision = "076"


def upgrade() -> None:
    # 1. Index on material_cards.deleted_at for soft-delete query performance
    op.create_index(
        "ix_material_cards_deleted_at",
        "material_cards",
        ["deleted_at"],
        if_not_exists=True,
    )

    # 2. Unique constraint on site_contacts (customer_site_id, email)
    # First clean up any existing duplicates (keep the latest by id)
    conn = op.get_bind()
    conn.execute(
        sa.text("""
        DELETE FROM site_contacts
        WHERE id NOT IN (
            SELECT MAX(id) FROM site_contacts
            WHERE email IS NOT NULL AND email != ''
            GROUP BY customer_site_id, email
        )
        AND email IS NOT NULL AND email != ''
        AND EXISTS (
            SELECT 1 FROM site_contacts sc2
            WHERE sc2.customer_site_id = site_contacts.customer_site_id
              AND sc2.email = site_contacts.email
              AND sc2.id > site_contacts.id
        )
        """)
    )
    op.create_index(
        "uq_site_contacts_site_email",
        "site_contacts",
        ["customer_site_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL AND email != ''"),
    )

    # 3. NOT NULL on Company denormalized counts (backfill NULLs first)
    conn.execute(sa.text("UPDATE companies SET site_count = 0 WHERE site_count IS NULL"))
    conn.execute(sa.text("UPDATE companies SET open_req_count = 0 WHERE open_req_count IS NULL"))
    op.alter_column("companies", "site_count", nullable=False, server_default="0")
    op.alter_column("companies", "open_req_count", nullable=False, server_default="0")


def downgrade() -> None:
    # Reverse NOT NULL
    op.alter_column("companies", "site_count", nullable=True)
    op.alter_column("companies", "open_req_count", nullable=True)

    # Drop unique index
    op.drop_index("uq_site_contacts_site_email", table_name="site_contacts")

    # Drop deleted_at index
    op.drop_index("ix_material_cards_deleted_at", table_name="material_cards")
