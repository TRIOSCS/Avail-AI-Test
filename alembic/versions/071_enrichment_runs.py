"""Add enrichment_runs table for tracking autonomous enrichment pipeline state.

Revision ID: 071_enrichment_runs
Revises: f3fbddb04947
Create Date: 2026-03-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "071_enrichment_runs"
down_revision: Union[str, None] = "f3fbddb04947"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "enrichment_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String(100), unique=True, nullable=False),
        sa.Column("phase", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("batch_ids", JSONB, server_default="[]"),
        sa.Column("request_map", JSONB, server_default="{}"),
        sa.Column("progress", JSONB, server_default="{}"),
        sa.Column("stats", JSONB, server_default="{}"),
        sa.Column("error_message", sa.Text),
        sa.Column("started_at", sa.DateTime),
        sa.Column("completed_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("enrichment_runs")
