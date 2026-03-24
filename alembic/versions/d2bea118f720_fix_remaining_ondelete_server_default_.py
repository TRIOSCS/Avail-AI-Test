"""Fix remaining ondelete, server_default, indexes, and types.

Covers model changes not addressed by prior migrations:
- ondelete clauses on 20+ FK columns across buy_plan, config, enrichment,
  error_report, intelligence, offers, performance, prospect_account, sourcing
- server_default=func.now() on timestamp columns in excess, trouble_ticket,
  root_cause_group
- Missing indexes on enrichment (started_by, reviewed_by, saved_by),
  config (graph_subscriptions.user_id)
- ProactiveMatch: margin_pct Float->Numeric(5,2), customer_site_id/salesperson_id
  nullable for SET NULL
- ActivityLog: user_id nullable for SET NULL
- ProactiveDoNotOffer: created_by_id nullable for SET NULL

Revision ID: d2bea118f720
Revises: 94e03f64fb8a
Create Date: 2026-03-24 15:58:40.459165
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "d2bea118f720"
down_revision: Union[str, None] = "94e03f64fb8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _recreate_fk(
    table: str,
    column: str,
    ref_table: str,
    ref_column: str,
    ondelete: str | None,
) -> None:
    """Drop and recreate a FK constraint with the specified ondelete behavior."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            """
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_attribute att ON att.attrelid = con.conrelid
                AND att.attnum = ANY(con.conkey)
            WHERE rel.relname = :table
                AND att.attname = :column
                AND con.contype = 'f'
            LIMIT 1
            """
        ),
        {"table": table, "column": column},
    )
    row = result.fetchone()
    if row is None:
        # No existing FK — create fresh
        new_name = f"fk_{table}_{column}"
        op.create_foreign_key(new_name, table, ref_table, [column], [ref_column], ondelete=ondelete)
        return
    old_name = row[0]
    new_name = f"fk_{table}_{column}"
    op.drop_constraint(old_name, table, type_="foreignkey")
    op.create_foreign_key(new_name, table, ref_table, [column], [ref_column], ondelete=ondelete)


def upgrade() -> None:
    op.execute(text("SET lock_timeout = '30s'"))

    # -----------------------------------------------------------------------
    # 1. Nullable changes (must come before FK recreation)
    # -----------------------------------------------------------------------
    op.alter_column("proactive_matches", "customer_site_id", nullable=True)
    op.alter_column("proactive_matches", "salesperson_id", nullable=True)
    op.alter_column("activity_log", "user_id", nullable=True)
    op.alter_column("proactive_do_not_offer", "created_by_id", nullable=True)

    # -----------------------------------------------------------------------
    # 2. Type change: ProactiveMatch.margin_pct Float → Numeric(5, 2)
    # -----------------------------------------------------------------------
    op.alter_column(
        "proactive_matches",
        "margin_pct",
        existing_type=sa.Float(),
        type_=sa.Numeric(5, 2),
        existing_nullable=True,
    )

    # -----------------------------------------------------------------------
    # 3. ondelete SET NULL (nullable FK columns)
    # -----------------------------------------------------------------------
    SET_NULL_FKS = [
        # buy_plan.py
        ("buy_plans_v3", "approved_by_id", "users", "id"),
        ("buy_plans_v3", "so_verified_by_id", "users", "id"),
        ("buy_plans_v3", "submitted_by_id", "users", "id"),
        ("buy_plans_v3", "cancelled_by_id", "users", "id"),
        ("buy_plans_v3", "halted_by_id", "users", "id"),
        ("buy_plan_lines", "buyer_id", "users", "id"),
        ("buy_plan_lines", "po_verified_by_id", "users", "id"),
        # enrichment.py
        ("enrichment_jobs", "started_by_id", "users", "id"),
        ("enrichment_queue", "reviewed_by_id", "users", "id"),
        ("prospect_contacts", "saved_by_id", "users", "id"),
        # error_report.py
        ("error_reports", "resolved_by_id", "users", "id"),
        # intelligence.py
        ("proactive_matches", "customer_site_id", "customer_sites", "id"),
        ("proactive_matches", "salesperson_id", "users", "id"),
        ("proactive_offers", "converted_requisition_id", "requisitions", "id"),
        ("proactive_offers", "converted_quote_id", "quotes", "id"),
        ("proactive_do_not_offer", "created_by_id", "users", "id"),
        ("change_log", "user_id", "users", "id"),
        ("activity_log", "user_id", "users", "id"),
        ("activity_log", "buy_plan_id", "buy_plans_v3", "id"),
        # offers.py (VendorResponse)
        ("vendor_responses", "contact_id", "contacts", "id"),
        ("vendor_responses", "requisition_id", "requisitions", "id"),
        # performance.py (StockListHash)
        ("stock_list_hashes", "vendor_card_id", "vendor_cards", "id"),
        # prospect_account.py
        ("prospect_accounts", "discovery_batch_id", "discovery_batches", "id"),
        ("prospect_accounts", "claimed_by", "users", "id"),
        ("prospect_accounts", "dismissed_by", "users", "id"),
        ("prospect_accounts", "company_id", "companies", "id"),
        # sourcing.py
        ("requisitions", "cloned_from_id", "requisitions", "id"),
    ]

    for table, column, ref_table, ref_col in SET_NULL_FKS:
        _recreate_fk(table, column, ref_table, ref_col, "SET NULL")

    # -----------------------------------------------------------------------
    # 4. ondelete CASCADE (NOT NULL FK columns)
    # -----------------------------------------------------------------------
    CASCADE_FKS = [
        # config.py
        ("graph_subscriptions", "user_id", "users", "id"),
        # error_report.py
        ("error_reports", "user_id", "users", "id"),
        # performance.py (StockListHash)
        ("stock_list_hashes", "user_id", "users", "id"),
    ]

    for table, column, ref_table, ref_col in CASCADE_FKS:
        _recreate_fk(table, column, ref_table, ref_col, "CASCADE")

    # -----------------------------------------------------------------------
    # 5. server_default on timestamp columns
    # -----------------------------------------------------------------------
    TIMESTAMP_DEFAULTS = [
        ("excess_lists", "created_at"),
        ("excess_lists", "updated_at"),
        ("excess_line_items", "created_at"),
        ("excess_line_items", "updated_at"),
        ("bids", "created_at"),
        ("bids", "updated_at"),
        ("bid_solicitations", "created_at"),
        ("trouble_tickets", "created_at"),
        ("trouble_tickets", "updated_at"),
        ("root_cause_groups", "created_at"),
        ("root_cause_groups", "updated_at"),
    ]

    for table, column in TIMESTAMP_DEFAULTS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(),
            server_default=sa.text("now()"),
        )

    # -----------------------------------------------------------------------
    # 6. Missing indexes
    # -----------------------------------------------------------------------
    NEW_INDEXES = [
        ("ix_ej_started_by", "enrichment_jobs", ["started_by_id"]),
        ("ix_eq_reviewed_by", "enrichment_queue", ["reviewed_by_id"]),
        ("ix_prospect_contacts_saved_by", "prospect_contacts", ["saved_by_id"]),
        ("ix_graph_subscriptions_user_id", "graph_subscriptions", ["user_id"]),
    ]

    for ix_name, table, columns in NEW_INDEXES:
        op.create_index(ix_name, table, columns)


def downgrade() -> None:
    # -----------------------------------------------------------------------
    # 6. Drop indexes
    # -----------------------------------------------------------------------
    op.drop_index("ix_graph_subscriptions_user_id", table_name="graph_subscriptions")
    op.drop_index("ix_prospect_contacts_saved_by", table_name="prospect_contacts")
    op.drop_index("ix_eq_reviewed_by", table_name="enrichment_queue")
    op.drop_index("ix_ej_started_by", table_name="enrichment_jobs")

    # -----------------------------------------------------------------------
    # 5. Remove server_default on timestamp columns
    # -----------------------------------------------------------------------
    TIMESTAMP_DEFAULTS = [
        ("root_cause_groups", "updated_at"),
        ("root_cause_groups", "created_at"),
        ("trouble_tickets", "updated_at"),
        ("trouble_tickets", "created_at"),
        ("bid_solicitations", "created_at"),
        ("bids", "updated_at"),
        ("bids", "created_at"),
        ("excess_line_items", "updated_at"),
        ("excess_line_items", "created_at"),
        ("excess_lists", "updated_at"),
        ("excess_lists", "created_at"),
    ]

    for table, column in TIMESTAMP_DEFAULTS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(),
            server_default=None,
        )

    # -----------------------------------------------------------------------
    # 4. Remove ondelete CASCADE (restore NO ACTION)
    # -----------------------------------------------------------------------
    CASCADE_FKS = [
        ("stock_list_hashes", "user_id", "users", "id"),
        ("error_reports", "user_id", "users", "id"),
        ("graph_subscriptions", "user_id", "users", "id"),
    ]

    for table, column, ref_table, ref_col in CASCADE_FKS:
        _recreate_fk(table, column, ref_table, ref_col, None)

    # -----------------------------------------------------------------------
    # 3. Remove ondelete SET NULL (restore NO ACTION)
    # -----------------------------------------------------------------------
    SET_NULL_FKS = [
        ("requisitions", "cloned_from_id", "requisitions", "id"),
        ("prospect_accounts", "company_id", "companies", "id"),
        ("prospect_accounts", "dismissed_by", "users", "id"),
        ("prospect_accounts", "claimed_by", "users", "id"),
        ("prospect_accounts", "discovery_batch_id", "discovery_batches", "id"),
        ("stock_list_hashes", "vendor_card_id", "vendor_cards", "id"),
        ("vendor_responses", "requisition_id", "requisitions", "id"),
        ("vendor_responses", "contact_id", "contacts", "id"),
        ("activity_log", "buy_plan_id", "buy_plans_v3", "id"),
        ("activity_log", "user_id", "users", "id"),
        ("change_log", "user_id", "users", "id"),
        ("proactive_do_not_offer", "created_by_id", "users", "id"),
        ("proactive_offers", "converted_quote_id", "quotes", "id"),
        ("proactive_offers", "converted_requisition_id", "requisitions", "id"),
        ("proactive_matches", "salesperson_id", "users", "id"),
        ("proactive_matches", "customer_site_id", "customer_sites", "id"),
        ("error_reports", "resolved_by_id", "users", "id"),
        ("prospect_contacts", "saved_by_id", "users", "id"),
        ("enrichment_queue", "reviewed_by_id", "users", "id"),
        ("enrichment_jobs", "started_by_id", "users", "id"),
        ("buy_plan_lines", "po_verified_by_id", "users", "id"),
        ("buy_plan_lines", "buyer_id", "users", "id"),
        ("buy_plans_v3", "halted_by_id", "users", "id"),
        ("buy_plans_v3", "cancelled_by_id", "users", "id"),
        ("buy_plans_v3", "submitted_by_id", "users", "id"),
        ("buy_plans_v3", "so_verified_by_id", "users", "id"),
        ("buy_plans_v3", "approved_by_id", "users", "id"),
    ]

    for table, column, ref_table, ref_col in SET_NULL_FKS:
        _recreate_fk(table, column, ref_table, ref_col, None)

    # -----------------------------------------------------------------------
    # 2. Revert type: Numeric(5, 2) → Float
    # -----------------------------------------------------------------------
    op.alter_column(
        "proactive_matches",
        "margin_pct",
        existing_type=sa.Numeric(5, 2),
        type_=sa.Float(),
        existing_nullable=True,
    )

    # -----------------------------------------------------------------------
    # 1. Revert nullable changes
    # -----------------------------------------------------------------------
    op.alter_column("proactive_do_not_offer", "created_by_id", nullable=False)
    op.alter_column("activity_log", "user_id", nullable=False)
    op.alter_column("proactive_matches", "salesperson_id", nullable=False)
    op.alter_column("proactive_matches", "customer_site_id", nullable=False)
