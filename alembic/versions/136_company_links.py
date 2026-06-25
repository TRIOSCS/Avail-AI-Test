"""Add primary_contact_id and parent_company_id to companies.

Revision ID: 136_company_links
Revises: 133_resell_outreach_schema
Create Date: 2026-06-23

Adds two nullable FKs to companies:
  primary_contact_id → site_contacts.id (SET NULL) — account-level primary contact
  parent_company_id  → companies.id (SET NULL)      — parent/child hierarchy
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "136_company_links"
down_revision: Union[str, None] = "133_resell_outreach_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("primary_contact_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("parent_company_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_companies_primary_contact",
        "companies",
        "site_contacts",
        ["primary_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_companies_parent_company",
        "companies",
        "companies",
        ["parent_company_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_companies_primary_contact_id", "companies", ["primary_contact_id"])
    op.create_index("ix_companies_parent_company_id", "companies", ["parent_company_id"])


def downgrade() -> None:
    op.drop_index("ix_companies_parent_company_id", table_name="companies")
    op.drop_index("ix_companies_primary_contact_id", table_name="companies")
    op.drop_constraint("fk_companies_parent_company", "companies", type_="foreignkey")
    op.drop_constraint("fk_companies_primary_contact", "companies", type_="foreignkey")
    op.drop_column("companies", "parent_company_id")
    op.drop_column("companies", "primary_contact_id")
