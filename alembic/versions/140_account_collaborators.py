"""Add account_collaborators table (Phase 3: helper collaborator access).

Revision ID: 140_account_collaborators
Revises: 139_contact_is_active_default
Create Date: 2026-06-24

AccountCollaborator grants a user helper-level access to a CRM company:
  - can_manage_account() returns True → may view + work the account
  - can_manage_account_team() returns False → may NOT add/remove collaborators
    or reassign the primary owner (that requires account_owner or manager/admin)

Schema:
  - account_collaborators: id PK, company_id FK CASCADE, user_id FK CASCADE,
    role VARCHAR(20) default 'helper', created_at UTCDateTime
  - UNIQUE(company_id, user_id) — prevents duplicate assignment
  - INDEX ix_account_collaborators_company on company_id (for exists() queries)

Downgrade: drops the entire table.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "140_account_collaborators"
down_revision = "139_contact_is_active_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_collaborators",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="helper"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "user_id", name="uq_account_collaborators_company_user"),
    )
    op.create_index(
        "ix_account_collaborators_company",
        "account_collaborators",
        ["company_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_account_collaborators_company", table_name="account_collaborators")
    op.drop_table("account_collaborators")
