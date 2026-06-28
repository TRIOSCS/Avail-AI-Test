"""CRM P4 power-UX: saved_views table (per-user list filter presets).

Adds the ``saved_views`` table backing the Saved Views control on the customers
(accounts) and contacts lists. Each row is one user's named filter preset for a
single list surface (``list_key`` = 'customers' | 'contacts'); ``filters`` holds
the whitelisted filter query-param dict as JSON. Unique per (user, list_key,
name) so re-saving a name overwrites in place.

Revision ID: 167_saved_views
Revises: 164_sp2_qp_sales_rename
"""

import sqlalchemy as sa

from alembic import op

revision = "167_saved_views"
down_revision = "164_sp2_qp_sales_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_views",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("list_key", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "list_key", "name", name="uq_saved_view_user_key_name"),
    )
    op.create_index("ix_saved_views_user_key", "saved_views", ["user_id", "list_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_saved_views_user_key", table_name="saved_views")
    op.drop_table("saved_views")
