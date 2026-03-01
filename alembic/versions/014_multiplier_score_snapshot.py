"""Add multiplier_score_snapshot table for competitive bonus determination.

Non-stacking offer pipeline points (each offer earns only its highest tier)
plus bonus points from RFQs/stock lists (buyer) or accounts (sales).
1st place $500, 2nd place $250 — requires minimum Avail Score.
Idempotent: table may already exist from create_all / startup.

Revision ID: 014_multiplier_score_snapshot
Revises: 013_avail_score_snapshot
Create Date: 2026-02-26
"""

from sqlalchemy import text

from alembic import op

revision = "014_multiplier_score_snapshot"
down_revision = "013_avail_score_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("""
        CREATE TABLE IF NOT EXISTS multiplier_score_snapshot (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            month DATE NOT NULL,
            role_type VARCHAR(20) NOT NULL,
            offer_points FLOAT DEFAULT 0,
            bonus_points FLOAT DEFAULT 0,
            total_points FLOAT DEFAULT 0,
            offers_total INTEGER DEFAULT 0,
            offers_base_count INTEGER DEFAULT 0,
            offers_base_pts FLOAT DEFAULT 0,
            offers_quoted_count INTEGER DEFAULT 0,
            offers_quoted_pts FLOAT DEFAULT 0,
            offers_bp_count INTEGER DEFAULT 0,
            offers_bp_pts FLOAT DEFAULT 0,
            offers_po_count INTEGER DEFAULT 0,
            offers_po_pts FLOAT DEFAULT 0,
            rfqs_sent_count INTEGER DEFAULT 0,
            rfqs_sent_pts FLOAT DEFAULT 0,
            stock_lists_count INTEGER DEFAULT 0,
            stock_lists_pts FLOAT DEFAULT 0,
            quotes_sent_count INTEGER DEFAULT 0,
            quotes_sent_pts FLOAT DEFAULT 0,
            quotes_won_count INTEGER DEFAULT 0,
            quotes_won_pts FLOAT DEFAULT 0,
            proactive_sent_count INTEGER DEFAULT 0,
            proactive_sent_pts FLOAT DEFAULT 0,
            proactive_converted_count INTEGER DEFAULT 0,
            proactive_converted_pts FLOAT DEFAULT 0,
            new_accounts_count INTEGER DEFAULT 0,
            new_accounts_pts FLOAT DEFAULT 0,
            rank INTEGER,
            avail_score FLOAT DEFAULT 0,
            qualified BOOLEAN DEFAULT FALSE,
            bonus_amount FLOAT DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
    """)
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_mss_user_month ON multiplier_score_snapshot (user_id, month, role_type)"
        )
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_mss_month_role_rank ON multiplier_score_snapshot (month, role_type, rank)")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mss_month_role_points ON multiplier_score_snapshot (month, role_type, total_points)"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_mss_month_role_points", table_name="multiplier_score_snapshot")
    op.drop_index("ix_mss_month_role_rank", table_name="multiplier_score_snapshot")
    op.drop_index("ix_mss_user_month", table_name="multiplier_score_snapshot")
    op.drop_table("multiplier_score_snapshot")
