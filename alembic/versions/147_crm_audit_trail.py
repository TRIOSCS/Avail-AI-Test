"""Add created_by_id + modified_by_id audit trail to companies, customer_sites,
site_contacts (migration 147).

Revision ID: 147_crm_audit_trail
Revises: 146_req_win_probability
Create Date: 2026-06-24

Adds two nullable Integer FK columns (ondelete=SET NULL → users.id) to the three
core CRM entity tables.  Background-job / import writes leave these NULL.
Authenticated request writes get them populated by the before_insert /
before_update SQLAlchemy event listeners in app/audit_listeners.py.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "147_crm_audit_trail"
down_revision = "146_req_win_probability"
branch_labels = None
depends_on = None

_TABLES = ("companies", "customer_sites", "site_contacts")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("created_by_id", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("modified_by_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_created_by",
            table,
            "users",
            ["created_by_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            f"fk_{table}_modified_by",
            table,
            "users",
            ["modified_by_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(f"fk_{table}_modified_by", table, type_="foreignkey")
        op.drop_constraint(f"fk_{table}_created_by", table, type_="foreignkey")
        op.drop_column(table, "modified_by_id")
        op.drop_column(table, "created_by_id")
