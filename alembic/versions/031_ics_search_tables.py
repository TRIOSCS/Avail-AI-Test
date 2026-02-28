"""ICsource search queue, log, worker status, and classification cache tables.

Revision ID: 031_ics_search_tables
Revises: 030_customer_enrichment_schema
Create Date: 2026-02-28

Mirrors the NetComponents search tables (022/023) for the ICsource scraper.
"""

revision = "031_ics_search_tables"
down_revision = "030_customer_enrichment_schema"

from alembic import op
import sqlalchemy as sa


def upgrade():
    # ── ics_search_queue ──────────────────────────────────────────────
    op.create_table(
        "ics_search_queue",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "requirement_id",
            sa.Integer,
            sa.ForeignKey("requirements.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "requisition_id",
            sa.Integer,
            sa.ForeignKey("requisitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mpn", sa.String(100), nullable=False),
        sa.Column("normalized_mpn", sa.String(100), nullable=False),
        sa.Column("manufacturer", sa.String(200)),
        sa.Column("description", sa.Text),
        sa.Column("commodity_class", sa.String(50)),
        sa.Column("gate_decision", sa.String(20)),
        sa.Column("gate_reason", sa.String(200)),
        sa.Column("priority", sa.SmallInteger, server_default="3"),
        sa.Column("status", sa.String(20), server_default="'pending'"),
        sa.Column("search_count", sa.Integer, server_default="0"),
        sa.Column("last_searched_at", sa.DateTime),
        sa.Column("results_count", sa.Integer),
        sa.Column("error_message", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ics_queue_poll",
        "ics_search_queue",
        ["status", "priority", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_ics_queue_dedup",
        "ics_search_queue",
        ["normalized_mpn", sa.text("last_searched_at DESC")],
        postgresql_where=sa.text("status = 'completed'"),
    )

    # ── ics_search_log ────────────────────────────────────────────────
    op.create_table(
        "ics_search_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "queue_id",
            sa.Integer,
            sa.ForeignKey("ics_search_queue.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("searched_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("results_found", sa.Integer),
        sa.Column("sightings_created", sa.Integer),
        sa.Column("page_html_hash", sa.String(64)),
        sa.Column("error", sa.Text),
    )

    # ── ics_worker_status (singleton) ─────────────────────────────────
    op.create_table(
        "ics_worker_status",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("is_running", sa.Boolean, server_default="false"),
        sa.Column("last_heartbeat", sa.DateTime),
        sa.Column("last_search_at", sa.DateTime),
        sa.Column("searches_today", sa.Integer, server_default="0"),
        sa.Column("sightings_today", sa.Integer, server_default="0"),
        sa.Column("circuit_breaker_open", sa.Boolean, server_default="false"),
        sa.Column("circuit_breaker_reason", sa.Text),
        sa.Column("daily_stats_json", sa.JSON),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_ics_worker_status_singleton"),
    )
    # Seed singleton row
    op.execute("INSERT INTO ics_worker_status (id) VALUES (1)")

    # ── ics_classification_cache ──────────────────────────────────────
    op.create_table(
        "ics_classification_cache",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("normalized_mpn", sa.String(100), nullable=False),
        sa.Column("manufacturer", sa.String(200)),
        sa.Column("commodity_class", sa.String(50), nullable=False),
        sa.Column("gate_decision", sa.String(20), nullable=False),
        sa.Column("gate_reason", sa.String(200)),
        sa.Column("classified_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("normalized_mpn", "manufacturer", name="uq_ics_cache_mpn_mfr"),
    )


def downgrade():
    op.drop_table("ics_classification_cache")
    op.drop_table("ics_worker_status")
    op.drop_table("ics_search_log")
    op.drop_index("ix_ics_queue_dedup", table_name="ics_search_queue")
    op.drop_index("ix_ics_queue_poll", table_name="ics_search_queue")
    op.drop_table("ics_search_queue")
