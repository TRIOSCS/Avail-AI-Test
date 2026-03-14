"""Add normalized source category columns to lead tables.

Keeps existing connector-level source_type values while adding a stable
category field aligned to sourcing handoff taxonomy.

Revision ID: 078
Revises: 077
Create Date: 2026-03-14
"""

import sqlalchemy as sa

from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sourcing_leads", sa.Column("primary_source_category", sa.String(length=32), nullable=True))
    op.create_index("ix_sourcing_leads_primary_source_category", "sourcing_leads", ["primary_source_category"])

    op.add_column("lead_evidence", sa.Column("source_category", sa.String(length=32), nullable=True))
    op.create_index("ix_lead_evidence_source_category", "lead_evidence", ["source_category"])


def downgrade() -> None:
    op.drop_index("ix_lead_evidence_source_category", table_name="lead_evidence")
    op.drop_column("lead_evidence", "source_category")

    op.drop_index("ix_sourcing_leads_primary_source_category", table_name="sourcing_leads")
    op.drop_column("sourcing_leads", "primary_source_category")
