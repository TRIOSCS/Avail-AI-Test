"""Add notification-preference columns to users (migration 151).

Revision ID: 151_user_notify_prefs
Revises: 150_user_mgmt
Create Date: 2026-06-24

Adds two Boolean NOT NULL columns (server_default true) to users for per-user
notification preferences. Tasks 7-9 of the settings-refine program wire these
to the Profile tab toggles that suppress buy-plan email and new-offer alerts.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "151_user_notify_prefs"
down_revision = "150_user_mgmt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "notify_buyplan_email_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "notify_new_offer_alert_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_new_offer_alert_enabled")
    op.drop_column("users", "notify_buyplan_email_enabled")
