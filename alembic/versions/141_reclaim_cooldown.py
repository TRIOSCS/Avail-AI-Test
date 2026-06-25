"""Add reclaim_blocked_until to prospect_accounts (Phase 4: compliance cooldown).

Revision ID: 141_reclaim_cooldown
Revises: 140_account_collaborators
Create Date: 2026-06-24

reclaim_blocked_until enforces a 30-day cooldown after an account is swept: the
former owner cannot reclaim it until this timestamp passes (see
prospect_reclamation.reclaim_prospect_account). Managers/admins bypass the cooldown
via the reassign endpoint, which also clears this column. Set at sweep time to
swept_at + 30 days.

Schema:
  - prospect_accounts.reclaim_blocked_until: timezone-aware DateTime, nullable

Downgrade: drops the column.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "141_reclaim_cooldown"
down_revision = "140_account_collaborators"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prospect_accounts",
        sa.Column("reclaim_blocked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prospect_accounts", "reclaim_blocked_until")
