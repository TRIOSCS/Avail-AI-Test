"""add_ondelete_and_missing_indexes.

Task 2.1: Add missing ondelete to FK columns (CASCADE or SET NULL).
Task 2.2: Add missing database indexes on frequently-queried columns.

Revision ID: add_ondelete_and_missing_indexes
Revises: restructure_substitutes_json
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "add_ondelete_and_missing_indexes"
down_revision: Union[str, None] = "restructure_substitutes_json"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Helper: swap a FK constraint's ondelete rule
# ---------------------------------------------------------------------------
def _swap_fk(
    table: str,
    constraint: str,
    column: str,
    ref_table: str,
    ref_col: str,
    new_ondelete: str,
    old_ondelete: str | None = None,
) -> None:
    """Drop old FK and recreate with new ondelete rule (upgrade direction)."""
    op.drop_constraint(constraint, table, type_="foreignkey")
    op.create_foreign_key(constraint, table, ref_table, [column], [ref_col], ondelete=new_ondelete)


def _restore_fk(
    table: str,
    constraint: str,
    column: str,
    ref_table: str,
    ref_col: str,
) -> None:
    """Restore FK with NO ACTION (downgrade direction)."""
    op.drop_constraint(constraint, table, type_="foreignkey")
    op.create_foreign_key(constraint, table, ref_table, [column], [ref_col])


# ---------------------------------------------------------------------------
# FK changes: (table, constraint_name, column, ref_table, ref_col, ondelete)
# ---------------------------------------------------------------------------
CASCADE_FKS = [
    ("vendor_reviews", "vendor_reviews_vendor_card_id_fkey", "vendor_card_id", "vendor_cards", "id"),
    (
        "material_vendor_history",
        "material_vendor_history_material_card_id_fkey",
        "material_card_id",
        "material_cards",
        "id",
    ),
    ("buyer_vendor_stats", "buyer_vendor_stats_user_id_fkey", "user_id", "users", "id"),
    ("buyer_vendor_stats", "buyer_vendor_stats_vendor_card_id_fkey", "vendor_card_id", "vendor_cards", "id"),
    ("email_intelligence", "email_intelligence_user_id_fkey", "user_id", "users", "id"),
    ("strategic_vendors", "strategic_vendors_user_id_fkey", "user_id", "users", "id"),
    ("strategic_vendors", "strategic_vendors_vendor_card_id_fkey", "vendor_card_id", "vendor_cards", "id"),
    (
        "material_price_snapshots",
        "material_price_snapshots_material_card_id_fkey",
        "material_card_id",
        "material_cards",
        "id",
    ),
]

SET_NULL_FKS = [
    ("proactive_offers", "proactive_offers_customer_site_id_fkey", "customer_site_id", "customer_sites", "id"),
    ("proactive_offers", "proactive_offers_salesperson_id_fkey", "salesperson_id", "users", "id"),
    (
        "proactive_throttle",
        "proactive_throttle_proactive_offer_id_fkey",
        "proactive_offer_id",
        "proactive_offers",
        "id",
    ),
    ("activity_log", "activity_log_company_id_fkey", "company_id", "companies", "id"),
    ("activity_log", "activity_log_vendor_card_id_fkey", "vendor_card_id", "vendor_cards", "id"),
    ("activity_log", "activity_log_vendor_contact_id_fkey", "vendor_contact_id", "vendor_contacts", "id"),
    ("activity_log", "activity_log_requisition_id_fkey", "requisition_id", "requisitions", "id"),
    ("activity_log", "activity_log_quote_id_fkey", "quote_id", "quotes", "id"),
    ("activity_log", "activity_log_customer_site_id_fkey", "customer_site_id", "customer_sites", "id"),
    ("sightings", "sightings_source_company_id_fkey", "source_company_id", "companies", "id"),
]

# ---------------------------------------------------------------------------
# Indexes to add: (index_name, table, columns)
# ---------------------------------------------------------------------------
NEW_INDEXES = [
    ("ix_pm_company", "proactive_matches", ["company_id"]),
    ("ix_requisitions_company", "requisitions", ["company_id"]),
    ("ix_ics_log_queue", "ics_search_log", ["queue_id"]),
    ("ix_discovery_batches_status", "discovery_batches", ["status"]),
    ("ix_discovery_batches_source_status", "discovery_batches", ["source", "status"]),
    ("ix_discovery_batches_started_at", "discovery_batches", ["started_at"]),
    ("ix_enrichment_runs_phase", "enrichment_runs", ["phase"]),
    ("ix_enrichment_runs_status", "enrichment_runs", ["status"]),
    ("ix_enrichment_runs_created_at", "enrichment_runs", ["created_at"]),
    ("ix_bidsol_status", "bid_solicitations", ["status"]),
    ("ix_ke_created_by", "knowledge_entries", ["created_by"]),
]


def upgrade() -> None:
    # Set a generous lock timeout so we don't hang on active connections
    op.execute(text("SET lock_timeout = '30s'"))

    # --- Task 2.1a: Make proactive_offers columns nullable for SET NULL ---
    op.alter_column("proactive_offers", "customer_site_id", nullable=True)
    op.alter_column("proactive_offers", "salesperson_id", nullable=True)

    # --- Task 2.1b: CASCADE FKs ---
    for table, constraint, col, ref_table, ref_col in CASCADE_FKS:
        _swap_fk(table, constraint, col, ref_table, ref_col, "CASCADE")

    # --- Task 2.1c: SET NULL FKs ---
    for table, constraint, col, ref_table, ref_col in SET_NULL_FKS:
        _swap_fk(table, constraint, col, ref_table, ref_col, "SET NULL")

    # --- Task 2.2: New indexes ---
    for ix_name, table, columns in NEW_INDEXES:
        op.create_index(ix_name, table, columns)


def downgrade() -> None:
    # --- Remove indexes ---
    for ix_name, table, _columns in reversed(NEW_INDEXES):
        op.drop_index(ix_name, table_name=table)

    # --- Restore SET NULL FKs to NO ACTION ---
    for table, constraint, col, ref_table, ref_col in SET_NULL_FKS:
        _restore_fk(table, constraint, col, ref_table, ref_col)

    # --- Restore CASCADE FKs to NO ACTION ---
    for table, constraint, col, ref_table, ref_col in CASCADE_FKS:
        _restore_fk(table, constraint, col, ref_table, ref_col)

    # --- Restore proactive_offers columns to NOT NULL ---
    op.alter_column("proactive_offers", "salesperson_id", nullable=False)
    op.alter_column("proactive_offers", "customer_site_id", nullable=False)
