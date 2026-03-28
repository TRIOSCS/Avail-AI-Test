"""Add unique constraint on site_contacts and non-nullable count columns.

Revision ID: b7e2a4c91f35
Revises: fb863358a701
Create Date: 2026-03-28
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2a4c91f35"
down_revision: Union[str, None] = "fb863358a701"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Deduplicate before adding constraint
    op.execute("""
        DELETE FROM site_contacts
        WHERE id NOT IN (
            SELECT MIN(id) FROM site_contacts
            GROUP BY customer_site_id, email
        )
    """)
    op.create_unique_constraint("uq_site_contacts_site_email", "site_contacts", ["customer_site_id", "email"])

    # Fix any NULL counts before making non-nullable
    op.execute("UPDATE companies SET site_count = 0 WHERE site_count IS NULL")
    op.execute("UPDATE companies SET open_req_count = 0 WHERE open_req_count IS NULL")
    op.alter_column("companies", "site_count", nullable=False, server_default="0")
    op.alter_column("companies", "open_req_count", nullable=False, server_default="0")


def downgrade() -> None:
    op.drop_constraint("uq_site_contacts_site_email", "site_contacts")
    op.alter_column("companies", "site_count", nullable=True)
    op.alter_column("companies", "open_req_count", nullable=True)
