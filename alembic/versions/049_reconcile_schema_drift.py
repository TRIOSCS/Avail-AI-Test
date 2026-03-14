"""049: Reconcile schema drift between models and production DB.

After migration 048 absorbed startup.py DDL, autogenerate still found
~80 cosmetic differences. This migration resolves them so future
autogenerate runs produce empty output.

Changes:
- Drop orphan table: inventory_snapshots
- Drop 12 orphan columns (Salesforce sf_*, Acctivate acctivate_*, last_synced_at)
- Replace ~40 old indexes with model-defined equivalents
- Add/fix FK ondelete rules (CASCADE, SET NULL)
- Add missing unique constraints and indexes
- No data loss — all dropped columns/tables are unused legacy

Revision ID: 049
Revises: 048
Create Date: 2026-03-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "049"
down_revision: Union[str, None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── BUCKET 4: Drop orphan table ──
    op.drop_index("ix_inv_product_warehouse", table_name="inventory_snapshots", if_exists=True)
    op.drop_index("ix_inventory_snapshots_product_id", table_name="inventory_snapshots", if_exists=True)
    op.drop_table("inventory_snapshots", if_exists=True)

    # ── BUCKET 3: Drop orphan columns ──
    op.drop_column("material_cards", "sf_material_id", if_exists=True)
    op.drop_column("requirements", "sf_req_item_id", if_exists=True)
    op.drop_column("requisitions", "sf_requisition_id", if_exists=True)
    op.drop_column("vendor_cards", "acctivate_vendor_id", if_exists=True)
    op.drop_column("vendor_cards", "acctivate_last_order_date", if_exists=True)
    op.drop_column("vendor_cards", "acctivate_total_units", if_exists=True)
    op.drop_column("vendor_cards", "acctivate_total_orders", if_exists=True)
    op.drop_column("vendor_cards", "sf_account_id", if_exists=True)
    op.drop_column("vendor_cards", "last_synced_at", if_exists=True)
    op.drop_column("material_vendor_history", "acctivate_last_price", if_exists=True)
    op.drop_column("material_vendor_history", "acctivate_rma_rate", if_exists=True)
    op.drop_column("material_vendor_history", "acctivate_last_date", if_exists=True)

    # ── BUCKET 1: Index reconciliation — activity_log ──
    op.drop_index("ix_activity_log_customer_site_id", table_name="activity_log", if_exists=True)
    op.drop_index("ix_activity_log_site_contact_id", table_name="activity_log", if_exists=True)
    op.drop_index("ix_activity_unmatched", table_name="activity_log", if_exists=True)
    op.create_index(
        "ix_activity_external",
        "activity_log",
        ["external_id"],
        unique=False,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index(
        "ix_activity_site_contact",
        "activity_log",
        ["site_contact_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("site_contact_id IS NOT NULL"),
    )
    op.create_index(
        "ix_activity_user_notif",
        "activity_log",
        ["user_id", "activity_type", "created_at"],
        unique=False,
        postgresql_where=sa.text("dismissed_at IS NULL"),
    )

    # ── BUCKET 1: Index reconciliation — buy_plans ──
    op.drop_index("ix_bp_approved_by", table_name="buy_plans", if_exists=True)
    op.drop_index("ix_bp_cancelled_by", table_name="buy_plans", if_exists=True)
    op.drop_index("ix_bp_completed_by", table_name="buy_plans", if_exists=True)
    op.create_index("ix_buyplans_token", "buy_plans", ["approval_token"], unique=False)

    # ── BUCKET 1: Index reconciliation — companies ──
    op.drop_index("ix_companies_owner", table_name="companies", if_exists=True)
    op.create_index("ix_companies_account_owner", "companies", ["account_owner_id"], unique=False)
    op.create_index("ix_companies_owner_created", "companies", ["account_owner_id", "created_at"], unique=False)
    op.create_unique_constraint("uq_companies_sf_account_id", "companies", ["sf_account_id"])

    # ── BUCKET 1: Index reconciliation — contacts ──
    op.drop_index("ix_contact_conv_id", table_name="contacts", if_exists=True)
    op.drop_index("ix_contacts_vendor_name", table_name="contacts", if_exists=True)
    op.create_index("ix_contact_type_created", "contacts", ["contact_type", "created_at"], unique=False)
    op.create_index("ix_contact_type_vendor", "contacts", ["contact_type", "vendor_name"], unique=False)

    # ── BUCKET 1: Index reconciliation — email_signature_extracts ──
    op.create_index("ix_ese_email", "email_signature_extracts", ["sender_email"], unique=True)

    # ── BUCKET 1: Index reconciliation — enrichment_jobs, enrichment_queue ──
    op.drop_index("ix_ej_started_by", table_name="enrichment_jobs", if_exists=True)
    op.drop_index("ix_eq_reviewed_by", table_name="enrichment_queue", if_exists=True)
    op.drop_index("ix_eq_vendor_contact", table_name="enrichment_queue", if_exists=True)
    op.create_index("ix_eq_status_source", "enrichment_queue", ["status", "source"], unique=False)

    # ── BUCKET 1: Index reconciliation — error_reports, material_card_audit, material_cards ──
    op.drop_index("ix_er_resolved_by", table_name="error_reports", if_exists=True)
    op.drop_index("ix_mca_material_card_id", table_name="material_card_audit", if_exists=True)
    op.drop_index("ix_mca_normalized_mpn", table_name="material_card_audit", if_exists=True)
    op.drop_index("ix_mc_internal_part", table_name="material_cards", if_exists=True)

    # ── BUCKET 1: Index reconciliation — notifications ──
    op.drop_index("ix_notifications_user_unread", table_name="notifications", if_exists=True)
    op.create_index("ix_notifications_id", "notifications", ["id"], unique=False)

    # ── BUCKET 1: Index reconciliation — offer_attachments, offers ──
    op.drop_index("ix_oa_uploaded_by", table_name="offer_attachments", if_exists=True)
    op.drop_index("ix_offers_approved_by_id", table_name="offers", if_exists=True)
    op.drop_index("ix_offers_status_created", table_name="offers", if_exists=True)
    op.drop_index("ix_offers_updated_by_id", table_name="offers", if_exists=True)
    op.drop_index("ix_offers_vendor_card", table_name="offers", if_exists=True)
    op.drop_index("ix_offers_vr", table_name="offers", if_exists=True)
    op.create_index("ix_offers_entered_created", "offers", ["entered_by_id", "created_at"], unique=False)
    op.create_index("ix_offers_req_created", "offers", ["requisition_id", "created_at"], unique=False)
    op.create_index("ix_offers_req_status", "offers", ["requisition_id", "status"], unique=False)
    op.create_index("ix_offers_status", "offers", ["status"], unique=False)

    # ── BUCKET 1: Index reconciliation — pending_batches ──
    op.drop_index("ix_pending_batches_status", table_name="pending_batches", if_exists=True)
    op.create_index("ix_pending_batches_status", "pending_batches", ["status", "submitted_at"], unique=False)
    op.create_index("ix_pending_batches_batch_id", "pending_batches", ["batch_id"], unique=False)

    # ── BUCKET 1: Index reconciliation — proactive_*, prospect_contacts ──
    op.drop_index("ix_pm_requirement", table_name="proactive_matches", if_exists=True)
    op.drop_index("ix_poff_conv_quote", table_name="proactive_offers", if_exists=True)
    op.drop_index("ix_poff_conv_req", table_name="proactive_offers", if_exists=True)
    op.drop_index("ix_poff_status_sent", table_name="proactive_offers", if_exists=True)
    op.drop_index("ix_pt_offer", table_name="proactive_throttle", if_exists=True)
    op.drop_index("ix_pc_saved_by", table_name="prospect_contacts", if_exists=True)

    # ── BUCKET 1: Index reconciliation — requirements ──
    op.drop_index("ix_requirements_mpn_trgm", table_name="requirements", if_exists=True)
    op.drop_index("ix_requirements_primary_mpn", table_name="requirements", if_exists=True)
    op.drop_index("ix_requirements_subs_trgm", table_name="requirements", if_exists=True)
    op.create_index("ix_req_primary_mpn", "requirements", ["primary_mpn"], unique=False)

    # ── BUCKET 1: Index reconciliation — requisitions ──
    op.drop_index("ix_req_cloned_from", table_name="requisitions", if_exists=True)
    op.drop_index("ix_req_created_by", table_name="requisitions", if_exists=True)
    op.drop_index("ix_req_site", table_name="requisitions", if_exists=True)
    op.drop_index("ix_requisitions_customer_name_trgm", table_name="requisitions", if_exists=True)
    op.drop_index("ix_requisitions_name_trgm", table_name="requisitions", if_exists=True)
    op.drop_index("ix_requisitions_updated_by_id", table_name="requisitions", if_exists=True)
    op.create_index("ix_requisitions_created_at", "requisitions", ["created_at"], unique=False)
    op.create_index("ix_requisitions_created_by", "requisitions", ["created_by"], unique=False)
    op.create_index("ix_requisitions_customer_name", "requisitions", ["customer_name"], unique=False)
    op.create_index("ix_requisitions_name", "requisitions", ["name"], unique=False)
    op.create_index("ix_requisitions_site", "requisitions", ["customer_site_id"], unique=False)
    op.create_index("ix_requisitions_status", "requisitions", ["status"], unique=False)

    # ── BUCKET 1: Index reconciliation — sightings ──
    op.create_index(
        "ix_sightings_req_score", "sightings", ["requirement_id", sa.literal_column("score DESC")], unique=False
    )
    op.create_index("ix_sightings_req_vendor", "sightings", ["requirement_id", "vendor_name"], unique=False)
    op.create_index("ix_sightings_vendor_name", "sightings", ["vendor_name"], unique=False)
    op.create_index("ix_sightings_vendor_name_normalized", "sightings", ["vendor_name_normalized"], unique=False)

    # ── BUCKET 1: Index reconciliation — site_contacts, system_config ──
    op.create_index("ix_site_contacts_email", "site_contacts", ["email"], unique=False)
    op.drop_constraint("system_config_key_key", "system_config", type_="unique")
    op.create_index("ix_system_config_key", "system_config", ["key"], unique=True)

    # ── BUCKET 1: Index reconciliation — vendor_cards (also drop orphan indexes) ──
    op.drop_index("ix_vc_fts", table_name="vendor_cards", if_exists=True)
    op.drop_index("ix_vendor_cards_acctivate_vendor_id", table_name="vendor_cards", if_exists=True)
    op.drop_index("ix_vendor_cards_blacklisted", table_name="vendor_cards", if_exists=True)
    op.drop_index("ix_vendor_cards_name_trgm", table_name="vendor_cards", if_exists=True)
    op.drop_index("ix_vendor_cards_sf_account_id", table_name="vendor_cards", if_exists=True)
    op.create_index("ix_vendor_cards_created_at", "vendor_cards", ["created_at"], unique=False)

    # ── BUCKET 1: Index reconciliation — vendor_contacts, vendor_metrics_snapshot ──
    op.create_index("ix_vendor_contacts_email", "vendor_contacts", ["email"], unique=False)
    op.create_index("ix_vms_composite", "vendor_metrics_snapshot", ["composite_score"], unique=False)
    op.create_index("ix_vms_date", "vendor_metrics_snapshot", ["snapshot_date"], unique=False)

    # ── BUCKET 1: Index reconciliation — vendor_responses ──
    op.drop_index("ix_vendor_responses_vendor_name", table_name="vendor_responses", if_exists=True)
    op.create_index("ix_vr_classification", "vendor_responses", ["classification"], unique=False)
    op.create_index("ix_vr_contact", "vendor_responses", ["contact_id"], unique=False)
    op.create_index("ix_vr_received_email", "vendor_responses", ["received_at", "vendor_email"], unique=False)
    op.create_index("ix_vr_req_email", "vendor_responses", ["requisition_id", "vendor_email"], unique=False)
    op.create_index("ix_vr_scanned_by", "vendor_responses", ["scanned_by_user_id"], unique=False)
    op.create_index("ix_vr_vendor_name", "vendor_responses", ["vendor_name"], unique=False)

    # ── BUCKET 1: Index reconciliation — vendor_reviews ──
    op.drop_index("ix_vrev_user", table_name="vendor_reviews", if_exists=True)
    op.create_index("ix_review_user", "vendor_reviews", ["user_id"], unique=False)

    # ── BUCKET 6: FK constraint upgrades — add ondelete rules ──
    op.drop_constraint("activity_log_site_contact_id_fkey", "activity_log", type_="foreignkey")
    op.create_foreign_key("fk_activity_log_quote", "activity_log", "quotes", ["quote_id"], ["id"])
    op.create_foreign_key(
        "fk_activity_log_site_contact",
        "activity_log",
        "site_contacts",
        ["site_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint("contacts_requisition_id_fkey", "contacts", type_="foreignkey")
    op.create_foreign_key(
        "fk_contacts_requisition", "contacts", "requisitions", ["requisition_id"], ["id"], ondelete="CASCADE"
    )

    op.drop_constraint("offers_requirement_id_fkey", "offers", type_="foreignkey")
    op.create_foreign_key(
        "fk_offers_requirement", "offers", "requirements", ["requirement_id"], ["id"], ondelete="CASCADE"
    )

    op.drop_constraint("requirements_requisition_id_fkey", "requirements", type_="foreignkey")
    op.create_foreign_key(
        "fk_requirements_requisition", "requirements", "requisitions", ["requisition_id"], ["id"], ondelete="CASCADE"
    )

    op.drop_constraint("requisitions_customer_site_id_fkey", "requisitions", type_="foreignkey")
    op.drop_constraint("requisitions_created_by_fkey", "requisitions", type_="foreignkey")
    op.create_foreign_key(
        "fk_requisitions_site", "requisitions", "customer_sites", ["customer_site_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_requisitions_created_by", "requisitions", "users", ["created_by"], ["id"], ondelete="SET NULL"
    )

    op.drop_constraint("sightings_requirement_id_fkey", "sightings", type_="foreignkey")
    op.create_foreign_key(
        "fk_sightings_requirement", "sightings", "requirements", ["requirement_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    # ── Reverse FK constraint changes ──
    op.drop_constraint("fk_sightings_requirement", "sightings", type_="foreignkey")
    op.create_foreign_key("sightings_requirement_id_fkey", "sightings", "requirements", ["requirement_id"], ["id"])

    op.drop_constraint("fk_requisitions_created_by", "requisitions", type_="foreignkey")
    op.drop_constraint("fk_requisitions_site", "requisitions", type_="foreignkey")
    op.create_foreign_key("requisitions_created_by_fkey", "requisitions", "users", ["created_by"], ["id"])
    op.create_foreign_key(
        "requisitions_customer_site_id_fkey", "requisitions", "customer_sites", ["customer_site_id"], ["id"]
    )

    op.drop_constraint("fk_requirements_requisition", "requirements", type_="foreignkey")
    op.create_foreign_key(
        "requirements_requisition_id_fkey", "requirements", "requisitions", ["requisition_id"], ["id"]
    )

    op.drop_constraint("fk_offers_requirement", "offers", type_="foreignkey")
    op.create_foreign_key("offers_requirement_id_fkey", "offers", "requirements", ["requirement_id"], ["id"])

    op.drop_constraint("fk_contacts_requisition", "contacts", type_="foreignkey")
    op.create_foreign_key("contacts_requisition_id_fkey", "contacts", "requisitions", ["requisition_id"], ["id"])

    op.drop_constraint("fk_activity_log_site_contact", "activity_log", type_="foreignkey")
    op.drop_constraint("fk_activity_log_quote", "activity_log", type_="foreignkey")
    op.create_foreign_key(
        "activity_log_site_contact_id_fkey", "activity_log", "site_contacts", ["site_contact_id"], ["id"]
    )

    # ── Reverse index changes (restore old indexes, drop new) ──
    op.drop_index("ix_review_user", table_name="vendor_reviews")
    op.create_index("ix_vrev_user", "vendor_reviews", ["user_id"], unique=False)

    op.drop_index("ix_vr_vendor_name", table_name="vendor_responses")
    op.drop_index("ix_vr_scanned_by", table_name="vendor_responses")
    op.drop_index("ix_vr_req_email", table_name="vendor_responses")
    op.drop_index("ix_vr_received_email", table_name="vendor_responses")
    op.drop_index("ix_vr_contact", table_name="vendor_responses")
    op.drop_index("ix_vr_classification", table_name="vendor_responses")
    op.create_index("ix_vendor_responses_vendor_name", "vendor_responses", ["vendor_name"], unique=False)

    op.drop_index("ix_vms_date", table_name="vendor_metrics_snapshot")
    op.drop_index("ix_vms_composite", table_name="vendor_metrics_snapshot")
    op.drop_index("ix_vendor_contacts_email", table_name="vendor_contacts")
    op.drop_index("ix_vendor_cards_created_at", table_name="vendor_cards")

    op.drop_index("ix_site_contacts_email", table_name="site_contacts")
    op.drop_index("ix_system_config_key", table_name="system_config")
    op.create_unique_constraint("system_config_key_key", "system_config", ["key"])

    op.drop_index("ix_sightings_vendor_name_normalized", table_name="sightings")
    op.drop_index("ix_sightings_vendor_name", table_name="sightings")
    op.drop_index("ix_sightings_req_vendor", table_name="sightings")
    op.drop_index("ix_sightings_req_score", table_name="sightings")

    op.drop_index("ix_requisitions_status", table_name="requisitions")
    op.drop_index("ix_requisitions_site", table_name="requisitions")
    op.drop_index("ix_requisitions_name", table_name="requisitions")
    op.drop_index("ix_requisitions_customer_name", table_name="requisitions")
    op.drop_index("ix_requisitions_created_by", table_name="requisitions")
    op.drop_index("ix_requisitions_created_at", table_name="requisitions")
    op.create_index("ix_requisitions_updated_by_id", "requisitions", ["updated_by_id"], unique=False)
    op.create_index("ix_req_site", "requisitions", ["customer_site_id"], unique=False)
    op.create_index("ix_req_created_by", "requisitions", ["created_by"], unique=False)
    op.create_index("ix_req_cloned_from", "requisitions", ["cloned_from_id"], unique=False)

    op.drop_index("ix_req_primary_mpn", table_name="requirements")

    op.create_index("ix_pc_saved_by", "prospect_contacts", ["saved_by_id"], unique=False)
    op.create_index("ix_pt_offer", "proactive_throttle", ["proactive_offer_id"], unique=False)
    op.create_index("ix_poff_status_sent", "proactive_offers", ["status", "sent_at"], unique=False)
    op.create_index("ix_poff_conv_req", "proactive_offers", ["converted_requisition_id"], unique=False)
    op.create_index("ix_poff_conv_quote", "proactive_offers", ["converted_quote_id"], unique=False)
    op.create_index("ix_pm_requirement", "proactive_matches", ["requirement_id"], unique=False)

    op.drop_index("ix_pending_batches_batch_id", table_name="pending_batches")
    op.drop_index("ix_pending_batches_status", table_name="pending_batches")
    op.create_index("ix_pending_batches_status", "pending_batches", ["status"], unique=False)

    op.drop_index("ix_offers_status", table_name="offers")
    op.drop_index("ix_offers_req_status", table_name="offers")
    op.drop_index("ix_offers_req_created", table_name="offers")
    op.drop_index("ix_offers_entered_created", table_name="offers")
    op.create_index("ix_offers_vr", "offers", ["vendor_response_id"], unique=False)
    op.create_index("ix_offers_vendor_card", "offers", ["vendor_card_id"], unique=False)
    op.create_index("ix_offers_updated_by_id", "offers", ["updated_by_id"], unique=False)
    op.create_index("ix_offers_status_created", "offers", ["status", "created_at"], unique=False)
    op.create_index("ix_offers_approved_by_id", "offers", ["approved_by_id"], unique=False)
    op.create_index("ix_oa_uploaded_by", "offer_attachments", ["uploaded_by_id"], unique=False)

    op.drop_index("ix_notifications_id", table_name="notifications")
    op.create_index("ix_notifications_user_unread", "notifications", ["user_id", "is_read"], unique=False)

    op.create_index("ix_mc_internal_part", "material_cards", ["is_internal_part"], unique=False)
    op.create_index("ix_mca_normalized_mpn", "material_card_audit", ["normalized_mpn"], unique=False)
    op.create_index("ix_mca_material_card_id", "material_card_audit", ["material_card_id"], unique=False)
    op.create_index("ix_er_resolved_by", "error_reports", ["resolved_by_id"], unique=False)

    op.drop_index("ix_eq_status_source", table_name="enrichment_queue")
    op.create_index("ix_eq_vendor_contact", "enrichment_queue", ["vendor_contact_id"], unique=False)
    op.create_index("ix_eq_reviewed_by", "enrichment_queue", ["reviewed_by_id"], unique=False)
    op.create_index("ix_ej_started_by", "enrichment_jobs", ["started_by_id"], unique=False)

    op.drop_index("ix_ese_email", table_name="email_signature_extracts")

    op.drop_index("ix_contact_type_vendor", table_name="contacts")
    op.drop_index("ix_contact_type_created", table_name="contacts")
    op.create_index("ix_contacts_vendor_name", "contacts", ["vendor_name"], unique=False)
    op.create_index("ix_contact_conv_id", "contacts", ["graph_conversation_id"], unique=False)

    op.drop_constraint("uq_companies_sf_account_id", "companies", type_="unique")
    op.drop_index("ix_companies_owner_created", table_name="companies")
    op.drop_index("ix_companies_account_owner", table_name="companies")
    op.create_index("ix_companies_owner", "companies", ["account_owner_id"], unique=False)

    op.drop_index("ix_buyplans_token", table_name="buy_plans")
    op.create_index("ix_bp_completed_by", "buy_plans", ["completed_by_id"], unique=False)
    op.create_index("ix_bp_cancelled_by", "buy_plans", ["cancelled_by_id"], unique=False)
    op.create_index("ix_bp_approved_by", "buy_plans", ["approved_by_id"], unique=False)

    op.drop_index("ix_activity_user_notif", table_name="activity_log")
    op.drop_index("ix_activity_site_contact", table_name="activity_log")
    op.drop_index("ix_activity_external", table_name="activity_log")
    op.create_index("ix_activity_log_site_contact_id", "activity_log", ["site_contact_id"], unique=False)
    op.create_index("ix_activity_log_customer_site_id", "activity_log", ["customer_site_id"], unique=False)

    # ── Restore orphan columns ──
    op.add_column("material_vendor_history", sa.Column("acctivate_last_date", sa.DATE(), nullable=True))
    op.add_column("material_vendor_history", sa.Column("acctivate_rma_rate", sa.DOUBLE_PRECISION(), nullable=True))
    op.add_column("material_vendor_history", sa.Column("acctivate_last_price", sa.DOUBLE_PRECISION(), nullable=True))
    op.add_column("vendor_cards", sa.Column("last_synced_at", postgresql.TIMESTAMP(), nullable=True))
    op.add_column("vendor_cards", sa.Column("sf_account_id", sa.VARCHAR(length=255), nullable=True))
    op.add_column("vendor_cards", sa.Column("acctivate_total_orders", sa.INTEGER(), nullable=True))
    op.add_column("vendor_cards", sa.Column("acctivate_total_units", sa.INTEGER(), nullable=True))
    op.add_column("vendor_cards", sa.Column("acctivate_last_order_date", sa.DATE(), nullable=True))
    op.add_column("vendor_cards", sa.Column("acctivate_vendor_id", sa.VARCHAR(length=255), nullable=True))
    op.create_index("ix_vendor_cards_sf_account_id", "vendor_cards", ["sf_account_id"], unique=True)
    op.create_index("ix_vendor_cards_acctivate_vendor_id", "vendor_cards", ["acctivate_vendor_id"], unique=False)
    op.add_column("requisitions", sa.Column("sf_requisition_id", sa.VARCHAR(length=255), nullable=True))
    op.add_column("requirements", sa.Column("sf_req_item_id", sa.VARCHAR(length=255), nullable=True))
    op.add_column("material_cards", sa.Column("sf_material_id", sa.VARCHAR(length=255), nullable=True))

    # ── Restore orphan table ──
    op.create_table(
        "inventory_snapshots",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.VARCHAR(length=255), nullable=False),
        sa.Column("warehouse_id", sa.VARCHAR(length=100), nullable=True),
        sa.Column("qty_on_hand", sa.INTEGER(), nullable=True),
        sa.Column("synced_at", postgresql.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="inventory_snapshots_pkey"),
    )
    op.create_index("ix_inventory_snapshots_product_id", "inventory_snapshots", ["product_id"], unique=False)
    op.create_index("ix_inv_product_warehouse", "inventory_snapshots", ["product_id", "warehouse_id"], unique=True)
