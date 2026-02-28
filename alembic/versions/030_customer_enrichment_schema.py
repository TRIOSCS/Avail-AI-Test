"""Customer enrichment schema — waterfall tracking fields and credit usage table.

Adds enrichment columns to site_contacts (verification, role, source tracking),
customer enrichment tracking to companies, and enrichment_credit_usage table.

Revision ID: 030_customer_enrichment_schema
Revises: 029_user_commodity_tags
Create Date: 2026-02-28
"""

import sqlalchemy as sa
from alembic import op

revision = "030_customer_enrichment_schema"
down_revision = "029_user_commodity_tags"
branch_labels = None
depends_on = None


def upgrade():
    # SiteContact enrichment columns
    op.add_column("site_contacts", sa.Column("phone_verified", sa.Boolean(), server_default="false"))
    op.add_column("site_contacts", sa.Column("email_verified", sa.Boolean(), server_default="false"))
    op.add_column("site_contacts", sa.Column("email_verified_at", sa.DateTime()))
    op.add_column("site_contacts", sa.Column("email_verification_status", sa.String(20)))
    op.add_column("site_contacts", sa.Column("enrichment_source", sa.String(50)))
    op.add_column("site_contacts", sa.Column("contact_role", sa.String(50)))
    op.add_column("site_contacts", sa.Column("needs_refresh", sa.Boolean(), server_default="false"))
    op.add_column("site_contacts", sa.Column("last_enriched_at", sa.DateTime()))
    op.add_column("site_contacts", sa.Column("linkedin_url", sa.String(500)))
    op.add_column("site_contacts", sa.Column("enrichment_field_sources", sa.JSON()))

    # Company customer enrichment tracking
    op.add_column("companies", sa.Column("customer_enrichment_at", sa.DateTime()))
    op.add_column("companies", sa.Column("customer_enrichment_status", sa.String(20)))

    # Credit usage tracking table
    op.create_table(
        "enrichment_credit_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credits_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )
    op.create_index("ix_ecu_provider_month", "enrichment_credit_usage", ["provider", "month"], unique=True)


def downgrade():
    op.drop_index("ix_ecu_provider_month", "enrichment_credit_usage")
    op.drop_table("enrichment_credit_usage")
    op.drop_column("companies", "customer_enrichment_status")
    op.drop_column("companies", "customer_enrichment_at")
    op.drop_column("site_contacts", "enrichment_field_sources")
    op.drop_column("site_contacts", "linkedin_url")
    op.drop_column("site_contacts", "last_enriched_at")
    op.drop_column("site_contacts", "needs_refresh")
    op.drop_column("site_contacts", "contact_role")
    op.drop_column("site_contacts", "enrichment_source")
    op.drop_column("site_contacts", "email_verification_status")
    op.drop_column("site_contacts", "email_verified_at")
    op.drop_column("site_contacts", "email_verified")
    op.drop_column("site_contacts", "phone_verified")
