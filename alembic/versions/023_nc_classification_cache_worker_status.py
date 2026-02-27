"""Add nc_classification_cache and nc_worker_status tables.

nc_classification_cache persists AI gate decisions across worker restarts.
nc_worker_status is a singleton table for API server to read worker health.

Revision ID: 023_nc_cache_status
Revises: 022_nc_search_tables
Create Date: 2026-02-27
"""

import sqlalchemy as sa
from alembic import op

revision = "023_nc_cache_status"
down_revision = "022_nc_search_tables"
branch_labels = None
depends_on = None


def upgrade():
    # ── nc_classification_cache ──────────────────────────────────────
    op.create_table(
        "nc_classification_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("normalized_mpn", sa.String(100), nullable=False),
        sa.Column("manufacturer", sa.String(200)),
        sa.Column("commodity_class", sa.String(50), nullable=False),
        sa.Column("gate_decision", sa.String(20), nullable=False),
        sa.Column("gate_reason", sa.String(200)),
        sa.Column(
            "classified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "normalized_mpn",
            "manufacturer",
            name="uq_nc_cache_mpn_mfr",
        ),
    )

    # ── nc_worker_status (singleton) ─────────────────────────────────
    op.create_table(
        "nc_worker_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("is_running", sa.Boolean(), server_default="false"),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True)),
        sa.Column("last_search_at", sa.DateTime(timezone=True)),
        sa.Column("searches_today", sa.Integer(), server_default="0"),
        sa.Column("sightings_today", sa.Integer(), server_default="0"),
        sa.Column("circuit_breaker_open", sa.Boolean(), server_default="false"),
        sa.Column("circuit_breaker_reason", sa.Text()),
        sa.Column("daily_stats_json", sa.JSON()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="ck_nc_worker_status_singleton"),
    )

    # Insert the singleton row
    op.execute("INSERT INTO nc_worker_status (id) VALUES (1)")


def downgrade():
    op.drop_table("nc_worker_status")
    op.drop_table("nc_classification_cache")
