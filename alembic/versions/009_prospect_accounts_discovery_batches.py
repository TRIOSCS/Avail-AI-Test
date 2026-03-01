"""Add prospect_accounts and discovery_batches tables, source column on companies.

Revision ID: 009_prospect_accounts_discovery_batches
Revises: 008_add_contact_status
Create Date: 2026-02-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "009_prospect_accounts_discovery_batches"
down_revision: Union[str, None] = "008_add_contact_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- discovery_batches (must exist before prospect_accounts FK) --
    op.create_table(
        "discovery_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("batch_id", sa.String(100), unique=True, nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("segment", sa.String(100)),
        sa.Column("regions", JSONB, server_default="[]"),
        sa.Column("search_filters", JSONB, server_default="{}"),
        sa.Column("status", sa.String(20), server_default="running"),
        sa.Column("prospects_found", sa.Integer, server_default="0"),
        sa.Column("prospects_new", sa.Integer, server_default="0"),
        sa.Column("prospects_updated", sa.Integer, server_default="0"),
        sa.Column("credits_used", sa.Integer, server_default="0"),
        sa.Column("error_message", sa.Text),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("completed_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    # -- prospect_accounts --
    op.create_table(
        "prospect_accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), unique=True, nullable=False),
        sa.Column("website", sa.String(500)),
        sa.Column("industry", sa.String(255)),
        sa.Column("naics_code", sa.String(10)),
        sa.Column("employee_count_range", sa.String(50)),
        sa.Column("revenue_range", sa.String(50)),
        sa.Column("hq_location", sa.String(255)),
        sa.Column("region", sa.String(50)),
        sa.Column("description", sa.Text),
        sa.Column("parent_company_domain", sa.String(255)),
        sa.Column("fit_score", sa.Integer, server_default="0"),
        sa.Column("fit_reasoning", sa.Text),
        sa.Column("readiness_score", sa.Integer, server_default="0"),
        sa.Column("readiness_signals", JSONB, server_default="{}"),
        sa.Column("discovery_source", sa.String(50), nullable=False),
        sa.Column(
            "discovery_batch_id",
            sa.Integer,
            sa.ForeignKey("discovery_batches.id"),
        ),
        sa.Column("status", sa.String(20), server_default="suggested"),
        sa.Column("import_priority", sa.String(20)),
        sa.Column("historical_context", JSONB, server_default="{}"),
        sa.Column("claimed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("claimed_at", sa.DateTime),
        sa.Column("dismissed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("dismissed_at", sa.DateTime),
        sa.Column("dismiss_reason", sa.String(255)),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id")),
        sa.Column("contacts_preview", JSONB, server_default="[]"),
        sa.Column("similar_customers", JSONB, server_default="[]"),
        sa.Column("enrichment_data", JSONB, server_default="{}"),
        sa.Column("email_pattern", sa.String(100)),
        sa.Column("ai_writeup", sa.Text),
        sa.Column("last_enriched_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    # Indexes for prospect_accounts
    op.create_index("ix_prospect_accounts_status", "prospect_accounts", ["status"])
    op.create_index("ix_prospect_accounts_fit_score", "prospect_accounts", ["fit_score"])
    op.create_index("ix_prospect_accounts_readiness_score", "prospect_accounts", ["readiness_score"])
    op.create_index("ix_prospect_accounts_region", "prospect_accounts", ["region"])
    op.create_index("ix_prospect_accounts_discovery_source", "prospect_accounts", ["discovery_source"])
    op.create_index(
        "ix_prospect_accounts_status_fit",
        "prospect_accounts",
        ["status", "fit_score"],
    )

    # -- Add source column to companies --
    op.add_column(
        "companies",
        sa.Column("source", sa.String(50), server_default="manual"),
    )


def downgrade() -> None:
    op.drop_column("companies", "source")
    op.drop_table("prospect_accounts")
    op.drop_table("discovery_batches")
