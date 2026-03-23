"""Float to Numeric monetary columns, add missing FK indexes, remove duplicate indexes,
add ondelete clauses.

Revision ID: 4724fcfde85e
Revises: fa1b90a20cf4
Create Date: 2026-03-23 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4724fcfde85e"
down_revision: Union[str, None] = "fa1b90a20cf4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Float → Numeric(12, 4) for monetary columns ---
    op.alter_column(
        "sightings",
        "unit_price",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "material_vendor_history",
        "last_price",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "proactive_matches",
        "customer_last_price",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "proactive_matches",
        "our_cost",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "vendor_cards",
        "total_revenue",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "material_price_snapshots",
        "price",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )

    # --- Add missing FK indexes ---
    op.create_index("ix_nc_search_log_queue_id", "nc_search_log", ["queue_id"])
    op.create_index("ix_req_attachments_req_id", "requisition_attachments", ["requisition_id"])
    op.create_index("ix_reqmt_attachments_reqmt_id", "requirement_attachments", ["requirement_id"])
    op.create_index("ix_notifications_ticket_id", "notifications", ["ticket_id"])

    # --- Remove duplicate index (keep unique constraint on column) ---
    op.drop_index("ix_ese_email", table_name="email_signature_extracts")

    # --- Add ondelete clauses to user FK columns ---
    # Offers
    _recreate_fk("offers", "entered_by_id", "users", "id", "SET NULL")
    _recreate_fk("offers", "promoted_by_id", "users", "id", "SET NULL")
    _recreate_fk("offers", "updated_by_id", "users", "id", "SET NULL")
    _recreate_fk("offers", "approved_by_id", "users", "id", "SET NULL")
    _recreate_fk("offer_attachments", "uploaded_by_id", "users", "id", "SET NULL")
    # Contacts
    _recreate_fk("contacts", "user_id", "users", "id", "CASCADE")
    # Vendor responses
    _recreate_fk("vendor_responses", "scanned_by_user_id", "users", "id", "SET NULL")
    # Quotes
    _recreate_fk("quotes", "created_by_id", "users", "id", "SET NULL")
    # Vendor reviews
    _recreate_fk("vendor_reviews", "user_id", "users", "id", "CASCADE")
    # Requisitions
    _recreate_fk("requisitions", "updated_by_id", "users", "id", "SET NULL")
    _recreate_fk("requisition_attachments", "uploaded_by_id", "users", "id", "SET NULL")
    _recreate_fk("requirement_attachments", "uploaded_by_id", "users", "id", "SET NULL")
    # CRM
    _recreate_fk("companies", "account_owner_id", "users", "id", "SET NULL")
    _recreate_fk("customer_sites", "owner_id", "users", "id", "SET NULL")


def downgrade() -> None:
    # --- Revert ondelete clauses (remove ondelete, back to RESTRICT default) ---
    _recreate_fk("customer_sites", "owner_id", "users", "id", None)
    _recreate_fk("companies", "account_owner_id", "users", "id", None)
    _recreate_fk("requirement_attachments", "uploaded_by_id", "users", "id", None)
    _recreate_fk("requisition_attachments", "uploaded_by_id", "users", "id", None)
    _recreate_fk("requisitions", "updated_by_id", "users", "id", None)
    _recreate_fk("vendor_reviews", "user_id", "users", "id", None)
    _recreate_fk("quotes", "created_by_id", "users", "id", None)
    _recreate_fk("vendor_responses", "scanned_by_user_id", "users", "id", None)
    _recreate_fk("contacts", "user_id", "users", "id", None)
    _recreate_fk("offer_attachments", "uploaded_by_id", "users", "id", None)
    _recreate_fk("offers", "approved_by_id", "users", "id", None)
    _recreate_fk("offers", "updated_by_id", "users", "id", None)
    _recreate_fk("offers", "promoted_by_id", "users", "id", None)
    _recreate_fk("offers", "entered_by_id", "users", "id", None)

    # --- Recreate duplicate index ---
    op.create_index("ix_ese_email", "email_signature_extracts", ["sender_email"], unique=True)

    # --- Drop added indexes ---
    op.drop_index("ix_notifications_ticket_id", table_name="notifications")
    op.drop_index("ix_reqmt_attachments_reqmt_id", table_name="requirement_attachments")
    op.drop_index("ix_req_attachments_req_id", table_name="requisition_attachments")
    op.drop_index("ix_nc_search_log_queue_id", table_name="nc_search_log")

    # --- Revert Numeric → Float ---
    op.alter_column(
        "material_price_snapshots",
        "price",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=True,
    )
    op.alter_column(
        "vendor_cards",
        "total_revenue",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=True,
    )
    op.alter_column(
        "proactive_matches",
        "our_cost",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=True,
    )
    op.alter_column(
        "proactive_matches",
        "customer_last_price",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=True,
    )
    op.alter_column(
        "material_vendor_history",
        "last_price",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=True,
    )
    op.alter_column(
        "sightings",
        "unit_price",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=True,
    )


def _recreate_fk(
    table: str,
    column: str,
    ref_table: str,
    ref_column: str,
    ondelete: str | None,
) -> None:
    """Drop and recreate a FK constraint with the specified ondelete behavior."""
    constraint_name = f"fk_{table}_{column}"
    # Try dropping with conventional name first; if it doesn't exist,
    # Alembic/PG will find it by column reference
    op.drop_constraint(constraint_name, table, type_="foreignkey")
    op.create_foreign_key(
        constraint_name,
        table,
        ref_table,
        [column],
        [ref_column],
        ondelete=ondelete,
    )
