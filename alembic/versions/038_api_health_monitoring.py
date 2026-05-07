"""Add api_usage_log table and health monitoring columns to api_sources.

Revision ID: 038_api_health_monitoring
Revises: 037_company_denormalized_counts
Create Date: 2026-03-01
"""

import sqlalchemy as sa

from alembic import op

revision = "038_api_health_monitoring"
down_revision = "037_company_denormalized_counts"


def upgrade() -> None:
    op.execute("ALTER TABLE api_sources ADD COLUMN IF NOT EXISTS monthly_quota INTEGER")
    op.execute("ALTER TABLE api_sources ADD COLUMN IF NOT EXISTS calls_this_month INTEGER DEFAULT '0'")
    op.execute("ALTER TABLE api_sources ADD COLUMN IF NOT EXISTS last_ping_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE api_sources ADD COLUMN IF NOT EXISTS last_deep_test_at TIMESTAMP WITHOUT TIME ZONE")

    op.create_table(
        "api_usage_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("api_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("endpoint", sa.String(200)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("response_ms", sa.Integer()),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.String(500)),
        sa.Column("check_type", sa.String(20), nullable=False),
        if_not_exists=True,
    )
    op.create_index("ix_usage_log_source_ts", "api_usage_log", ["source_id", "timestamp"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_usage_log_source_ts", table_name="api_usage_log", if_exists=True)
    op.drop_table("api_usage_log", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS api_sources DROP COLUMN IF EXISTS last_deep_test_at")
    op.execute("ALTER TABLE IF EXISTS api_sources DROP COLUMN IF EXISTS last_ping_at")
    op.execute("ALTER TABLE IF EXISTS api_sources DROP COLUMN IF EXISTS calls_this_month")
    op.execute("ALTER TABLE IF EXISTS api_sources DROP COLUMN IF EXISTS monthly_quota")
