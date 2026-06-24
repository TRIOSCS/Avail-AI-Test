"""Database models — re-exports all models for backward compatibility.

Import from here:  from app.models import User, Company, ...
Or from submodules: from app.models.auth import User
"""

# Alert read-state (per-user seen-state for cross-app alerts)
from .alert_seen import AlertSeen  # noqa: F401

# Auth & Users
from .auth import User  # noqa: F401
from .base import Base  # noqa: F401

# Buy Plans (unified V4 — structured lines, dual approval tracks)
from .buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember  # noqa: F401

# System Config
from .config import ApiSource, ApiUsageLog, GraphSubscription, SystemConfig  # noqa: F401

# CRM: Companies & Sites
from .crm import (  # noqa: F401
    AccountCollaborator,
    Company,
    CompanyAttachment,
    CustomerSite,
    SiteContact,
    SiteContactAttachment,
)

# Discovery / Prospecting
from .discovery_batch import DiscoveryBatch  # noqa: F401

# Email Intelligence (AI-powered inbox mining)
from .email_intelligence import EmailIntelligence  # noqa: F401

# Enrichment
from .enrichment import (  # noqa: F401
    EmailSignatureExtract,
    EnrichmentJob,
    EnrichmentQueue,
    IntelCache,
    ProspectContact,
)

# Enrichment Pipeline State
from .enrichment_run import EnrichmentRun  # noqa: F401

# Enrichment Worker Status
from .enrichment_worker_status import EnrichmentWorkerStatus  # noqa: F401

# Excess Inventory / Resell (resell-brokerage) offers
from .excess import (  # noqa: F401
    BuyerScore,
    CustomerBid,
    CustomerBidLine,
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
    ExcessOutreach,
)

# Faceted Search
from .faceted_search import CommoditySpecSchema, MaterialSpecFacet  # noqa: F401

# FRU crosswalk (IBM/Lenovo FRU ↔ 11S ↔ model ↔ tray relationships)
from .fru_link import FruLink  # noqa: F401

# ICsource Search
from .ics_search_log import IcsSearchLog  # noqa: F401
from .ics_search_queue import IcsSearchQueue  # noqa: F401
from .ics_worker_status import IcsWorkerStatus  # noqa: F401
from .intelligence import (  # noqa: F401
    ActivityDigest,
    ActivityLog,
    ChangeLog,
    MaterialCard,
    MaterialCardAttachment,
    MaterialCardAudit,
    MaterialCardDatasheet,
    MaterialVendorHistory,
    ProactiveDoNotOffer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
)

# Knowledge Ledger
from .knowledge import KnowledgeConfig, KnowledgeEntry  # noqa: F401

# NetComponents Search
from .nc_search_log import NcSearchLog  # noqa: F401
from .nc_search_queue import NcSearchQueue  # noqa: F401
from .nc_worker_status import NcWorkerStatus  # noqa: F401

# OEM web-resolution crosswalk (PartSurfer/PSREF spare → canonical MPN cache)
from .oem_crosswalk import OemCrosswalk  # noqa: F401

# Offers, Contacts, Vendor Responses
from .offers import Contact, Offer, OfferAttachment, VendorResponse  # noqa: F401

# Performance Tracking
from .performance import (  # noqa: F401
    AvailScoreSnapshot,
    BuyerLeaderboardSnapshot,
    BuyerVendorStats,
    MultiplierScoreSnapshot,
    StockListHash,
    VendorMetricsSnapshot,
)

# Email Pipeline
from .pipeline import ColumnMappingCache, PendingBatch, ProcessedMessage, SyncState  # noqa: F401

# Intelligence: Materials, Proactive, Activity, Price Snapshots
from .price_snapshot import MaterialPriceSnapshot  # noqa: F401
from .prospect_account import ProspectAccount  # noqa: F401

# Purchase History (Proactive matching backbone)
from .purchase_history import CustomerPartHistory  # noqa: F401

# Quotes (V1 BuyPlan model removed — use BuyPlan from buy_plan module)
from .quotes import Quote, QuoteLine  # noqa: F401
from .root_cause_group import RootCauseGroup  # noqa: F401

# Core: Requisitions, Requirements & Attachments
from .sourcing import (  # noqa: F401
    Manufacturer,
    Requirement,
    RequirementAttachment,
    Requisition,
    RequisitionAttachment,
    Sighting,
)
from .sourcing_lead import LeadEvidence, LeadFeedbackEvent, SourcingLead  # noqa: F401

# Strategic Vendors (per-buyer assignments with 39-day TTL)
from .strategic import StrategicVendor  # noqa: F401

# Sync
from .sync import SyncLog  # noqa: F401

# Tagging (AI classification + entity propagation)
from .tags import EntityTag, MaterialTag, Tag, TagThresholdConfig  # noqa: F401

# Task Board (pipeline tasks per requisition)
from .task import RequisitionTask  # noqa: F401

# The Broker Forum (TBF) Search
from .tbf_search_log import TbfSearchLog  # noqa: F401
from .tbf_search_queue import TbfSearchQueue  # noqa: F401
from .tbf_worker_status import TbfWorkerStatus  # noqa: F401

# Trust telemetry (durable reconcile tallies + facet-audit verdicts)
from .telemetry import FacetAudit, ReconcileRun  # noqa: F401
from .trouble_ticket import TroubleTicket  # noqa: F401

# Unified Score (cross-role leaderboard)
from .unified_score import UnifiedScoreSnapshot  # noqa: F401

# Vendor+Part Unavailability (durable "stock is gone" knowledge per vendor+MPN)
from .vendor_part_unavailability import VendorPartUnavailability  # noqa: F401

# Vendor Sighting Summary (materialized vendor-level sighting aggregation)
from .vendor_sighting_summary import VendorSightingSummary  # noqa: F401

# Vendors
from .vendors import (  # noqa: F401
    VendorCard,
    VendorCardAttachment,
    VendorContact,
    VendorContactAttachment,
    VendorReview,
)
