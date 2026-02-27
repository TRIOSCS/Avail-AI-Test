"""Create quote_lines table for structured line-item tracking.

Replaces JSON line_items with normalized rows for better querying.

Revision ID: 027_quote_lines
Revises: 026_prospect_promote
Create Date: 2026-02-27
"""

import sqlalchemy as sa
from alembic import op

revision = "027_quote_lines"
down_revision = "026_prospect_promote"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quote_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("quote_id", sa.Integer(), sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("material_card_id", sa.Integer(), sa.ForeignKey("material_cards.id", ondelete="SET NULL")),
        sa.Column("offer_id", sa.Integer(), sa.ForeignKey("offers.id", ondelete="SET NULL")),
        sa.Column("mpn", sa.String(255), nullable=False),
        sa.Column("manufacturer", sa.String(255)),
        sa.Column("qty", sa.Integer()),
        sa.Column("cost_price", sa.Numeric(12, 4)),
        sa.Column("sell_price", sa.Numeric(12, 4)),
        sa.Column("margin_pct", sa.Numeric(5, 2)),
        sa.Column("currency", sa.String(10), server_default="USD"),
    )
    op.create_index("ix_quote_lines_quote", "quote_lines", ["quote_id"])
    op.create_index("ix_quote_lines_card", "quote_lines", ["material_card_id"])
    op.create_index("ix_quote_lines_mpn", "quote_lines", ["mpn"])


def downgrade():
    op.drop_table("quote_lines")
