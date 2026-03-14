"""Database models — re-exports all models for backward compatibility.

Import from here:  from app.models import User, Company, ...
Or from submodules: from app.models.auth import User
"""

# Auth & Users
from .auth import User  # noqa: F401
from .base import Base  # noqa: F401

# Buy Plans (unified V4 — structured lines, dual approval tracks)
from .buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember  # noqa: F401

# System Config
from .config import ApiSource, ApiUsageLog, GraphSubscription, SystemConfig  # noqa: F401

# CRM: Companies & Sites
from .crm import Company, CustomerSite, SiteContact  # noqa: F401

# Discovery / Prospecting
from .discovery_batch import DiscoveryBatch  # noqa: F401

# Email Intelligence (AI-powered inbox mining)
from .email_intelligence import EmailIntelligence  # noqa: F401

# Enrichment
from .enrichment import (  # noqa: F401
    EmailSignatureExtract,
    EnrichmentCreditUsage,
    EnrichmentJob,
    EnrichmentQueue,
    IntelCache,
    ProspectContact,
)

# Error Reports / Trouble Tickets
from .error_report import ErrorReport  # noqa: F401

# ICsource Search
from .ics_classification_cache import IcsClassificationCache  # noqa: F401
from .ics_search_log import IcsSearchLog  # noqa: F401
from .ics_search_queue import IcsSearchQueue  # noqa: F401
from .ics_worker_status import IcsWorkerStatus  # noqa: F401

# Intelligence: Materials, Proactive, Activity
from .intelligence import (  # noqa: F401
    ActivityLog,
    ChangeLog,
    MaterialCard,
    MaterialCardAudit,
    MaterialVendorHistory,
    ProactiveDoNotOffer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
    ReactivationSignal,
)

# Knowledge Ledger
from .knowledge import KnowledgeConfig, KnowledgeEntry  # noqa: F401

# NetComponents Search
from .nc_classification_cache import NcClassificationCache  # noqa: F401
from .nc_search_log import NcSearchLog  # noqa: F401
from .nc_search_queue import NcSearchQueue  # noqa: F401
from .nc_worker_status import NcWorkerStatus  # noqa: F401

# Notifications
from .notification import Notification  # noqa: F401

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
from .prospect_account import ProspectAccount  # noqa: F401

# Purchase History (Proactive matching backbone)
from .purchase_history import CustomerPartHistory  # noqa: F401

# Quotes (V1 BuyPlan model removed — use BuyPlan from buy_plan module)
from .quotes import Quote, QuoteLine  # noqa: F401

# Risk Flags (structured deal intelligence)
from .risk_flag import RiskFlag  # noqa: F401

# Core: Requisitions, Requirements & Attachments
from .sourcing import (  # noqa: F401
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

# Teams alert config (per-user DM preferences)
from .teams_alert_config import TeamsAlertConfig  # noqa: F401

# Teams notification audit log
from .teams_notification_log import TeamsNotificationLog  # noqa: F401
from .trouble_ticket import TroubleTicket  # noqa: F401

# Unified Score (cross-role leaderboard)
from .unified_score import UnifiedScoreSnapshot  # noqa: F401

# Vendors
from .vendors import VendorCard, VendorContact, VendorReview  # noqa: F401
