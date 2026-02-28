"""Database models — re-exports all models for backward compatibility.

Import from here:  from app.models import User, Company, ...
Or from submodules: from app.models.auth import User
"""

# Auth & Users
from .auth import User  # noqa: F401
from .base import Base  # noqa: F401

# System Config
from .config import ApiSource, GraphSubscription, SystemConfig  # noqa: F401

# CRM: Companies & Sites
from .crm import Company, CustomerSite, SiteContact  # noqa: F401

# Discovery / Prospecting
from .discovery_batch import DiscoveryBatch  # noqa: F401
from .prospect_account import ProspectAccount  # noqa: F401

# Enrichment
from .enrichment import (  # noqa: F401
    ClayOAuthToken,
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

# NetComponents Search
from .nc_classification_cache import NcClassificationCache  # noqa: F401
from .nc_search_log import NcSearchLog  # noqa: F401
from .nc_search_queue import NcSearchQueue  # noqa: F401
from .nc_worker_status import NcWorkerStatus  # noqa: F401

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

# Offers, Contacts, Vendor Responses
from .offers import Contact, Offer, OfferAttachment, VendorResponse  # noqa: F401

# Purchase History (Proactive matching backbone)
from .purchase_history import CustomerPartHistory  # noqa: F401

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

# Quotes & Buy Plans (V1 — JSON line_items)
from .quotes import BuyPlan, Quote, QuoteLine  # noqa: F401

# Buy Plans V3 (structured lines, dual approval tracks)
from .buy_plan import BuyPlanLine, BuyPlanV3, VerificationGroupMember  # noqa: F401

# Core: Requisitions, Requirements & Attachments
from .sourcing import (  # noqa: F401
    Requirement,
    RequirementAttachment,
    Requisition,
    RequisitionAttachment,
    Sighting,
)

# Sync
from .sync import SyncLog  # noqa: F401

# Vendors
from .vendors import VendorCard, VendorContact, VendorReview  # noqa: F401
