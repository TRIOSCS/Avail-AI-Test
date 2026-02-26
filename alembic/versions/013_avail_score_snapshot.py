"""Add avail_score_snapshot table for Avail Score system.

0-100 scoring (50 behaviors + 50 outcomes) ranking buyers and salespeople
with $500/$250 bonus for 1st/2nd place finishers.
Idempotent: table may already exist from create_all / startup.

Revision ID: 013_avail_score_snapshot
Revises: 012_vendor_name_normalized
Create Date: 2026-02-26
"""

from alembic import op
from sqlalchemy import text

revision = "013_avail_score_snapshot"
down_revision = "012_vendor_name_normalized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS avail_score_snapshot (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            month DATE NOT NULL,
            role_type VARCHAR(20) NOT NULL,
            b1_score FLOAT DEFAULT 0, b1_label VARCHAR(50), b1_raw VARCHAR(100),
            b2_score FLOAT DEFAULT 0, b2_label VARCHAR(50), b2_raw VARCHAR(100),
            b3_score FLOAT DEFAULT 0, b3_label VARCHAR(50), b3_raw VARCHAR(100),
            b4_score FLOAT DEFAULT 0, b4_label VARCHAR(50), b4_raw VARCHAR(100),
            b5_score FLOAT DEFAULT 0, b5_label VARCHAR(50), b5_raw VARCHAR(100),
            behavior_total FLOAT DEFAULT 0,
            o1_score FLOAT DEFAULT 0, o1_label VARCHAR(50), o1_raw VARCHAR(100),
            o2_score FLOAT DEFAULT 0, o2_label VARCHAR(50), o2_raw VARCHAR(100),
            o3_score FLOAT DEFAULT 0, o3_label VARCHAR(50), o3_raw VARCHAR(100),
            o4_score FLOAT DEFAULT 0, o4_label VARCHAR(50), o4_raw VARCHAR(100),
            o5_score FLOAT DEFAULT 0, o5_label VARCHAR(50), o5_raw VARCHAR(100),
            outcome_total FLOAT DEFAULT 0,
            total_score FLOAT DEFAULT 0,
            rank INTEGER,
            qualified BOOLEAN DEFAULT FALSE,
            bonus_amount FLOAT DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
    """))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_ass_user_month ON avail_score_snapshot (user_id, month, role_type)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ass_month_role_rank ON avail_score_snapshot (month, role_type, rank)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ass_month_role_score ON avail_score_snapshot (month, role_type, total_score)"
    ))


def downgrade() -> None:
    op.drop_index("ix_ass_month_role_score", table_name="avail_score_snapshot")
    op.drop_index("ix_ass_month_role_rank", table_name="avail_score_snapshot")
    op.drop_index("ix_ass_user_month", table_name="avail_score_snapshot")
    op.drop_table("avail_score_snapshot")
