"""The Broker Forum (TBF) search queue, log, and worker status tables.

Revision ID: 130_tbf_search_tables
Revises: 129_drop_bid_tables
Create Date: 2026-06-23

Mirrors the NetComponents search tables (022/023) for the TBF browser worker,
using the COMPOUND (requirement_id, normalized_mpn) unique constraint so one
requirement can carry multiple queue rows (primary + resolved-AVL MPNs).

Phase 1 ships DORMANT — the worker stays idle until creds + selectors exist.
"""

revision = "131_tbf_search_tables"
down_revision = "130_phone_normalization"

import sqlalchemy as sa

from alembic import op


def upgrade():
    # ── tbf_search_queue ──────────────────────────────────────────────
    op.create_table(
        "tbf_search_queue",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "requirement_id",
            sa.Integer,
            sa.ForeignKey("requirements.id", ondelete="CASCADE"),
            nullable=False,
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
        sa.Column("last_searched_at", sa.DateTime(timezone=True)),
        sa.Column("results_count", sa.Integer),
        sa.Column("error_message", sa.Text),
        sa.Column("resolved_via_spec_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # Compound dedup: one requirement can have multiple queue rows
        # (primary MPN + resolver-driven AVL MPNs), one per normalized MPN.
        sa.UniqueConstraint("requirement_id", "normalized_mpn", name="uq_tbf_queue_requirement_mpn"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_tbf_queue_poll",
        "tbf_search_queue",
        ["status", "priority", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_tbf_queue_dedup",
        "tbf_search_queue",
        ["normalized_mpn", sa.text("last_searched_at DESC")],
        postgresql_where=sa.text("status = 'completed'"),
        if_not_exists=True,
    )

    # ── tbf_search_log ────────────────────────────────────────────────
    op.create_table(
        "tbf_search_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "queue_id",
            sa.Integer,
            sa.ForeignKey("tbf_search_queue.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("searched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("results_found", sa.Integer),
        sa.Column("sightings_created", sa.Integer),
        sa.Column("page_html_hash", sa.String(64)),
        sa.Column("error", sa.Text),
        if_not_exists=True,
    )
    op.create_index(
        "ix_tbf_search_log_queue_id",
        "tbf_search_log",
        ["queue_id"],
        if_not_exists=True,
    )

    # ── tbf_worker_status (singleton) ─────────────────────────────────
    op.create_table(
        "tbf_worker_status",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("is_running", sa.Boolean, server_default="false"),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True)),
        sa.Column("last_search_at", sa.DateTime(timezone=True)),
        sa.Column("searches_today", sa.Integer, server_default="0"),
        sa.Column("sightings_today", sa.Integer, server_default="0"),
        sa.Column("circuit_breaker_open", sa.Boolean, server_default="false"),
        sa.Column("circuit_breaker_reason", sa.Text),
        sa.Column("daily_stats_json", sa.JSON),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_tbf_worker_status_singleton"),
        if_not_exists=True,
    )
    # Seed singleton row (primary seed — startup.py reseed is the idempotent backup)
    op.execute("INSERT INTO tbf_worker_status (id) VALUES (1)")


def downgrade():
    op.drop_table("tbf_worker_status", if_exists=True)
    op.drop_index("ix_tbf_search_log_queue_id", table_name="tbf_search_log", if_exists=True)
    op.drop_table("tbf_search_log", if_exists=True)
    op.drop_index("ix_tbf_queue_dedup", table_name="tbf_search_queue", if_exists=True)
    op.drop_index("ix_tbf_queue_poll", table_name="tbf_search_queue", if_exists=True)
    op.drop_table("tbf_search_queue", if_exists=True)
