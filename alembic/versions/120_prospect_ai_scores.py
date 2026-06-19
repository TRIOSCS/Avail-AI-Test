"""Add trio_match_score + opportunity_score to prospect_accounts (SP3 AI screening).

What:
  * prospect_accounts.trio_match_score (Integer, default 0, indexed) — AI procurement-fit score
  * prospect_accounts.opportunity_score (Integer, default 0, indexed) — AI opportunity size score
  Both columns store 0-100 scalars from the AI screen verdict; full verdict in JSONB enrichment_data['ai_screen'].
Downgrade: drops the two columns and their indexes.

Revision ID: 120_prospect_ai_scores
Revises: 119_alert_seen
Create Date: 2026-06-18
"""

import sqlalchemy as sa

from alembic import op

revision = "120_prospect_ai_scores"
down_revision = "119_alert_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospect_accounts", sa.Column("trio_match_score", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("prospect_accounts", sa.Column("opportunity_score", sa.Integer(), nullable=True, server_default="0"))
    op.create_index("ix_prospect_accounts_trio_match_score", "prospect_accounts", ["trio_match_score"])
    op.create_index("ix_prospect_accounts_opportunity_score", "prospect_accounts", ["opportunity_score"])


def downgrade() -> None:
    op.drop_index("ix_prospect_accounts_opportunity_score", table_name="prospect_accounts")
    op.drop_index("ix_prospect_accounts_trio_match_score", table_name="prospect_accounts")
    op.drop_column("prospect_accounts", "opportunity_score")
    op.drop_column("prospect_accounts", "trio_match_score")
