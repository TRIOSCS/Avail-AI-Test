"""Database models — re-exports all models for backward compatibility.

Import from here:  from app.models import User, Company, ...
Or from submodules: from app.models.auth import User
"""

from .base import Base  # noqa: F401

# Auth & Users
from .auth import User  # noqa: F401

# CRM: Companies & Sites
from .crm import Company, CustomerSite, SiteContact  # noqa: F401

# Core: Requisitions & Requirements
from .sourcing import Requirement, Requisition, Sighting  # noqa: F401

# Offers, Contacts, Vendor Responses
from .offers import Contact, Offer, OfferAttachment, VendorResponse  # noqa: F401

# Quotes & Buy Plans
from .quotes import BuyPlan, Quote  # noqa: F401

# Vendors
from .vendors import VendorCard, VendorContact, VendorReview  # noqa: F401

# Intelligence: Materials, Proactive, Activity
from .intelligence import (  # noqa: F401
    ActivityLog,
    MaterialCard,
    MaterialVendorHistory,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
)

# Performance Tracking
from .performance import (  # noqa: F401
    BuyerLeaderboardSnapshot,
    BuyerVendorStats,
    StockListHash,
    VendorMetricsSnapshot,
)

# Enrichment
from .enrichment import (  # noqa: F401
    EmailSignatureExtract,
    EnrichmentJob,
    EnrichmentQueue,
    IntelCache,
    ProspectContact,
)

# Email Pipeline
from .pipeline import ColumnMappingCache, PendingBatch, ProcessedMessage, SyncState  # noqa: F401

# Sync
from .sync import SyncLog  # noqa: F401

# System Config
from .config import ApiSource, GraphSubscription, SystemConfig  # noqa: F401

# Error Reports / Trouble Tickets
from .error_report import ErrorReport  # noqa: F401
