"""Add GraphSubscription health tracking columns.

What: adds graph_subscriptions.last_renewed_at (nullable UTCDateTime),
      renew_fail_count (Integer NOT NULL default 0), last_error (String nullable)
      so subscription renewal failures are durable and observable.
Downgrade: drops the three columns.

Revision ID: 115_subscription_health
Revises: 114_contact_sent_at
Create Date: 2026-06-18
"""

import sqlalchemy as sa

from alembic import op

revision = "115_subscription_health"
down_revision = "114_contact_sent_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "graph_subscriptions",
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "graph_subscriptions",
        sa.Column(
            "renew_fail_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "graph_subscriptions",
        sa.Column("last_error", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("graph_subscriptions", "last_error")
    op.drop_column("graph_subscriptions", "renew_fail_count")
    op.drop_column("graph_subscriptions", "last_renewed_at")
