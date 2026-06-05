"""Add OEM-tier daily counters to enrichment_worker_status.

Adds ``oem_sourced_today`` and ``not_catalogued_today`` so the worker's per-tier
heartbeat/daily-summary observability covers the two OEM tiers (oem_sourced /
not_catalogued) added alongside the no-hallucination OEM enrichment feature. Also
documents the expanded MaterialEnrichmentStatus value set on material_cards via a
Postgres column comment (guarded so it no-ops on SQLite test runs).

Revision ID: 089_oem_enrichment_status_columns
Revises: 088_enrichment_worker_status
Create Date: 2026-06-05
"""

import sqlalchemy as sa

from alembic import op

revision = "089_oem_enrichment_status_columns"
down_revision = "088_enrichment_worker_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "enrichment_worker_status",
        sa.Column("oem_sourced_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "enrichment_worker_status",
        sa.Column("not_catalogued_today", sa.Integer(), nullable=False, server_default="0"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "COMMENT ON COLUMN material_cards.enrichment_status IS "
            "'unenriched|verified|web_sourced|oem_sourced|ai_inferred|not_found|not_catalogued "
            "(see MaterialEnrichmentStatus)'"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("COMMENT ON COLUMN material_cards.enrichment_status IS NULL")
    op.drop_column("enrichment_worker_status", "not_catalogued_today")
    op.drop_column("enrichment_worker_status", "oem_sourced_today")
