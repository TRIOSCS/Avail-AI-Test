"""CRM P5 trust: crm_field_history table (per-record field-change audit trail).

Adds the ``crm_field_history`` table backing the field-DIFF history surfaced on
the CRM company History tab and the contact History modal. One row per inline
single-field edit that actually changed a value (old→new, field, who, when).
Polymorphic by (entity_type, entity_id) — 'company' | 'contact' — mirroring the
approval_requests subject pair so one table serves both CRM entities.

Distinct from companies/site_contacts.modified_by_id (migration 147), which only
records the latest editor; this keeps the full ordered per-field history.

Revision ID: 169_crm_field_history
Revises: 167_saved_views
"""

import sqlalchemy as sa

from alembic import op

revision = "169_crm_field_history"
down_revision = "167_saved_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crm_field_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["changed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_crm_field_history_entity",
        "crm_field_history",
        ["entity_type", "entity_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_crm_field_history_entity", table_name="crm_field_history")
    op.drop_table("crm_field_history")
