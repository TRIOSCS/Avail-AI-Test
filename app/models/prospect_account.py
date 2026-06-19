"""Prospect account model — unified pool for suggested accounts."""

from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from ..database import UTCDateTime
from .base import Base


class ProspectAccount(Base):
    """A prospect in the unified pool — SF imports and new discoveries alike.

    The pool only grows: records change status but are never deleted.
    SF-migrated prospects link to existing Company records via company_id.
    New discoveries have company_id=NULL until claimed and converted.
    """

    __tablename__ = "prospect_accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    domain = Column(String(255), unique=True, nullable=False)
    website = Column(String(500))
    industry = Column(String(255))
    naics_code = Column(String(10))
    employee_count_range = Column(String(50))
    revenue_range = Column(String(50))
    hq_location = Column(String(255))
    region = Column(String(50))
    description = Column(Text)
    parent_company_domain = Column(String(255))

    # Scoring
    fit_score = Column(Integer, default=0)
    fit_reasoning = Column(Text)
    readiness_score = Column(Integer, default=0)
    readiness_signals = Column(JSONB, default=dict)

    # AI screening scores (SP3) — populated by prospect_screening.screen_prospect
    trio_match_score = Column(Integer, default=0)
    opportunity_score = Column(Integer, default=0)

    # Discovery tracking
    discovery_source = Column(String(50), nullable=False)
    discovery_batch_id = Column(Integer, ForeignKey("discovery_batches.id", ondelete="SET NULL"))

    # Status lifecycle
    status = Column(String(20), default="suggested")
    import_priority = Column(String(20))

    # Historical context (for SF imports)
    historical_context = Column(JSONB, default=dict)

    # Claim / dismiss
    claimed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    claimed_at = Column(UTCDateTime)
    dismissed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    dismissed_at = Column(UTCDateTime)
    dismiss_reason = Column(String(255))

    # Link to Company (set for SF imports, created on claim for discoveries)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"))

    # Enrichment data
    contacts_preview = Column(JSONB, default=list)
    similar_customers = Column(JSONB, default=list)
    enrichment_data = Column(JSONB, default=dict)
    email_pattern = Column(String(100))
    ai_writeup = Column(Text)
    last_enriched_at = Column(UTCDateTime)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # SP4 Park provenance
    swept_from_owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    swept_at = Column(UTCDateTime, nullable=True)
    parked_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    company = relationship("Company", foreign_keys=[company_id])
    claimed_by_user = relationship("User", foreign_keys=[claimed_by])
    dismissed_by_user = relationship("User", foreign_keys=[dismissed_by])
    discovery_batch = relationship("DiscoveryBatch", foreign_keys=[discovery_batch_id])
    swept_from_owner = relationship("User", foreign_keys=[swept_from_owner_id])
    parked_by_user = relationship("User", foreign_keys=[parked_by_id])

    __table_args__ = (
        Index("ix_prospect_accounts_status", "status"),
        Index("ix_prospect_accounts_fit_score", "fit_score"),
        Index("ix_prospect_accounts_readiness_score", "readiness_score"),
        Index("ix_prospect_accounts_region", "region"),
        Index("ix_prospect_accounts_discovery_source", "discovery_source"),
        Index(
            "ix_prospect_accounts_status_fit",
            "status",
            "fit_score",
        ),
        Index("ix_prospect_accounts_trio_match_score", "trio_match_score"),
        Index("ix_prospect_accounts_opportunity_score", "opportunity_score"),
    )
