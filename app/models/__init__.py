"""Database models — re-exports all models for backward compatibility.

Import from here:  from app.models import User, Company, ...
Or from submodules: from app.models.auth import User
"""

# Alert read-state (per-user seen-state for cross-app alerts)
from .alert_seen import AlertSeen  # noqa: F401

# Approvals engine (5 tables: request, step, recipient, event, outbox)
# Approver eligibility uses per-user toggles on the User model — no gate-config table.
from .approvals import (
    ApprovalEvent,  # noqa: F401
    ApprovalOutbox,  # noqa: F401
    ApprovalRequest,  # noqa: F401
    ApprovalStep,  # noqa: F401
    ApprovalStepRecipient,  # noqa: F401
)

# Auth & Users
from .auth import User, UserAdminAudit  # noqa: F401
from .base import Base  # noqa: F401

# Buy Plans (unified V4 — structured lines, dual approval tracks)
from .buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember  # noqa: F401

# System Config
from .config import ApiSource, ApiUsageLog, GraphSubscription, SystemConfig  # noqa: F401

# CRM: Companies & Sites
from .crm import (
    AccountCollaborator,  # noqa: F401
    Company,  # noqa: F401
    CompanyAttachment,  # noqa: F401
    CrmFieldHistory,  # noqa: F401
    CustomerSite,  # noqa: F401
    SavedView,  # noqa: F401
    SiteContact,  # noqa: F401
    SiteContactAttachment,  # noqa: F401
)

# Discovery / Prospecting
from .discovery_batch import DiscoveryBatch  # noqa: F401

# Email Intelligence (AI-powered inbox mining)
from .email_intelligence import EmailIntelligence  # noqa: F401

# Enrichment
from .enrichment import (
    EmailSignatureExtract,  # noqa: F401
    EnrichmentJob,  # noqa: F401
    EnrichmentQueue,  # noqa: F401
    ProspectContact,  # noqa: F401
)

# Enrichment Pipeline State
from .enrichment_run import EnrichmentRun  # noqa: F401

# Enrichment Worker Status
from .enrichment_worker_status import EnrichmentWorkerStatus  # noqa: F401

# Excess Inventory / Resell (resell-brokerage) offers
from .excess import (
    BuyerScore,  # noqa: F401
    CustomerBid,  # noqa: F401
    CustomerBidLine,  # noqa: F401
    ExcessLineItem,  # noqa: F401
    ExcessList,  # noqa: F401
    ExcessOffer,  # noqa: F401
    ExcessOfferLine,  # noqa: F401
    ExcessOutreach,  # noqa: F401
)

# Faceted Search
from .faceted_search import CommoditySpecSchema, MaterialSpecFacet  # noqa: F401

# FRU crosswalk (IBM/Lenovo FRU ↔ 11S ↔ model ↔ tray relationships)
from .fru_link import FruLink  # noqa: F401

# ICsource Search
from .ics_search_log import IcsSearchLog  # noqa: F401
from .ics_search_queue import IcsSearchQueue  # noqa: F401
from .ics_worker_status import IcsWorkerStatus  # noqa: F401
from .intelligence import (
    ActivityDigest,  # noqa: F401
    ActivityLog,  # noqa: F401
    ChangeLog,  # noqa: F401
    MaterialCard,  # noqa: F401
    MaterialCardAttachment,  # noqa: F401
    MaterialCardAudit,  # noqa: F401
    MaterialCardDatasheet,  # noqa: F401
    MaterialVendorHistory,  # noqa: F401
    ProactiveDoNotOffer,  # noqa: F401
    ProactiveMatch,  # noqa: F401
    ProactiveOffer,  # noqa: F401
    ProactiveThrottle,  # noqa: F401
)

# Knowledge Ledger
from .knowledge import KnowledgeEntry  # noqa: F401

# NetComponents Search
from .nc_search_log import NcSearchLog  # noqa: F401
from .nc_search_queue import NcSearchQueue  # noqa: F401
from .nc_worker_status import NcWorkerStatus  # noqa: F401

# In-app notifications (registered so the table is in Base.metadata; the model file's
# docstring claimed this but the import was missing — schema-drift gate flagged it)
from .notification import Notification  # noqa: F401

# OEM web-resolution crosswalk (PartSurfer/PSREF spare → canonical MPN cache)
from .oem_crosswalk import OemCrosswalk  # noqa: F401

# Offers, Contacts, Vendor Responses
from .offers import Contact, Offer, OfferAttachment, VendorResponse  # noqa: F401
from .partsurfer_desc_negative import PartsurferDescNegative  # noqa: F401

# Performance Tracking
from .performance import (
    AvailScoreSnapshot,  # noqa: F401
    BuyerLeaderboardSnapshot,  # noqa: F401
    BuyerVendorStats,  # noqa: F401
    MultiplierScoreSnapshot,  # noqa: F401
    StockListHash,  # noqa: F401
    VendorMetricsSnapshot,  # noqa: F401
)

# Email Pipeline
from .pipeline import ColumnMappingCache, PendingBatch, ProcessedMessage, SyncState  # noqa: F401

# PO cancellations (immutable vendor-fall-down fact powering cancellation metrics)
from .po_cancellation import POCancellation  # noqa: F401

# Intelligence: Materials, Proactive, Activity, Price Snapshots
from .price_snapshot import MaterialPriceSnapshot  # noqa: F401
from .prospect_account import ProspectAccount  # noqa: F401

# Purchase History (Proactive matching backbone)
from .purchase_history import CustomerPartHistory  # noqa: F401

# Quality Plans + Prepayments (QP workflow subjects)
from .quality_plan import Prepayment, QpFruLookup, QpSerialEntry, QualityPlan  # noqa: F401

# Quotes (V1 BuyPlan model removed — use BuyPlan from buy_plan module)
from .quotes import Quote, QuoteLine, QuoteRequisition  # noqa: F401
from .root_cause_group import RootCauseGroup  # noqa: F401

# Core: Requisitions, Requirements & Attachments
from .sourcing import (
    Manufacturer,  # noqa: F401
    Requirement,  # noqa: F401
    RequirementAttachment,  # noqa: F401
    Requisition,  # noqa: F401
    RequisitionAttachment,  # noqa: F401
    Sighting,  # noqa: F401
)
from .sourcing_lead import LeadEvidence, LeadFeedbackEvent, SourcingLead  # noqa: F401

# Strategic Vendors (per-buyer assignments with 39-day TTL)
from .strategic import StrategicVendor  # noqa: F401

# Tagging (AI classification + entity propagation)
from .tags import EntityTag, MaterialTag, Tag, TagThresholdConfig  # noqa: F401

# Task Board (pipeline tasks per requisition)
from .task import RequisitionTask  # noqa: F401

# The Broker Forum (TBF) Search
from .tbf_search_log import TbfSearchLog  # noqa: F401
from .tbf_search_queue import TbfSearchQueue  # noqa: F401
from .tbf_worker_status import TbfWorkerStatus  # noqa: F401

# Trust telemetry (durable reconcile tallies)
from .telemetry import ReconcileRun  # noqa: F401
from .trouble_ticket import TroubleTicket  # noqa: F401

# Unified Score (cross-role leaderboard)
from .unified_score import UnifiedScoreSnapshot  # noqa: F401

# Vendor+Part Unavailability (durable "stock is gone" knowledge per vendor+MPN)
from .vendor_part_unavailability import VendorPartUnavailability  # noqa: F401

# Vendor Sighting Summary (materialized vendor-level sighting aggregation)
from .vendor_sighting_summary import VendorSightingSummary  # noqa: F401

# Vendors
from .vendors import (
    VendorCard,  # noqa: F401
    VendorCardAttachment,  # noqa: F401
    VendorContact,  # noqa: F401
    VendorContactAttachment,  # noqa: F401
    VendorReview,  # noqa: F401
)
