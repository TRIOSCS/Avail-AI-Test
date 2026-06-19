"""Add alert_seen table — per-user read-state for cross-app alerts.

What: creates alert_seen (user_id, alert_kind, ref_id, seen_at) with a unique
      constraint on (user_id, alert_kind, ref_id) and an index on (user_id, alert_kind).
      Drives FYI alert badge exclusion + one-time in-tab spotlight pulse suppression.
Downgrade: drops the table.

Revision ID: 119_alert_seen
Revises: 118_company_disposition
Create Date: 2026-06-18

Re-numbered 117->118->119 across origin/main merges: feat/datasheet-company-library (117)
and feat/crm-disposition (118) merged first, so this re-chains onto 118_company_disposition.
"""

import sqlalchemy as sa

from alembic import op

revision = "119_alert_seen"
down_revision = "118_company_disposition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_seen",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("alert_kind", sa.String(length=40), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "alert_kind", "ref_id", name="uq_alert_seen_user_kind_ref"),
    )
    op.create_index("ix_alert_seen_user_kind", "alert_seen", ["user_id", "alert_kind"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_alert_seen_user_kind", table_name="alert_seen")
    op.drop_table("alert_seen")
