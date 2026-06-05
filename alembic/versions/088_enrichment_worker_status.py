"""Add enrichment_worker_status singleton table.

Single-row heartbeat/stats table for the paced web-search enrichment worker.
Tracks is_running, daily tier counts (web_sourced, ai_inferred, not_found),
circuit breaker state, and last heartbeat.

Revision ID: 088_enrichment_worker_status
Revises: a1f7c2d9e4b8
Create Date: 2026-06-05
"""

import sqlalchemy as sa

from alembic import op

revision = "088_enrichment_worker_status"
down_revision = "a1f7c2d9e4b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enrichment_worker_status",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("is_running", sa.Boolean, server_default="false", nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True)),
        sa.Column("last_enriched_at", sa.DateTime(timezone=True)),
        sa.Column("enriched_today", sa.Integer, server_default="0", nullable=False),
        sa.Column("web_sourced_today", sa.Integer, server_default="0", nullable=False),
        sa.Column("ai_inferred_today", sa.Integer, server_default="0", nullable=False),
        sa.Column("not_found_today", sa.Integer, server_default="0", nullable=False),
        sa.Column("circuit_breaker_open", sa.Boolean, server_default="false", nullable=False),
        sa.Column("circuit_breaker_reason", sa.Text),
        sa.Column("daily_stats_json", sa.JSON),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_enrichment_worker_status_singleton"),
        if_not_exists=True,
    )
    # Seed singleton row (idempotent — matches the if_not_exists table create, so a
    # re-run after a partial apply does not fail on the primary-key conflict).
    op.execute("INSERT INTO enrichment_worker_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade() -> None:
    op.drop_table("enrichment_worker_status", if_exists=True)
