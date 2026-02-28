"""Clay OAuth2 token storage table.

Stores access/refresh tokens for the Clay OAuth2 integration.
Single-row table — one active token pair at a time.

Revision ID: 032_clay_oauth_tokens
Revises: 031_ics_search_tables
Create Date: 2026-02-28
"""

import sqlalchemy as sa
from alembic import op

revision = "032_clay_oauth_tokens"
down_revision = "031_ics_search_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "clay_oauth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("scope", sa.String(255), server_default="mcp mcp:run-enrichment"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade():
    op.drop_table("clay_oauth_tokens")
