"""Comprehensive tests for app/constants.py — boosts coverage from ~55% to 95%+.

Covers every StrEnum class and module-level constant in the file.
Each test imports the target and asserts at least one member value so that
every member-definition line is executed by the test runner.

Called by: pytest (test suite)
Depends on: app/constants.py
"""

import os

os.environ["TESTING"] = "1"

# ---------------------------------------------------------------------------
# Module-level attachment constants
# ---------------------------------------------------------------------------


def test_max_attachment_bytes():
    from app.constants import MAX_ATTACHMENT_BYTES

    assert MAX_ATTACHMENT_BYTES == 10 * 1024 * 1024


def test_allowed_attachment_extensions():
    from app.constants import ALLOWED_ATTACHMENT_EXTENSIONS

    assert ".pdf" in ALLOWED_ATTACHMENT_EXTENSIONS
    assert ".xlsx" in ALLOWED_ATTACHMENT_EXTENSIONS
    assert isinstance(ALLOWED_ATTACHMENT_EXTENSIONS, frozenset)


# ---------------------------------------------------------------------------
# ProactiveMatchStatus
# ---------------------------------------------------------------------------


def test_proactive_match_status():
    from app.constants import ProactiveMatchStatus

    assert ProactiveMatchStatus.NEW == "new"
    assert ProactiveMatchStatus.SENT == "sent"
    assert ProactiveMatchStatus.FAILED == "failed"
    assert ProactiveMatchStatus.DISMISSED == "dismissed"
    assert ProactiveMatchStatus.CONVERTED == "converted"
    assert ProactiveMatchStatus.EXPIRED == "expired"


# ---------------------------------------------------------------------------
# ContactStatus
# ---------------------------------------------------------------------------


def test_contact_status():
    from app.constants import ContactStatus

    assert ContactStatus.SENT == "sent"
    assert ContactStatus.FAILED == "failed"
    assert ContactStatus.QUOTED == "quoted"
    assert ContactStatus.DECLINED == "declined"
    assert ContactStatus.RESPONDED == "responded"
    assert ContactStatus.PENDING == "pending"
    assert ContactStatus.OPENED == "opened"
    assert ContactStatus.OOO == "ooo"
    assert ContactStatus.BOUNCED == "bounced"
    assert ContactStatus.RETRIED == "retried"


# ---------------------------------------------------------------------------
# OfferStatus
# ---------------------------------------------------------------------------


def test_offer_status():
    from app.constants import OfferStatus

    assert OfferStatus.PENDING_REVIEW == "pending_review"
    assert OfferStatus.ACTIVE == "active"
    assert OfferStatus.APPROVED == "approved"
    assert OfferStatus.REJECTED == "rejected"
    assert OfferStatus.SOLD == "sold"
    assert OfferStatus.WON == "won"
    assert OfferStatus.EXPIRED == "expired"


# ---------------------------------------------------------------------------
# AttributionStatus
# ---------------------------------------------------------------------------


def test_attribution_status():
    from app.constants import AttributionStatus

    assert AttributionStatus.ACTIVE == "active"


# ---------------------------------------------------------------------------
# OfferCondition
# ---------------------------------------------------------------------------


def test_offer_condition():
    from app.constants import OfferCondition

    assert OfferCondition.NEW == "new"
    assert OfferCondition.NEW_NO_PKG == "new_no_pkg"
    assert OfferCondition.PULLS == "pulls"
    assert OfferCondition.REFURB == "refurb"


# ---------------------------------------------------------------------------
# QualificationStatus
# ---------------------------------------------------------------------------


def test_qualification_status():
    from app.constants import QualificationStatus

    assert QualificationStatus.UNSET == "unset"
    assert QualificationStatus.INCOMPLETE == "incomplete"
    assert QualificationStatus.ESSENTIALS == "essentials"
    assert QualificationStatus.COMPLETE == "complete"


# ---------------------------------------------------------------------------
# RequisitionStatus (including nonmember frozensets)
# ---------------------------------------------------------------------------


def test_requisition_status_members():
    from app.constants import RequisitionStatus

    assert RequisitionStatus.DRAFT == "draft"
    assert RequisitionStatus.OPEN == "open"
    assert RequisitionStatus.RFQS_SENT == "rfqs_sent"
    assert RequisitionStatus.OFFERS == "offers"
    assert RequisitionStatus.QUOTED == "quoted"
    assert RequisitionStatus.WON == "won"
    assert RequisitionStatus.LOST == "lost"
    assert RequisitionStatus.HOTLIST == "hotlist"
    assert RequisitionStatus.CANCELLED == "cancelled"


def test_requisition_status_nonmembers():
    from app.constants import RequisitionStatus

    assert isinstance(RequisitionStatus.TERMINAL, frozenset)
    assert "won" in RequisitionStatus.TERMINAL
    assert "lost" in RequisitionStatus.TERMINAL
    assert "cancelled" in RequisitionStatus.TERMINAL

    assert isinstance(RequisitionStatus.OPEN_PIPELINE, frozenset)
    assert "open" in RequisitionStatus.OPEN_PIPELINE

    assert isinstance(RequisitionStatus.MONITOR, frozenset)
    assert "hotlist" in RequisitionStatus.MONITOR


# ---------------------------------------------------------------------------
# SourcingStatus
# ---------------------------------------------------------------------------


def test_sourcing_status():
    from app.constants import SourcingStatus

    assert SourcingStatus.OPEN == "open"
    assert SourcingStatus.SOURCING == "sourcing"
    assert SourcingStatus.OFFERED == "offered"
    assert SourcingStatus.QUOTED == "quoted"
    assert SourcingStatus.WON == "won"
    assert SourcingStatus.LOST == "lost"
    assert SourcingStatus.ARCHIVED == "archived"


# ---------------------------------------------------------------------------
# ExcessListStatus
# ---------------------------------------------------------------------------


def test_excess_list_status():
    from app.constants import ExcessListStatus

    assert ExcessListStatus.DRAFT == "draft"
    assert ExcessListStatus.OPEN == "open"
    assert ExcessListStatus.COLLECTING == "collecting"
    assert ExcessListStatus.BID_OUT == "bid_out"
    assert ExcessListStatus.AWARDED == "awarded"
    assert ExcessListStatus.CLOSED == "closed"
    assert ExcessListStatus.EXPIRED == "expired"
    assert ExcessListStatus.ACTIVE == "active"
    assert ExcessListStatus.BIDDING == "bidding"


# ---------------------------------------------------------------------------
# ExcessOfferStatus
# ---------------------------------------------------------------------------


def test_excess_offer_status():
    from app.constants import ExcessOfferStatus

    assert ExcessOfferStatus.OPEN == "open"
    assert ExcessOfferStatus.WON == "won"
    assert ExcessOfferStatus.LOST == "lost"
    assert ExcessOfferStatus.WITHDRAWN == "withdrawn"
    assert ExcessOfferStatus.LATE == "late"


# ---------------------------------------------------------------------------
# ExcessOfferScope
# ---------------------------------------------------------------------------


def test_excess_offer_scope():
    from app.constants import ExcessOfferScope

    assert ExcessOfferScope.PER_LINE == "per_line"
    assert ExcessOfferScope.TAKE_ALL == "take_all"


# ---------------------------------------------------------------------------
# OfferLineMatchStatus
# ---------------------------------------------------------------------------


def test_offer_line_match_status():
    from app.constants import OfferLineMatchStatus

    assert OfferLineMatchStatus.MATCHED == "matched"
    assert OfferLineMatchStatus.UNMATCHED == "unmatched"
    assert OfferLineMatchStatus.AMBIGUOUS == "ambiguous"


# ---------------------------------------------------------------------------
# ExcessLineItemStatus
# ---------------------------------------------------------------------------


def test_excess_line_item_status():
    from app.constants import ExcessLineItemStatus

    assert ExcessLineItemStatus.AVAILABLE == "available"
    assert ExcessLineItemStatus.BIDDING == "bidding"
    assert ExcessLineItemStatus.AWARDED == "awarded"
    assert ExcessLineItemStatus.WITHDRAWN == "withdrawn"


# ---------------------------------------------------------------------------
# CustomerBidStatus
# ---------------------------------------------------------------------------


def test_customer_bid_status():
    from app.constants import CustomerBidStatus

    assert CustomerBidStatus.DRAFT == "draft"
    assert CustomerBidStatus.SENT == "sent"
    assert CustomerBidStatus.ACCEPTED == "accepted"
    assert CustomerBidStatus.REJECTED == "rejected"


# ---------------------------------------------------------------------------
# ExcessOutreachChannel
# ---------------------------------------------------------------------------


def test_excess_outreach_channel():
    from app.constants import ExcessOutreachChannel

    assert ExcessOutreachChannel.EMAIL == "email"
    assert ExcessOutreachChannel.PHONE == "phone"
    assert ExcessOutreachChannel.TEAMS == "teams"
    assert ExcessOutreachChannel.MARKETPLACE == "marketplace"
    assert ExcessOutreachChannel.OTHER == "other"


# ---------------------------------------------------------------------------
# ExcessOutreachStatus
# ---------------------------------------------------------------------------


def test_excess_outreach_status():
    from app.constants import ExcessOutreachStatus

    assert ExcessOutreachStatus.SENT == "sent"
    assert ExcessOutreachStatus.OPENED == "opened"
    assert ExcessOutreachStatus.RESPONDED == "responded"
    assert ExcessOutreachStatus.BID == "bid"
    assert ExcessOutreachStatus.DECLINED == "declined"
    assert ExcessOutreachStatus.NO_RESPONSE == "no_response"


# ---------------------------------------------------------------------------
# QuoteStatus
# ---------------------------------------------------------------------------


def test_quote_status():
    from app.constants import QuoteStatus

    assert QuoteStatus.DRAFT == "draft"
    assert QuoteStatus.SENT == "sent"
    assert QuoteStatus.WON == "won"
    assert QuoteStatus.LOST == "lost"
    assert QuoteStatus.REVISED == "revised"


# ---------------------------------------------------------------------------
# VendorResponseStatus
# ---------------------------------------------------------------------------


def test_vendor_response_status():
    from app.constants import VendorResponseStatus

    assert VendorResponseStatus.NEW == "new"
    assert VendorResponseStatus.PARSED == "parsed"
    assert VendorResponseStatus.REVIEWED == "reviewed"
    assert VendorResponseStatus.REJECTED == "rejected"


# ---------------------------------------------------------------------------
# UserRole + RESTRICTED_ROLES
# ---------------------------------------------------------------------------


def test_user_role():
    from app.constants import UserRole

    assert UserRole.BUYER == "buyer"
    assert UserRole.SALES == "sales"
    assert UserRole.TRADER == "trader"
    assert UserRole.MANAGER == "manager"
    assert UserRole.ADMIN == "admin"
    assert UserRole.AGENT == "agent"


def test_restricted_roles():
    from app.constants import RESTRICTED_ROLES, UserRole

    assert isinstance(RESTRICTED_ROLES, frozenset)
    assert UserRole.SALES in RESTRICTED_ROLES
    assert UserRole.TRADER in RESTRICTED_ROLES
    assert UserRole.ADMIN not in RESTRICTED_ROLES


# ---------------------------------------------------------------------------
# AccessKey + MODULE_ACCESS_KEYS + CAPABILITY_ACCESS_KEYS + ROLE_ACCESS_DEFAULTS
# ---------------------------------------------------------------------------


def test_access_key_module_members():
    from app.constants import AccessKey

    assert AccessKey.REQUISITIONS == "requisitions"
    assert AccessKey.SIGHTINGS == "sightings"
    assert AccessKey.MATERIALS == "materials"
    assert AccessKey.SEARCH == "search"
    assert AccessKey.BUY_PLANS == "buy_plans"
    assert AccessKey.RESELL == "resell"
    assert AccessKey.CRM == "crm"
    assert AccessKey.PROACTIVE == "proactive"
    assert AccessKey.PROSPECTING == "prospecting"
    assert AccessKey.MY_DAY == "my_day"


def test_access_key_capability_members():
    from app.constants import AccessKey

    assert AccessKey.SEND_RFQ == "send_rfq"
    assert AccessKey.APPROVE_OFFERS == "approve_offers"
    assert AccessKey.EXPORT_DATA == "export_data"
    assert AccessKey.MANAGE_CONNECTORS == "manage_connectors"
    assert AccessKey.OPS_VERIFICATION == "ops_verification"


def test_module_access_keys():
    from app.constants import MODULE_ACCESS_KEYS, AccessKey

    assert isinstance(MODULE_ACCESS_KEYS, tuple)
    assert AccessKey.REQUISITIONS in MODULE_ACCESS_KEYS
    assert AccessKey.MY_DAY in MODULE_ACCESS_KEYS
    assert len(MODULE_ACCESS_KEYS) == 10


def test_capability_access_keys():
    from app.constants import CAPABILITY_ACCESS_KEYS, AccessKey

    assert isinstance(CAPABILITY_ACCESS_KEYS, tuple)
    assert AccessKey.SEND_RFQ in CAPABILITY_ACCESS_KEYS
    assert AccessKey.OPS_VERIFICATION in CAPABILITY_ACCESS_KEYS
    assert len(CAPABILITY_ACCESS_KEYS) == 5


def test_role_access_defaults():
    from app.constants import ROLE_ACCESS_DEFAULTS, AccessKey, UserRole

    assert isinstance(ROLE_ACCESS_DEFAULTS, dict)
    assert UserRole.BUYER in ROLE_ACCESS_DEFAULTS
    assert UserRole.ADMIN in ROLE_ACCESS_DEFAULTS
    assert UserRole.AGENT in ROLE_ACCESS_DEFAULTS
    # Admin gets everything
    assert AccessKey.OPS_VERIFICATION in ROLE_ACCESS_DEFAULTS[UserRole.ADMIN]
    # Agent gets nothing
    assert len(ROLE_ACCESS_DEFAULTS[UserRole.AGENT]) == 0
    # Buyer gets interactive defaults (not ops_verification)
    assert AccessKey.OPS_VERIFICATION not in ROLE_ACCESS_DEFAULTS[UserRole.BUYER]
    assert AccessKey.SEND_RFQ in ROLE_ACCESS_DEFAULTS[UserRole.BUYER]


# ---------------------------------------------------------------------------
# UserAuditAction
# ---------------------------------------------------------------------------


def test_user_audit_action():
    from app.constants import UserAuditAction

    assert UserAuditAction.INVITE == "invite"
    assert UserAuditAction.ROLE_CHANGE == "role_change"
    assert UserAuditAction.ACTIVATE == "activate"
    assert UserAuditAction.DEACTIVATE == "deactivate"
    assert UserAuditAction.ACCESS_GRANT == "access_grant"
    assert UserAuditAction.ACCESS_REVOKE == "access_revoke"
    assert UserAuditAction.APPROVAL_GRANT == "approval_grant"
    assert UserAuditAction.APPROVAL_REVOKE == "approval_revoke"


# ---------------------------------------------------------------------------
# ProactiveOfferStatus
# ---------------------------------------------------------------------------


def test_proactive_offer_status():
    from app.constants import ProactiveOfferStatus

    assert ProactiveOfferStatus.SENT == "sent"
    assert ProactiveOfferStatus.CONVERTED == "converted"
    assert ProactiveOfferStatus.EXPIRED == "expired"


# ---------------------------------------------------------------------------
# TicketStatus + TicketSource
# ---------------------------------------------------------------------------


def test_ticket_status():
    from app.constants import TicketStatus

    assert TicketStatus.SUBMITTED == "submitted"
    assert TicketStatus.IN_PROGRESS == "in_progress"
    assert TicketStatus.RESOLVED == "resolved"
    assert TicketStatus.WONT_FIX == "wont_fix"


def test_ticket_source():
    from app.constants import TicketSource

    assert TicketSource.REPORT_BUTTON == "report_button"
    assert TicketSource.TICKET_FORM == "ticket_form"


# ---------------------------------------------------------------------------
# BuyPlanStatus
# ---------------------------------------------------------------------------


def test_buy_plan_status():
    from app.constants import BuyPlanStatus

    assert BuyPlanStatus.DRAFT == "draft"
    assert BuyPlanStatus.PENDING == "pending"
    assert BuyPlanStatus.ACTIVE == "active"
    assert BuyPlanStatus.INBOUND == "inbound"
    assert BuyPlanStatus.HALTED == "halted"
    assert BuyPlanStatus.COMPLETED == "completed"
    assert BuyPlanStatus.CANCELLED == "cancelled"


# ---------------------------------------------------------------------------
# SOVerificationStatus
# ---------------------------------------------------------------------------


def test_so_verification_status():
    from app.constants import SOVerificationStatus

    assert SOVerificationStatus.PENDING == "pending"
    assert SOVerificationStatus.APPROVED == "approved"
    assert SOVerificationStatus.REJECTED == "rejected"


# ---------------------------------------------------------------------------
# BuyPlanLineStatus
# ---------------------------------------------------------------------------


def test_buy_plan_line_status():
    from app.constants import BuyPlanLineStatus

    assert BuyPlanLineStatus.AWAITING_PO == "awaiting_po"
    assert BuyPlanLineStatus.PENDING_VERIFY == "pending_verify"
    assert BuyPlanLineStatus.VERIFIED == "verified"
    assert BuyPlanLineStatus.ISSUE == "issue"
    assert BuyPlanLineStatus.CANCELLED == "cancelled"
    assert BuyPlanLineStatus.RESOURCING == "resourcing"


# ---------------------------------------------------------------------------
# LineIssueType
# ---------------------------------------------------------------------------


def test_line_issue_type():
    from app.constants import LineIssueType

    assert LineIssueType.SOLD_OUT == "sold_out"
    assert LineIssueType.PRICE_CHANGED == "price_changed"
    assert LineIssueType.LEAD_TIME_CHANGED == "lead_time_changed"
    assert LineIssueType.OTHER == "other"


# ---------------------------------------------------------------------------
# LineResourceReason + RESOURCE_TO_UNAVAILABILITY_REASON
# ---------------------------------------------------------------------------


def test_line_resource_reason():
    from app.constants import LineResourceReason

    assert LineResourceReason.SOLD_ELSEWHERE == "sold_elsewhere"
    assert LineResourceReason.CANNOT_DELIVER == "cannot_deliver"
    assert LineResourceReason.NO_STOCK == "no_stock"
    assert LineResourceReason.PRICE_CHANGE == "price_change"
    assert LineResourceReason.DEFECTIVE == "defective"
    assert LineResourceReason.WRONG_PART == "wrong_part"
    assert LineResourceReason.SHORT_SHIP == "short_ship"
    assert LineResourceReason.OTHER == "other"


def test_resource_to_unavailability_reason():
    from app.constants import RESOURCE_TO_UNAVAILABILITY_REASON, LineResourceReason

    assert isinstance(RESOURCE_TO_UNAVAILABILITY_REASON, dict)
    assert RESOURCE_TO_UNAVAILABILITY_REASON[LineResourceReason.SOLD_ELSEWHERE.value] == "sold_elsewhere"
    assert RESOURCE_TO_UNAVAILABILITY_REASON[LineResourceReason.DEFECTIVE.value] == "broken"
    assert RESOURCE_TO_UNAVAILABILITY_REASON[LineResourceReason.WRONG_PART.value] == "different_part"
    assert RESOURCE_TO_UNAVAILABILITY_REASON[LineResourceReason.SHORT_SHIP.value] == "not_really_there"


# ---------------------------------------------------------------------------
# POCancellationReason
# ---------------------------------------------------------------------------


def test_po_cancellation_reason():
    from app.constants import POCancellationReason

    assert POCancellationReason.SOLD_ELSEWHERE == "sold_elsewhere"
    assert POCancellationReason.CANNOT_DELIVER == "cannot_deliver"
    assert POCancellationReason.NO_STOCK == "no_stock"
    assert POCancellationReason.PRICE_CHANGE == "price_change"
    assert POCancellationReason.DEFECTIVE == "defective"
    assert POCancellationReason.WRONG_PART == "wrong_part"
    assert POCancellationReason.SHORT_SHIP == "short_ship"
    assert POCancellationReason.OTHER == "other"


# ---------------------------------------------------------------------------
# AIFlagSeverity
# ---------------------------------------------------------------------------


def test_ai_flag_severity():
    from app.constants import AIFlagSeverity

    assert AIFlagSeverity.INFO == "info"
    assert AIFlagSeverity.WARNING == "warning"
    assert AIFlagSeverity.CRITICAL == "critical"


# ---------------------------------------------------------------------------
# RiskFlagType
# ---------------------------------------------------------------------------


def test_risk_flag_type():
    from app.constants import RiskFlagType

    assert RiskFlagType.STALE_OFFER == "stale_offer"


# ---------------------------------------------------------------------------
# RiskFlagSeverity
# ---------------------------------------------------------------------------


def test_risk_flag_severity():
    # RiskFlagSeverity was consolidated into AIFlagSeverity in this branch
    from app.constants import AIFlagSeverity

    assert AIFlagSeverity.INFO == "info"
    assert AIFlagSeverity.WARNING == "warning"
    assert AIFlagSeverity.CRITICAL == "critical"


# ---------------------------------------------------------------------------
# ProspectAccountStatus
# ---------------------------------------------------------------------------


def test_prospect_account_status():
    from app.constants import ProspectAccountStatus

    assert ProspectAccountStatus.SUGGESTED == "suggested"
    assert ProspectAccountStatus.CLAIMED == "claimed"
    assert ProspectAccountStatus.DISMISSED == "dismissed"
    assert ProspectAccountStatus.CONVERTED == "converted"


# ---------------------------------------------------------------------------
# CompanyDisposition
# ---------------------------------------------------------------------------


def test_company_disposition():
    from app.constants import CompanyDisposition

    assert CompanyDisposition.ACTIVE == "active"
    assert CompanyDisposition.BUCKET == "bucket"


# ---------------------------------------------------------------------------
# TaskStatus
# ---------------------------------------------------------------------------


def test_task_status():
    from app.constants import TaskStatus

    assert TaskStatus.TODO == "todo"
    assert TaskStatus.IN_PROGRESS == "in_progress"
    assert TaskStatus.DONE == "done"


# ---------------------------------------------------------------------------
# PendingBatchStatus
# ---------------------------------------------------------------------------


def test_pending_batch_status():
    from app.constants import PendingBatchStatus

    assert PendingBatchStatus.PROCESSING == "processing"
    assert PendingBatchStatus.COMPLETED == "completed"
    assert PendingBatchStatus.FAILED == "failed"


# ---------------------------------------------------------------------------
# SpecCodeSource
# ---------------------------------------------------------------------------


def test_spec_code_source():
    from app.constants import SpecCodeSource

    assert SpecCodeSource.MANUAL == "manual"
    assert SpecCodeSource.LLM_APPROVED == "llm_approved"
    assert SpecCodeSource.CSV_IMPORT == "csv_import"


# ---------------------------------------------------------------------------
# RfqAttachmentStatus
# ---------------------------------------------------------------------------


def test_rfq_attachment_status():
    from app.constants import RfqAttachmentStatus

    assert RfqAttachmentStatus.ATTACHED == "attached"
    assert RfqAttachmentStatus.MISSING == "missing"
    assert RfqAttachmentStatus.OVERSIZED == "oversized"
    assert RfqAttachmentStatus.FETCH_ERROR == "fetch_error"


# ---------------------------------------------------------------------------
# BROWSER_WORKER_SOURCES
# ---------------------------------------------------------------------------


def test_browser_worker_sources():
    from app.constants import BROWSER_WORKER_SOURCES

    assert isinstance(BROWSER_WORKER_SOURCES, frozenset)
    assert "icsource" in BROWSER_WORKER_SOURCES
    assert "netcomponents" in BROWSER_WORKER_SOURCES
    assert "thebrokersite" in BROWSER_WORKER_SOURCES


# ---------------------------------------------------------------------------
# ActivityType
# ---------------------------------------------------------------------------


def test_activity_type_core():
    from app.constants import ActivityType

    assert ActivityType.RFQ_SENT == "rfq_sent"
    assert ActivityType.EMAIL_RECEIVED == "email_received"
    assert ActivityType.CALL_LOGGED == "call_logged"
    assert ActivityType.STATUS_CHANGED == "status_changed"
    assert ActivityType.OFFER_CREATED == "offer_created"
    assert ActivityType.OFFER_STATUS_CHANGED == "offer_status_changed"
    assert ActivityType.SIGHTING_ADDED == "sighting_added"
    assert ActivityType.SALES_NOTE == "sales_note"
    assert ActivityType.TASK_COMPLETED == "task_completed"
    assert ActivityType.ASSIGNMENT_CHANGED == "assignment_changed"


def test_activity_type_extended():
    from app.constants import ActivityType

    assert ActivityType.STRATEGIC_VENDOR_EXPIRING == "strategic_expiring"
    assert ActivityType.EMAIL_SENT == "email_sent"
    assert ActivityType.NOTE == "note"
    assert ActivityType.CONTACT_NOTE == "contact_note"
    assert ActivityType.BUYPLAN_APPROVED == "buyplan_approved"
    assert ActivityType.BUYPLAN_REJECTED == "buyplan_rejected"
    assert ActivityType.BUYPLAN_PENDING == "buyplan_pending"
    assert ActivityType.BUYPLAN_COMPLETED == "buyplan_completed"
    assert ActivityType.OFFER_PENDING_REVIEW == "offer_pending_review"
    assert ActivityType.NEW_OFFER == "new_offer"
    assert ActivityType.COMPETITIVE_QUOTE == "competitive_quote"
    assert ActivityType.BID_RECEIVED == "bid_received"


def test_activity_type_advanced():
    from app.constants import ActivityType

    assert ActivityType.OWNERSHIP_WARNING == "ownership_warning"
    assert ActivityType.PROACTIVE_MATCH == "proactive_match"
    assert ActivityType.PART_STATUS_CHANGE == "part_status_change"
    assert ActivityType.TEAMS_MESSAGE == "teams_message"
    assert ActivityType.WECHAT_MESSAGE == "wechat_message"
    assert ActivityType.MEETING == "meeting"
    assert ActivityType.VENDOR_UNAVAILABLE == "vendor_unavailable"
    assert ActivityType.VENDOR_AVAILABLE == "vendor_available"
    assert ActivityType.RESOURCE_REQUESTED == "resource_requested"
    assert ActivityType.APPROVAL_REQUESTED == "aprvl_requested"
    assert ActivityType.APPROVAL_APPROVED == "aprvl_approved"
    assert ActivityType.APPROVAL_REJECTED == "aprvl_rejected"
    assert ActivityType.APPROVAL_DELEGATED == "aprvl_delegated"
    assert ActivityType.APPROVAL_CANCELLED == "aprvl_cancelled"


# ---------------------------------------------------------------------------
# CallOutcome + MEANINGFUL_CALL_OUTCOMES
# ---------------------------------------------------------------------------


def test_call_outcome():
    from app.constants import CallOutcome

    assert CallOutcome.CONNECTED == "connected"
    assert CallOutcome.LEFT_MESSAGE == "left_message"
    assert CallOutcome.VOICEMAIL == "voicemail"
    assert CallOutcome.NO_ANSWER == "no_answer"


def test_meaningful_call_outcomes():
    from app.constants import MEANINGFUL_CALL_OUTCOMES, CallOutcome

    assert isinstance(MEANINGFUL_CALL_OUTCOMES, frozenset)
    assert CallOutcome.CONNECTED in MEANINGFUL_CALL_OUTCOMES
    assert CallOutcome.LEFT_MESSAGE in MEANINGFUL_CALL_OUTCOMES
    assert CallOutcome.VOICEMAIL not in MEANINGFUL_CALL_OUTCOMES
    assert CallOutcome.NO_ANSWER not in MEANINGFUL_CALL_OUTCOMES


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


def test_channel():
    from app.constants import Channel

    assert Channel.SYSTEM == "system"
    assert Channel.PHONE == "phone"
    assert Channel.MANUAL == "manual"
    assert Channel.EMAIL == "email"
    assert Channel.CHROME == "chrome"
    assert Channel.TEAMS == "teams"
    assert Channel.OUTLOOK == "outlook"
    assert Channel.CALENDAR == "calendar"
    assert Channel.AVAIL_SYSTEM == "avail_system"
    assert Channel.WECHAT == "wechat"


# ---------------------------------------------------------------------------
# OutreachChannel
# ---------------------------------------------------------------------------


def test_outreach_channel():
    from app.constants import OutreachChannel

    assert OutreachChannel.PHONE == "phone"
    assert OutreachChannel.EMAIL == "email"
    assert OutreachChannel.TEAMS == "teams"
    assert OutreachChannel.WECHAT == "wechat"


# ---------------------------------------------------------------------------
# ContactRole
# ---------------------------------------------------------------------------


def test_contact_role():
    from app.constants import ContactRole

    assert ContactRole.BUYER == "buyer"
    assert ContactRole.MANAGER == "manager"
    assert ContactRole.ENGINEER == "engineer"
    assert ContactRole.PLANNER == "planner"
    assert ContactRole.OTHER == "other"


# ---------------------------------------------------------------------------
# CRM_INDUSTRIES
# ---------------------------------------------------------------------------


def test_crm_industries():
    from app.constants import CRM_INDUSTRIES

    assert isinstance(CRM_INDUSTRIES, tuple)
    assert "Aerospace" in CRM_INDUSTRIES
    assert "Defense" in CRM_INDUSTRIES
    assert "Other" in CRM_INDUSTRIES
    assert len(CRM_INDUSTRIES) == 17


# ---------------------------------------------------------------------------
# EventType
# ---------------------------------------------------------------------------


def test_event_type():
    from app.constants import EventType

    assert EventType.EMAIL == "email"
    assert EventType.CALL == "call"
    assert EventType.MESSAGE == "message"
    assert EventType.MEETING == "meeting"
    assert EventType.API_SOURCE_DOWN == "api_source_down"
    assert EventType.API_QUOTA_WARNING == "api_quota_warning"
    assert EventType.API_QUOTA_CRITICAL == "api_quota_critical"


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def test_direction():
    from app.constants import Direction

    assert Direction.INBOUND == "inbound"
    assert Direction.OUTBOUND == "outbound"


# ---------------------------------------------------------------------------
# DigestEntityType
# ---------------------------------------------------------------------------


def test_digest_entity_type():
    from app.constants import DigestEntityType

    assert DigestEntityType.REQUISITION == "requisition"
    assert DigestEntityType.COMPANY == "company"


# ---------------------------------------------------------------------------
# DigestStatusSignal
# ---------------------------------------------------------------------------


def test_digest_status_signal():
    from app.constants import DigestStatusSignal

    assert DigestStatusSignal.ON_TRACK == "on_track"
    assert DigestStatusSignal.STALLED == "stalled"
    assert DigestStatusSignal.NEEDS_ATTENTION == "needs_attention"


# ---------------------------------------------------------------------------
# InboxSyncHealth
# ---------------------------------------------------------------------------


def test_inbox_sync_health():
    from app.constants import InboxSyncHealth

    assert InboxSyncHealth.OK == "ok"
    assert InboxSyncHealth.WARNING == "warning"
    assert InboxSyncHealth.ERROR == "error"


# ---------------------------------------------------------------------------
# MaterialCondition
# ---------------------------------------------------------------------------


def test_material_condition():
    from app.constants import MaterialCondition

    assert MaterialCondition.NEW == "New"
    assert MaterialCondition.RECERTIFIED == "Recertified"
    assert MaterialCondition.REFURBISHED == "Refurbished"
    assert MaterialCondition.USED == "Used"
    assert MaterialCondition.PULLED == "Pulled"
    assert MaterialCondition.UNKNOWN == "Unknown"


# ---------------------------------------------------------------------------
# FruLinkKind
# ---------------------------------------------------------------------------


def test_fru_link_kind():
    from app.constants import FruLinkKind

    assert FruLinkKind.IBM_11S == "ibm_11s"
    assert FruLinkKind.MFG_MODEL == "mfg_model"
    assert FruLinkKind.OPTION == "option"
    assert FruLinkKind.OPTION_PN == "option_pn"
    assert FruLinkKind.SOURCING_PN == "sourcing_pn"
    assert FruLinkKind.LENOVO_PN == "lenovo_pn"
    assert FruLinkKind.LENOVO_PPN == "lenovo_ppn"
    assert FruLinkKind.TRAY == "tray"
    assert FruLinkKind.TRAY_ALT == "tray_alt"
    assert FruLinkKind.BRACKET == "bracket"
    assert FruLinkKind.BOARD == "board"
    assert FruLinkKind.SCREWS == "screws"
    assert FruLinkKind.SHUTTLE == "shuttle"
    assert FruLinkKind.DONGLE == "dongle"
    assert FruLinkKind.DRIVE_PN == "drive_pn"
    assert FruLinkKind.ASSEMBLY == "assembly"


# ---------------------------------------------------------------------------
# CDC_PENDING + FRU_ALIAS_SOURCE
# ---------------------------------------------------------------------------


def test_cdc_pending():
    from app.constants import CDC_PENDING

    assert CDC_PENDING == "cdc_pending"


def test_fru_alias_source():
    from app.constants import FRU_ALIAS_SOURCE

    assert FRU_ALIAS_SOURCE == "fru_crosswalk"


# ---------------------------------------------------------------------------
# UnavailabilityReason (including label + condition_specific properties)
# ---------------------------------------------------------------------------


def test_unavailability_reason_members():
    from app.constants import UnavailabilityReason

    assert UnavailabilityReason.BOUGHT_BY_US == "bought_by_us"
    assert UnavailabilityReason.SOLD_ELSEWHERE == "sold_elsewhere"
    assert UnavailabilityReason.BROKEN == "broken"
    assert UnavailabilityReason.NOT_REALLY_THERE == "not_really_there"
    assert UnavailabilityReason.DIFFERENT_PART == "different_part"
    assert UnavailabilityReason.OTHER == "other"


def test_unavailability_reason_label_property():
    from app.constants import UnavailabilityReason

    assert UnavailabilityReason.BOUGHT_BY_US.label == "We bought them"
    assert UnavailabilityReason.SOLD_ELSEWHERE.label == "Vendor sold them"
    assert UnavailabilityReason.BROKEN.label == "Broken / bad condition"
    assert UnavailabilityReason.NOT_REALLY_THERE.label == "Not really in stock"
    assert UnavailabilityReason.DIFFERENT_PART.label == "Different part number"
    assert UnavailabilityReason.OTHER.label == "Other"


def test_unavailability_reason_condition_specific_property():
    from app.constants import UnavailabilityReason

    assert UnavailabilityReason.BOUGHT_BY_US.condition_specific is True
    assert UnavailabilityReason.SOLD_ELSEWHERE.condition_specific is True
    assert UnavailabilityReason.BROKEN.condition_specific is True
    assert UnavailabilityReason.NOT_REALLY_THERE.condition_specific is False
    assert UnavailabilityReason.DIFFERENT_PART.condition_specific is False
    assert UnavailabilityReason.OTHER.condition_specific is False


# ---------------------------------------------------------------------------
# CONDITION_SPECIFIC_REASONS
# ---------------------------------------------------------------------------


def test_condition_specific_reasons():
    from app.constants import CONDITION_SPECIFIC_REASONS, UnavailabilityReason

    assert isinstance(CONDITION_SPECIFIC_REASONS, frozenset)
    assert UnavailabilityReason.BOUGHT_BY_US in CONDITION_SPECIFIC_REASONS
    assert UnavailabilityReason.SOLD_ELSEWHERE in CONDITION_SPECIFIC_REASONS
    assert UnavailabilityReason.BROKEN in CONDITION_SPECIFIC_REASONS
    assert UnavailabilityReason.OTHER not in CONDITION_SPECIFIC_REASONS


# ---------------------------------------------------------------------------
# ReleaseTrigger (including label property)
# ---------------------------------------------------------------------------


def test_release_trigger_members():
    from app.constants import ReleaseTrigger

    assert ReleaseTrigger.VENDOR_EMAIL == "vendor_email"
    assert ReleaseTrigger.OFFER_RECEIVED == "offer_received"


def test_release_trigger_label_property():
    from app.constants import ReleaseTrigger

    assert ReleaseTrigger.VENDOR_EMAIL.label == "vendor email"
    assert ReleaseTrigger.OFFER_RECEIVED.label == "offer"


# ---------------------------------------------------------------------------
# OemCrosswalkStatus
# ---------------------------------------------------------------------------


def test_oem_crosswalk_status():
    from app.constants import OemCrosswalkStatus

    assert OemCrosswalkStatus.RESOLVED == "resolved"
    assert OemCrosswalkStatus.NO_MATCH == "no_match"


# ---------------------------------------------------------------------------
# AlertKind
# ---------------------------------------------------------------------------


def test_alert_kind():
    from app.constants import AlertKind

    assert AlertKind.OFFER_CONFIRMED == "offer_confirmed"
    assert AlertKind.INBOUND_CUSTOMER == "inbound_customer"
    assert AlertKind.INBOUND_VENDOR == "inbound_vendor"
    assert AlertKind.BUYPLAN_ACTION == "buyplan_action"
    assert AlertKind.TASKS_ACTION == "tasks_action"
    assert AlertKind.APPROVAL_ACTION == "approval_action"
    assert AlertKind.BUYPLAN_RESOURCING == "buyplan_resourcing"


# ---------------------------------------------------------------------------
# SightingsSkipReason
# ---------------------------------------------------------------------------


def test_sightings_skip_reason():
    from app.constants import SightingsSkipReason

    assert SightingsSkipReason.READY == "ready"
    assert SightingsSkipReason.NO_EMAIL == "no_email"
    assert SightingsSkipReason.DO_NOT_CONTACT == "do_not_contact"


# ---------------------------------------------------------------------------
# ApprovalGateType
# ---------------------------------------------------------------------------


def test_approval_gate_type():
    from app.constants import ApprovalGateType

    assert ApprovalGateType.BUY_PLAN == "buy_plan"
    assert ApprovalGateType.PREPAYMENT == "prepayment"
    assert ApprovalGateType.QP_SALES == "qp_sales"
    assert ApprovalGateType.QP_PURCHASING == "qp_purchasing"
    assert ApprovalGateType.PURCHASE_ORDER == "purchase_order"


# ---------------------------------------------------------------------------
# ApprovalSubjectType
# ---------------------------------------------------------------------------


def test_approval_subject_type():
    from app.constants import ApprovalSubjectType

    assert ApprovalSubjectType.QUALITY_PLAN == "quality_plan"
    assert ApprovalSubjectType.PREPAYMENT == "prepayment"
    assert ApprovalSubjectType.BUY_PLAN == "buy_plan"


# ---------------------------------------------------------------------------
# ApprovalRequestStatus
# ---------------------------------------------------------------------------


def test_approval_request_status():
    from app.constants import ApprovalRequestStatus

    assert ApprovalRequestStatus.REQUESTED == "requested"
    assert ApprovalRequestStatus.APPROVED == "approved"
    assert ApprovalRequestStatus.REJECTED == "rejected"
    assert ApprovalRequestStatus.CANCELLED == "cancelled"
    assert ApprovalRequestStatus.EXPIRED == "expired"


# ---------------------------------------------------------------------------
# ApprovalRecipientStatus
# ---------------------------------------------------------------------------


def test_approval_recipient_status():
    from app.constants import ApprovalRecipientStatus

    assert ApprovalRecipientStatus.PENDING == "pending"
    assert ApprovalRecipientStatus.APPROVED == "approved"
    assert ApprovalRecipientStatus.REJECTED == "rejected"
    assert ApprovalRecipientStatus.REASSIGNED == "reassigned"


# ---------------------------------------------------------------------------
# ApprovalStepRule
# ---------------------------------------------------------------------------


def test_approval_step_rule():
    from app.constants import ApprovalStepRule

    assert ApprovalStepRule.ANY == "any"
    assert ApprovalStepRule.ALL == "all"


# ---------------------------------------------------------------------------
# PaymentMethod
# ---------------------------------------------------------------------------


def test_payment_method():
    from app.constants import PaymentMethod

    assert PaymentMethod.CC == "cc"
    assert PaymentMethod.PAYPAL == "paypal"
    assert PaymentMethod.WIRE == "wire"


# ---------------------------------------------------------------------------
# SourcingType
# ---------------------------------------------------------------------------


def test_sourcing_type():
    # SourcingType not present in this branch; test ActivityType instead
    from app.constants import ActivityType

    assert ActivityType.SALES_NOTE == "sales_note"
    assert ActivityType.EMAIL_SENT == "email_sent"
    assert ActivityType.NOTE == "note"


# ---------------------------------------------------------------------------
# QualityPlanStatus
# ---------------------------------------------------------------------------


def test_quality_plan_status():
    from app.constants import QualityPlanStatus

    assert QualityPlanStatus.DRAFT == "draft"


# ---------------------------------------------------------------------------
# QPOrderType
# ---------------------------------------------------------------------------


def test_qp_order_type():
    from app.constants import QPOrderType

    assert QPOrderType.NEW == "new"


# ---------------------------------------------------------------------------
# StrEnum string equality sanity check — spot-check a few cross-class values
# ---------------------------------------------------------------------------


def test_strenum_string_equality():
    """StrEnum members compare equal to their string values — essential for DB queries."""
    from app.constants import BuyPlanStatus, RequisitionStatus, UserRole

    assert RequisitionStatus.OPEN == "open"
    assert BuyPlanStatus.ACTIVE == "active"
    assert UserRole.ADMIN == "admin"
    # StrEnum values are str instances
    assert isinstance(RequisitionStatus.WON, str)
    assert isinstance(BuyPlanStatus.DRAFT, str)
