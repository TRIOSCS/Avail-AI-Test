"""Proactive augmentation schema (2026-08-06 spec): demand signals + digest + tracking.

What:
  - proactive_matches += match_source, requirement_count, last_asked_at,
    last_asked_qty — the engine now seeds from requirement history (any
    requisition status, windowed) + hotlists instead of purchase history;
    these columns carry the demand signals each match line surfaces.
  - NEW proactive_digests — per-salesperson outreach digest, generated as a
    draft, human-reviewed, sent manually (never automatically).
  - NEW proactive_outreach_lines — one digest line frozen at generation
    (last asked / available / low cost / quote + win price anchors) plus
    post-send tracking (contacted, outcome, produced requisition/quote,
    sales_order_number as an external ERP reference only).

All additive; downgrade drops the new tables then the new columns. Indexes
are declared in the models' __table_args__ with identical names so the
fresh-DB drift gate stays green.

Called by: alembic (upgrade/downgrade).
Depends on: proactive_matches (001_initial), requisitions, quotes, companies,
users, material_cards.

Revision ID: 205_proactive_digest_tracking
Revises: 204_backfill_task_assignee
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "205_proactive_digest_tracking"
down_revision = "204_backfill_task_assignee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("proactive_matches", sa.Column("match_source", sa.String(20), nullable=True))
    op.add_column("proactive_matches", sa.Column("requirement_count", sa.Integer(), nullable=True))
    op.add_column("proactive_matches", sa.Column("last_asked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("proactive_matches", sa.Column("last_asked_qty", sa.Integer(), nullable=True))

    op.create_table(
        "proactive_digests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("salesperson_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(20), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pdig_sales", "proactive_digests", ["salesperson_id"])
    op.create_index("ix_pdig_status", "proactive_digests", ["status"])
    op.create_index("ix_pdig_generated", "proactive_digests", ["generated_at"])

    op.create_table(
        "proactive_outreach_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "digest_id",
            sa.Integer(),
            sa.ForeignKey("proactive_digests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match_id", sa.Integer(), sa.ForeignKey("proactive_matches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mpn", sa.String(255), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("salesperson_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_asked_qty", sa.Integer(), nullable=True),
        sa.Column("requirement_count", sa.Integer(), nullable=True),
        sa.Column("available_qty", sa.Integer(), nullable=True),
        sa.Column("low_cost", sa.Numeric(12, 4), nullable=True),
        sa.Column("last_quote_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("last_quote_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_quote_company", sa.String(255), nullable=True),
        sa.Column("last_quote_rep", sa.String(255), nullable=True),
        sa.Column("last_win_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("last_win_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_win_company", sa.String(255), nullable=True),
        sa.Column("last_win_rep", sa.String(255), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contacted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("outcome", sa.String(20), nullable=True),
        sa.Column(
            "produced_requisition_id",
            sa.Integer(),
            sa.ForeignKey("requisitions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("produced_quote_id", sa.Integer(), sa.ForeignKey("quotes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sales_order_number", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pol_digest", "proactive_outreach_lines", ["digest_id"])
    op.create_index("ix_pol_sales", "proactive_outreach_lines", ["salesperson_id"])
    op.create_index("ix_pol_company", "proactive_outreach_lines", ["company_id"])
    op.create_index("ix_pol_mpn", "proactive_outreach_lines", ["mpn"])
    op.create_index("ix_pol_sent", "proactive_outreach_lines", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_pol_sent", table_name="proactive_outreach_lines")
    op.drop_index("ix_pol_mpn", table_name="proactive_outreach_lines")
    op.drop_index("ix_pol_company", table_name="proactive_outreach_lines")
    op.drop_index("ix_pol_sales", table_name="proactive_outreach_lines")
    op.drop_index("ix_pol_digest", table_name="proactive_outreach_lines")
    op.drop_table("proactive_outreach_lines")
    op.drop_index("ix_pdig_generated", table_name="proactive_digests")
    op.drop_index("ix_pdig_status", table_name="proactive_digests")
    op.drop_index("ix_pdig_sales", table_name="proactive_digests")
    op.drop_table("proactive_digests")
    op.drop_column("proactive_matches", "last_asked_qty")
    op.drop_column("proactive_matches", "last_asked_at")
    op.drop_column("proactive_matches", "requirement_count")
    op.drop_column("proactive_matches", "match_source")
