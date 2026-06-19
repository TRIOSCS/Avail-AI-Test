"""Add trio_match_score + opportunity_score to prospect_accounts (SP3 AI screening).

What:
  * prospect_accounts.trio_match_score (Integer, default 0, indexed) — AI procurement-fit score
  * prospect_accounts.opportunity_score (Integer, default 0, indexed) — AI opportunity size score
  Both columns store 0-100 scalars from the AI screen verdict; full verdict in JSONB enrichment_data['ai_screen'].
Downgrade: drops the two columns and their indexes.

Revision ID: 122_prospect_ai_scores
Revises: 121_datasheet_lib_col_rename
Create Date: 2026-06-18

RE-NUMBERED 120->121: feat/crm-aiorg claimed 120_company_name_matching and merged to main
first; re-chained onto it per the MIGRATION_NUMBERS_IN_FLIGHT protocol.
"""

import sqlalchemy as sa

from alembic import op

revision = "122_prospect_ai_scores"
down_revision = "121_datasheet_lib_col_rename"
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
