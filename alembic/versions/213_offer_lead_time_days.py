"""213_offer_lead_time_days — normalized lead-time-in-days sibling of Offer.lead_time.

Offer.lead_time is free text ("2-3wk", "stock", "4-6 weeks"), so offers can't be
sorted or filtered by lead time. This adds a normalized integer companion populated
deterministically by normalize_lead_time at offer save, plus a nightly backfill that
uses AI only for the free-text residue the deterministic parser can't resolve. The
boolean flag marks AI-sourced values so the UI can show the "AI — verify" chip.

Additive + reversible (downgrade drops both). Round-tripped on throwaway PG.
Chains onto 212_worker_singleton_checks.

Revision ID: 213_offer_lead_time_days
Revises: 212_worker_singleton_checks
"""

import sqlalchemy as sa

from alembic import op

revision = "213_offer_lead_time_days"
down_revision = "212_worker_singleton_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("offers", sa.Column("lead_time_days", sa.Integer(), nullable=True))
    op.add_column(
        "offers",
        sa.Column("lead_time_days_ai", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # When the nightly backfill's AI tier tried a row (success OR decline), stamp this
    # so a permanently-unparseable lead time is never re-billed to Claude every night.
    op.add_column("offers", sa.Column("lead_time_ai_attempted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("offers", "lead_time_ai_attempted_at")
    op.drop_column("offers", "lead_time_days_ai")
    op.drop_column("offers", "lead_time_days")
