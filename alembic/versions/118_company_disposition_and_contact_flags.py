"""Company disposition + site-contact priority/archive flags (Increment 1).

What:
  * companies.disposition (String(20), indexed; NULL => active) +
    disposition_reason (String, nullable) + disposition_set_by
    (FK users.id ON DELETE SET NULL) + disposition_set_at (timestamptz)
  * site_contacts.is_priority + is_archived (Boolean NOT NULL, server_default
    false) — mirror do_not_contact exactly.
Downgrade: drops the added columns + index (reversible).

Revision ID: 118_company_disposition_and_contact_flags
Revises: 117_datasheet_library_drive_id
Create Date: 2026-06-18
"""

import sqlalchemy as sa

from alembic import op

revision = "118_company_disposition_and_contact_flags"
down_revision = "117_datasheet_library_drive_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("disposition", sa.String(length=20), nullable=True))
    op.add_column("companies", sa.Column("disposition_reason", sa.String(), nullable=True))
    op.add_column(
        "companies",
        sa.Column("disposition_set_by", sa.Integer(), nullable=True),
    )
    op.add_column("companies", sa.Column("disposition_set_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_companies_disposition_set_by_users",
        "companies",
        "users",
        ["disposition_set_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_companies_disposition", "companies", ["disposition"])

    op.add_column(
        "site_contacts",
        sa.Column("is_priority", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "site_contacts",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("site_contacts", "is_archived")
    op.drop_column("site_contacts", "is_priority")
    op.drop_index("ix_companies_disposition", table_name="companies")
    op.drop_constraint("fk_companies_disposition_set_by_users", "companies", type_="foreignkey")
    op.drop_column("companies", "disposition_set_at")
    op.drop_column("companies", "disposition_set_by")
    op.drop_column("companies", "disposition_reason")
    op.drop_column("companies", "disposition")
