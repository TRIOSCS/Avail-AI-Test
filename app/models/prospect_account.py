"""Prospect account model — unified pool for suggested accounts."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, String, Text, event
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

    # Persisted CACHE of the composite buyer-ready score. build_priority_snapshot()
    # remains the source of truth; the before_insert/before_update listener below keeps
    # this column in lockstep on every flush so the prospecting list can rank in SQL
    # (order_by + offset/limit) instead of snapshotting every row O(N) per request.
    # Nullable only so a brand-new row exists before the first flush populates it.
    buyer_ready_score = Column(Integer, nullable=True)

    # Persisted CACHE of build_priority_snapshot()["is_buyer_ready"] (migration 218) —
    # a separate boolean mirror of the same snapshot dict buyer_ready_score already
    # caches, kept in lockstep by the same before_insert/before_update listener so the
    # stats panel can SUM this column in SQL instead of re-snapshotting every SUGGESTED
    # row. Nullable pre-backfill/pre-first-flush; callers coalesce to False.
    is_buyer_ready = Column(Boolean, nullable=True)

    # Persisted CACHE of enrichment_data['ai_screen']['verdict'] (migration 218) — a flat,
    # indexed mirror of one JSONB key so the AI-screen-on prospecting lane can
    # filter/sort/paginate in SQL instead of loading the whole pool into Python. The
    # JSONB blob stays the source of truth; the listener below re-derives this column
    # from it on every flush. One of "pass" / "screened_out" / "insufficient_data", or
    # NULL before the account is ever screened.
    ai_screen_verdict = Column(String(32), nullable=True)

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

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # SP4 Park provenance
    swept_from_owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    swept_at = Column(UTCDateTime, nullable=True)
    parked_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # SP4 Phase 4 — compliance cooldown: a former owner cannot reclaim a swept account
    # until this timestamp passes (managers/admins bypass it via reassign). Set at sweep
    # time to swept_at + 30 days; cleared on manager reassign.
    reclaim_blocked_until = Column(UTCDateTime, nullable=True)

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
        Index("ix_prospect_accounts_buyer_ready_score", "buyer_ready_score"),
        Index("ix_prospect_accounts_is_buyer_ready", "is_buyer_ready"),
        Index("ix_prospect_accounts_ai_screen_verdict", "ai_screen_verdict"),
    )


def _sync_buyer_ready_score(_mapper, _connection, target: "ProspectAccount") -> None:
    """Write-through the ``buyer_ready_score`` / ``is_buyer_ready`` /
    ``ai_screen_verdict`` caches before every insert/update.

    build_priority_snapshot() is the single source of truth for the composite buyer-
    ready score and its is_buyer_ready flag; recomputing here on each flush keeps both
    persisted columns consistent with it so the prospecting list and stats panel can
    rank/aggregate in SQL instead of snapshotting every row in memory (migration 218
    added is_buyer_ready alongside the pre-existing buyer_ready_score). The scorer is a
    pure function of the instance's own attributes (no DB/IO), so this is safe inside a
    flush. Imported lazily to keep the model layer free of a hard service import
    (prospect_priority itself imports nothing from app, so there is no cycle either
    way).

    ai_screen_verdict is a flat, indexed mirror of
    enrichment_data['ai_screen']['verdict'] (the JSONB blob stays the source of truth) —
    re-derived here too so every ORM write that sets enrichment_data (e.g.
    prospect_screening.screen_prospect) keeps the mirror in lockstep at zero extra cost,
    without prospect_screening needing to know about it.
    """
    from ..services.prospect_priority import build_priority_snapshot

    snapshot = build_priority_snapshot(target)
    target.buyer_ready_score = snapshot["buyer_ready_score"]
    target.is_buyer_ready = snapshot["is_buyer_ready"]

    enrichment_data: dict = target.enrichment_data if isinstance(target.enrichment_data, dict) else {}
    ai_screen = enrichment_data.get("ai_screen")
    verdict = ai_screen.get("verdict") if isinstance(ai_screen, dict) else None
    # Column is String(32): a non-str or >32-char rogue/legacy JSONB value must degrade
    # to NULL (honest cache miss) rather than fail every subsequent flush of the row.
    if not (isinstance(verdict, str) and len(verdict) <= 32):
        verdict = None
    target.ai_screen_verdict = verdict  # type: ignore[assignment]  # instrumented attr write (legacy Column model)


event.listen(ProspectAccount, "before_insert", _sync_buyer_ready_score)
event.listen(ProspectAccount, "before_update", _sync_buyer_ready_score)
