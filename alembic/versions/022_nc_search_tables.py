"""Add NetComponents search queue and log tables.

Creates nc_search_queue (tracks parts to search on NC) and nc_search_log
(audit trail per search attempt). Also adds source_searched_at to sightings.

Revision ID: 022_nc_search_tables
Revises: 021_activity_buy_plan_id
Create Date: 2026-02-27
"""

import sqlalchemy as sa

from alembic import op

revision = "022_nc_search_tables"
down_revision = "021_activity_buy_plan_id"
branch_labels = None
depends_on = None


def upgrade():
    # ── nc_search_queue ──────────────────────────────────────────────
    op.create_table(
        "nc_search_queue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "requirement_id",
            sa.Integer(),
            sa.ForeignKey("requirements.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "requisition_id",
            sa.Integer(),
            sa.ForeignKey("requisitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mpn", sa.String(100), nullable=False),
        sa.Column("normalized_mpn", sa.String(100), nullable=False),
        sa.Column("manufacturer", sa.String(200)),
        sa.Column("description", sa.Text()),
        sa.Column("commodity_class", sa.String(50)),
        sa.Column("gate_decision", sa.String(20)),
        sa.Column("gate_reason", sa.String(200)),
        sa.Column("priority", sa.SmallInteger(), server_default="3"),
        sa.Column("status", sa.String(20), server_default="'pending'"),
        sa.Column("search_count", sa.Integer(), server_default="0"),
        sa.Column("last_searched_at", sa.DateTime(timezone=True)),
        sa.Column("results_count", sa.Integer()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    # Partial index for polling queued items
    op.create_index(
        "ix_nc_queue_poll",
        "nc_search_queue",
        ["status", "priority", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    # Partial index for dedup lookups on completed searches
    op.create_index(
        "ix_nc_queue_dedup",
        "nc_search_queue",
        ["normalized_mpn", sa.text("last_searched_at DESC")],
        postgresql_where=sa.text("status = 'completed'"),
    )

    # ── nc_search_log ────────────────────────────────────────────────
    op.create_table(
        "nc_search_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "queue_id",
            sa.Integer(),
            sa.ForeignKey("nc_search_queue.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "searched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("results_found", sa.Integer()),
        sa.Column("sightings_created", sa.Integer()),
        sa.Column("page_html_hash", sa.String(64)),
        sa.Column("error", sa.Text()),
    )

    # ── sightings: add source_searched_at ────────────────────────────
    op.add_column(
        "sightings",
        sa.Column("source_searched_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_column("sightings", "source_searched_at")
    op.drop_table("nc_search_log")
    op.drop_index("ix_nc_queue_dedup", table_name="nc_search_queue")
    op.drop_index("ix_nc_queue_poll", table_name="nc_search_queue")
    op.drop_table("nc_search_queue")
