"""Add missing FK indexes, drop duplicate indexes, tune for performance

Revision ID: 003_perf_fk_indexes
Revises: 002_search_indexes
Create Date: 2026-02-21

Addresses performance audit findings:
- 23 FK columns lacked indexes (slow JOINs, cascading deletes)
- 8 duplicate indexes wasting ~3MB and slowing writes
- vendor_responses: 93% sequential scans (199K seq scans)
- contacts: 99.96% sequential scans (207K seq scans)
- requisitions: missing indexes on customer_site_id, created_by, cloned_from_id
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003_perf_fk_indexes"
down_revision: Union[str, None] = "002_search_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Phase 1: Drop duplicate indexes ─────────────────────────────────
    # Each pair: (dropped, kept)
    # ix_buyplans_token ↔ buy_plans_approval_token_key (UNIQUE)
    op.drop_index("ix_buyplans_token", table_name="buy_plans")
    # ix_contacts_vendor_name (WHERE) ↔ ix_contact_vendor_name (full)
    op.drop_index("ix_contacts_vendor_name", table_name="contacts")
    # ix_ese_email ↔ email_signature_extracts_sender_email_key (UNIQUE)
    op.drop_index("ix_ese_email", table_name="email_signature_extracts")
    # ix_offers_vendor_card (WHERE) ↔ ix_offers_vendor (full)
    op.drop_index("ix_offers_vendor_card", table_name="offers")
    # ix_sysconfig_key ↔ system_config_key_key (UNIQUE)
    op.drop_index("ix_sysconfig_key", table_name="system_config")
    # ix_vc_domain ↔ ix_vendor_cards_domain (same definition)
    op.drop_index("ix_vc_domain", table_name="vendor_cards")
    # ix_vendor_responses_vendor_name (WHERE) ↔ ix_vr_vendor_name (full)
    op.drop_index("ix_vendor_responses_vendor_name", table_name="vendor_responses")
    # ix_vr_message_id ↔ ix_vendor_responses_message_id (both UNIQUE)
    op.drop_index("ix_vr_message_id", table_name="vendor_responses")

    # ── Phase 2: Add missing FK indexes ─────────────────────────────────
    # vendor_responses (high seq scan table — 199K scans, 206M tuples)
    op.create_index("ix_vr_scanned_by", "vendor_responses", ["scanned_by_user_id"])
    op.create_index("ix_vr_contact", "vendor_responses", ["contact_id"])

    # requisitions (19M seq tuple reads)
    op.create_index("ix_req_site", "requisitions", ["customer_site_id"])
    op.create_index("ix_req_cloned_from", "requisitions", ["cloned_from_id"])
    op.create_index("ix_req_created_by", "requisitions", ["created_by"])

    # companies
    op.create_index("ix_companies_owner", "companies", ["account_owner_id"])

    # enrichment_queue
    op.create_index("ix_eq_reviewed_by", "enrichment_queue", ["reviewed_by_id"])
    op.create_index("ix_eq_vendor_contact", "enrichment_queue", ["vendor_contact_id"])

    # buy_plans
    op.create_index("ix_bp_completed_by", "buy_plans", ["completed_by_id"])
    op.create_index("ix_bp_approved_by", "buy_plans", ["approved_by_id"])
    op.create_index("ix_bp_cancelled_by", "buy_plans", ["cancelled_by_id"])

    # quotes
    op.create_index("ix_quotes_created_by", "quotes", ["created_by_id"])

    # offers
    op.create_index("ix_offers_vr", "offers", ["vendor_response_id"])
    op.create_index("ix_offers_entered_by", "offers", ["entered_by_id"])

    # prospect_contacts
    op.create_index("ix_pc_saved_by", "prospect_contacts", ["saved_by_id"])

    # enrichment_jobs
    op.create_index("ix_ej_started_by", "enrichment_jobs", ["started_by_id"])

    # proactive_matches
    op.create_index("ix_pm_requirement", "proactive_matches", ["requirement_id"])

    # offer_attachments
    op.create_index("ix_oa_uploaded_by", "offer_attachments", ["uploaded_by_id"])

    # vendor_reviews
    op.create_index("ix_vrev_user", "vendor_reviews", ["user_id"])

    # proactive_offers
    op.create_index("ix_poff_conv_req", "proactive_offers", ["converted_requisition_id"])
    op.create_index("ix_poff_conv_quote", "proactive_offers", ["converted_quote_id"])

    # error_reports
    op.create_index("ix_er_resolved_by", "error_reports", ["resolved_by_id"])

    # proactive_throttle
    op.create_index("ix_pt_offer", "proactive_throttle", ["proactive_offer_id"])


def downgrade() -> None:
    # ── Remove FK indexes ───────────────────────────────────────────────
    op.drop_index("ix_pt_offer", table_name="proactive_throttle")
    op.drop_index("ix_er_resolved_by", table_name="error_reports")
    op.drop_index("ix_poff_conv_quote", table_name="proactive_offers")
    op.drop_index("ix_poff_conv_req", table_name="proactive_offers")
    op.drop_index("ix_vrev_user", table_name="vendor_reviews")
    op.drop_index("ix_oa_uploaded_by", table_name="offer_attachments")
    op.drop_index("ix_pm_requirement", table_name="proactive_matches")
    op.drop_index("ix_ej_started_by", table_name="enrichment_jobs")
    op.drop_index("ix_pc_saved_by", table_name="prospect_contacts")
    op.drop_index("ix_offers_entered_by", table_name="offers")
    op.drop_index("ix_offers_vr", table_name="offers")
    op.drop_index("ix_quotes_created_by", table_name="quotes")
    op.drop_index("ix_bp_cancelled_by", table_name="buy_plans")
    op.drop_index("ix_bp_approved_by", table_name="buy_plans")
    op.drop_index("ix_bp_completed_by", table_name="buy_plans")
    op.drop_index("ix_eq_vendor_contact", table_name="enrichment_queue")
    op.drop_index("ix_eq_reviewed_by", table_name="enrichment_queue")
    op.drop_index("ix_companies_owner", table_name="companies")
    op.drop_index("ix_req_created_by", table_name="requisitions")
    op.drop_index("ix_req_cloned_from", table_name="requisitions")
    op.drop_index("ix_req_site", table_name="requisitions")
    op.drop_index("ix_vr_contact", table_name="vendor_responses")
    op.drop_index("ix_vr_scanned_by", table_name="vendor_responses")

    # ── Restore duplicate indexes ───────────────────────────────────────
    op.create_index("ix_vr_message_id", "vendor_responses", ["message_id"], unique=True)
    op.create_index(
        "ix_vendor_responses_vendor_name", "vendor_responses", ["vendor_name"],
        postgresql_where="vendor_name IS NOT NULL",
    )
    op.create_index("ix_vc_domain", "vendor_cards", ["domain"])
    op.create_index("ix_sysconfig_key", "system_config", ["key"], unique=True)
    op.create_index(
        "ix_offers_vendor_card", "offers", ["vendor_card_id"],
        postgresql_where="vendor_card_id IS NOT NULL",
    )
    op.create_index("ix_ese_email", "email_signature_extracts", ["sender_email"], unique=True)
    op.create_index(
        "ix_contacts_vendor_name", "contacts", ["vendor_name"],
        postgresql_where="vendor_name IS NOT NULL",
    )
    op.create_index("ix_buyplans_token", "buy_plans", ["approval_token"])
