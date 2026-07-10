"""Add custom_fields JSONB to companies and site_contacts.

Revision ID: 132_crm_custom_fields
Revises: 131_tbf_search_tables
Create Date: 2026-06-23

No-per-field migration for arbitrary label:value pairs on CRM accounts and contacts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "132_crm_custom_fields"
down_revision: str | None = "131_tbf_search_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "custom_fields",
            JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=True,
        ),
    )
    op.add_column(
        "site_contacts",
        sa.Column(
            "custom_fields",
            JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("site_contacts", "custom_fields")
    op.drop_column("companies", "custom_fields")
