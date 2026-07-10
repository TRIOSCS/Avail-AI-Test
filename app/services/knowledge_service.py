"""Knowledge Ledger service — CRUD, Q&A, auto-capture, AI context engine.

Central service for the knowledge base. Handles entry creation, Q&A
threading, notification triggers, auto-capture from quotes/offers,
and AI insight generation.

Called by: routers/htmx/insights_views.py (insight panels), routers/crm/quotes.py,
routers/crm/offers.py, email_service.py, services/quote_builder_service.py,
services/email_intelligence_service.py
Depends on: models/knowledge.py, utils/claude_client.py
"""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models.knowledge import KnowledgeEntry

# Expiry defaults (days)
EXPIRY_PRICE_FACT = 90
EXPIRY_AI_INSIGHT = 30


def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
    """Check if a timestamp is expired, handling naive/aware datetime comparison."""
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < now


def create_entry(
    db: Session,
    *,
    user_id: int,
    entry_type: str,
    content: str,
    source: str = "manual",
    confidence: float | None = None,
    expires_at: datetime | None = None,
    mpn: str | None = None,
    vendor_card_id: int | None = None,
    company_id: int | None = None,
    requisition_id: int | None = None,
    requirement_id: int | None = None,
    parent_id: int | None = None,
    assigned_to_ids: list[int] | None = None,
    commit: bool = True,
) -> KnowledgeEntry:
    """Create a knowledge entry with optional entity linkage."""
    entry = KnowledgeEntry(
        entry_type=entry_type,
        content=content,
        source=source,
        confidence=confidence,
        expires_at=expires_at,
        created_by=user_id,
        mpn=mpn,
        vendor_card_id=vendor_card_id,
        company_id=company_id,
        requisition_id=requisition_id,
        requirement_id=requirement_id,
        parent_id=parent_id,
        assigned_to_ids=assigned_to_ids or [],
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(entry)
    logger.info("Knowledge entry created: id={} type={} source={}", entry.id, entry_type, source)
    return entry


# ---------------------------------------------------------------------------
# Auto-capture: extract facts from quotes, offers, and RFQ responses
# ---------------------------------------------------------------------------


def capture_quote_fact(db: Session, *, quote, user_id: int) -> KnowledgeEntry | None:
    """Auto-capture price facts when a quote is created.

    Uses a savepoint so a create failure doesn't corrupt the caller's transaction (the
    quote it just created), then commits its own entry. Callers treat this as fire-and-
    forget and several (create_quote, build_quote) return without a further commit, so
    the capture MUST persist the entry itself or it rolls back at session close. Mirrors
    capture_offer_fact. Called from: app/routers/crm/quotes.py and
    app/services/quote_builder_service.py after quote creation.
    """
    nested = db.begin_nested()
    try:
        line_items = quote.line_items or []
        if not line_items:
            nested.rollback()
            return None

        facts = []
        for item in line_items:
            mpn = item.get("mpn") or item.get("part_number", "")
            price = item.get("unit_sell") or item.get("sell_price")
            qty = item.get("qty") or item.get("quantity")
            vendor = item.get("vendor_name", "")
            if mpn and price:
                facts.append(
                    f"{mpn}: ${float(price):.2f}" + (f" x{qty}" if qty else "") + (f" from {vendor}" if vendor else "")
                )

        if not facts:
            nested.rollback()
            return None

        content = "Quote #{} — {}".format(quote.quote_number, "; ".join(facts))
        entry = create_entry(
            db,
            user_id=user_id,
            entry_type="fact",
            content=content,
            source="system",
            confidence=1.0,
            expires_at=datetime.now(UTC) + timedelta(days=EXPIRY_PRICE_FACT),
            requisition_id=quote.requisition_id,
            commit=False,
        )
        nested.commit()
    except Exception as e:
        nested.rollback()
        logger.warning("Failed to capture quote fact: {}", e)
        return None
    # Persist the released savepoint. Kept OUTSIDE the savepoint block so a commit error
    # can't roll back an already-released savepoint; the caller's own (already-committed)
    # work is unaffected either way.
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("Failed to persist quote fact: {}", e)
        return None
    return entry


def capture_offer_fact(db: Session, *, offer, user_id: int | None = None) -> KnowledgeEntry | None:
    """Auto-capture facts when an offer is created (manual or parsed).

    Uses a savepoint so FK failures don't corrupt the caller's transaction, then commits
    its own entry — callers treat this as fire-and-forget and don't reliably commit
    afterward, so the capture must persist the entry itself or it rolls back at session
    close. Called from: app/routers/crm/offers.py, app/email_service.py
    """
    nested = db.begin_nested()
    try:
        mpn = getattr(offer, "mpn", None) or ""
        price = getattr(offer, "unit_price", None)
        qty = getattr(offer, "quantity", None)
        vendor_name = getattr(offer, "vendor_name", None) or ""
        lead_time = getattr(offer, "lead_time", None)

        content_parts = []
        if mpn:
            content_parts.append(f"MPN: {mpn}")
        if price:
            content_parts.append(f"${float(price):.2f}")
        if qty:
            content_parts.append(f"qty {qty}")
        if vendor_name:
            content_parts.append(f"from {vendor_name}")
        if lead_time:
            content_parts.append(f"lead time: {lead_time}")

        if not content_parts:
            nested.rollback()
            return None

        content = "Offer — " + ", ".join(content_parts)
        entry = create_entry(
            db,
            user_id=user_id,
            entry_type="fact",
            content=content,
            source="system",
            confidence=1.0,
            expires_at=datetime.now(UTC) + timedelta(days=EXPIRY_PRICE_FACT),
            mpn=mpn or None,
            vendor_card_id=getattr(offer, "vendor_card_id", None),
            requisition_id=getattr(offer, "requisition_id", None),
            commit=False,
        )
        nested.commit()
    except Exception as e:
        nested.rollback()
        logger.warning("Failed to capture offer fact: {}", e)
        return None
    # Persist the released savepoint (see capture_quote_fact — kept outside the savepoint
    # block so a commit error can't roll back an already-released savepoint).
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("Failed to persist offer fact: {}", e)
        return None
    return entry


# ---------------------------------------------------------------------------
# AI Context Engine: build context and generate insights
# ---------------------------------------------------------------------------

INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "confidence": {"type": "number"},
                    "based_on_expired": {"type": "boolean"},
                },
                "required": ["content", "confidence", "based_on_expired"],
            },
        },
    },
    "required": ["insights"],
}

INSIGHT_SYSTEM_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given knowledge entries about parts, vendors, customers, and requisitions, generate 3-5 actionable insights.

Focus on:
- Price history and trends (compare current vs past quotes)
- Cross-requisition opportunities (same MPN needed elsewhere)
- Vendor reliability patterns
- Lead time warnings
- Customer buying patterns

Entries marked [OUTDATED] are expired — mention they may be outdated. Weight them at 0.3x.
Keep each insight to 1-2 sentences. Be specific with numbers, dates, and names."""


def build_context(db: Session, *, requisition_id: int) -> str:
    """Gather all relevant knowledge for a requisition and format for AI prompt."""
    from app.models.sourcing import Requirement, Requisition

    req = db.get(Requisition, requisition_id)
    if not req:
        return ""

    now = datetime.now(UTC)
    sections = []

    # 1. Direct knowledge on this req
    direct = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.requisition_id == requisition_id)
        .filter(KnowledgeEntry.entry_type != "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(50)
        .all()
    )
    if direct:
        lines = []
        for e in direct:
            prefix = "[OUTDATED] " if _is_expired(e.expires_at, now) else ""
            lines.append(
                "- {}{}: {} (source: {}, {})".format(
                    prefix, e.entry_type, e.content, e.source, e.created_at.strftime("%Y-%m-%d")
                )
            )
        sections.append("## Direct knowledge for this requisition\n" + "\n".join(lines))

    # 2. MPN knowledge from other reqs
    mpns = [
        r.primary_mpn
        for r in db.query(Requirement.primary_mpn).filter(Requirement.requisition_id == requisition_id).all()
        if r.primary_mpn
    ]
    if mpns:
        mpn_entries = (
            db.query(KnowledgeEntry)
            .filter(KnowledgeEntry.mpn.in_(mpns))
            .filter(KnowledgeEntry.requisition_id != requisition_id)
            .filter(KnowledgeEntry.entry_type != "ai_insight")
            .order_by(KnowledgeEntry.created_at.desc())
            .limit(30)
            .all()
        )
        if mpn_entries:
            lines = []
            for e in mpn_entries:
                prefix = "[OUTDATED] " if _is_expired(e.expires_at, now) else ""
                lines.append(
                    "- {}{}: {} (req #{}, {})".format(
                        prefix, e.mpn, e.content, e.requisition_id, e.created_at.strftime("%Y-%m-%d")
                    )
                )
            sections.append("## Same MPNs on other requisitions\n" + "\n".join(lines))

    # 3. Vendor knowledge
    from app.models.offers import Offer

    vendor_ids = [
        o.vendor_card_id
        for o in db.query(Offer.vendor_card_id)
        .filter(Offer.requisition_id == requisition_id, Offer.vendor_card_id.isnot(None))
        .distinct()
        .all()
    ]
    if vendor_ids:
        vendor_entries = (
            db.query(KnowledgeEntry)
            .filter(KnowledgeEntry.vendor_card_id.in_(vendor_ids))
            .filter(KnowledgeEntry.entry_type != "ai_insight")
            .order_by(KnowledgeEntry.created_at.desc())
            .limit(20)
            .all()
        )
        if vendor_entries:
            lines = []
            for e in vendor_entries:
                prefix = "[OUTDATED] " if _is_expired(e.expires_at, now) else ""
                lines.append(
                    "- {}Vendor #{}: {} ({})".format(
                        prefix, e.vendor_card_id, e.content, e.created_at.strftime("%Y-%m-%d")
                    )
                )
            sections.append("## Vendor intelligence\n" + "\n".join(lines))

    # 4. Company knowledge
    if req.company_id:
        company_entries = (
            db.query(KnowledgeEntry)
            .filter(KnowledgeEntry.company_id == req.company_id)
            .filter(KnowledgeEntry.entry_type != "ai_insight")
            .order_by(KnowledgeEntry.created_at.desc())
            .limit(20)
            .all()
        )
        if company_entries:
            lines = []
            for e in company_entries:
                prefix = "[OUTDATED] " if _is_expired(e.expires_at, now) else ""
                lines.append("- {}{} ({})".format(prefix, e.content, e.created_at.strftime("%Y-%m-%d")))
            sections.append("## Customer intelligence\n" + "\n".join(lines))

    if not sections:
        return ""

    return "\n\n".join(sections)


# Interactive (HTTP-request-scoped) callers — the four "Refresh AI insights"
# HTMX endpoints in routers/htmx/insights_views.py — tighten the Claude call
# budget so a slow/overloaded API can't hold the request open for the full
# timeout × retries worst case (P2.8). Non-interactive callers (none today —
# the KB-insight refresh job was deleted 2026-07-06) get the original
# claude_structured defaults (30s timeout, 3 attempts = up to ~96s per call).
_INTERACTIVE_TIMEOUT_SECONDS = 25
_INTERACTIVE_MAX_ATTEMPTS = 1


async def _regenerate_insights(
    db: Session,
    *,
    context: str,
    delete_filters: tuple,
    prompt: str,
    system: str,
    entry_kwargs: dict,
    no_context_log: str,
    unavailable_log: str,
    failed_log: str,
    no_results_log: str,
    generated_log: str,
    generated_args: tuple = (),
    interactive: bool = False,
) -> list[KnowledgeEntry]:
    """Shared insight pipeline: replace cached insights for one scope.

    Deletes the existing ``ai_insight`` rows matched by ``delete_filters``, asks
    Claude for fresh insights, and recreates them linked via ``entry_kwargs``.
    The single-arg ``*_log`` strings are pre-formatted (logged verbatim) so each
    caller keeps its exact wording; ``generated_log``/``failed_log`` are loguru
    templates whose runtime values (count, scope id, error) are passed as args.

    ``interactive=True`` (set by the HTMX "Refresh" endpoints) tightens the
    Claude call to a ~25s timeout with a single attempt (no retries) so the
    request can't block for the full default 30s × 3-retry worst case. On
    timeout/failure this returns ``[]``, and callers fall back to serving the
    existing cached insights (``entries or get_cached_*_insights(...)``).
    """
    from app.utils.claude_client import claude_structured
    from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError

    if not context:
        logger.debug(no_context_log)
        return []

    # Snapshot (but do NOT yet delete) the cached insights. We only replace them
    # once fresh insights are in hand — a failed/empty AI call must leave the old
    # rows intact rather than wiping the cache with nothing to show for it.
    old_insights = db.query(KnowledgeEntry).filter(*delete_filters).all()

    claude_kwargs: dict = {}
    if interactive:
        claude_kwargs["timeout"] = _INTERACTIVE_TIMEOUT_SECONDS
        claude_kwargs["max_attempts"] = _INTERACTIVE_MAX_ATTEMPTS

    try:
        result = await claude_structured(
            prompt=prompt,
            schema=INSIGHT_SCHEMA,
            system=system,
            model_tier="smart",
            max_tokens=2048,
            thinking_budget=5000,
            **claude_kwargs,
        )
    except ClaudeUnavailableError:
        logger.info(unavailable_log)
        return []
    except ClaudeError as e:
        logger.warning(failed_log, e)
        return []

    if not result or not result.get("insights"):
        logger.warning(no_results_log)
        return []

    # Fresh insights are available — safe to swap out the old cached rows now.
    for old in old_insights:
        db.delete(old)
    db.flush()

    entries = []
    now = datetime.now(UTC)
    for insight in result["insights"][:5]:  # Cap at 5
        entry = create_entry(
            db,
            user_id=None,  # system
            entry_type="ai_insight",
            content=insight["content"],
            source="ai_generated",
            confidence=insight.get("confidence", 0.8),
            expires_at=now + timedelta(days=EXPIRY_AI_INSIGHT),
            **entry_kwargs,
        )
        entries.append(entry)

    logger.info(generated_log, len(entries), *generated_args)
    return entries


async def generate_insights(db: Session, requisition_id: int, *, interactive: bool = False) -> list[KnowledgeEntry]:
    """Generate AI insights for a requisition using the context engine.

    ``interactive=True`` (the HTMX refresh endpoint) tightens the Claude call
    budget — see ``_regenerate_insights``. Defaults to False (background job).
    """
    context = build_context(db, requisition_id=requisition_id)
    return await _regenerate_insights(
        db,
        context=context,
        delete_filters=(
            KnowledgeEntry.requisition_id == requisition_id,
            KnowledgeEntry.entry_type == "ai_insight",
        ),
        prompt=f"Analyze this knowledge base and generate insights:\n\n{context}",
        system=INSIGHT_SYSTEM_PROMPT,
        entry_kwargs={"requisition_id": requisition_id},
        no_context_log=f"No context for req {requisition_id} — skipping insight generation",
        unavailable_log="Claude not configured — skipping insight generation",
        failed_log="Claude AI failed for insight generation: {}",
        no_results_log=f"AI insight generation returned no results for req {requisition_id}",
        generated_log="Generated {} insights for req {}",
        generated_args=(requisition_id,),
        interactive=interactive,
    )


def get_cached_insights(db: Session, requisition_id: int) -> list[KnowledgeEntry]:
    """Return pre-computed AI insights for a requisition."""
    return (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.requisition_id == requisition_id,
            KnowledgeEntry.entry_type == "ai_insight",
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# Entity-scoped context builders and insight generators
# ---------------------------------------------------------------------------

VENDOR_INSIGHT_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given knowledge entries about a specific vendor, generate 3-5 actionable insights.

Focus on:
- Response patterns (speed, consistency, ghosting)
- Pricing competitiveness relative to other vendors
- Part specialization (what categories do they dominate?)
- Red flags (cancellation rates, quality issues, declining engagement)

Entries marked [OUTDATED] are expired — mention they may be outdated. Weight them at 0.3x.
Keep each insight to 1-2 sentences. Be specific with numbers, dates, and names."""

PIPELINE_INSIGHT_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given a summary of the active requisition pipeline, generate 3-5 actionable insights.

Focus on:
- Stalling deals (requisitions with no recent activity)
- Coverage gaps (MPNs with few or no offers)
- Win/loss trends (status distribution over time)
- Pipeline health (bottlenecks, overloaded buyers, deadlines at risk)

Keep each insight to 1-2 sentences. Be specific with numbers, dates, and names."""

COMPANY_INSIGHT_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given knowledge entries about a specific customer company, generate 3-5 actionable insights.

Focus on:
- Engagement trends (increasing/decreasing order frequency)
- Open deal status and progress
- Response time and communication patterns
- Relationship health (strategic value, risk of churn, growth potential)

Entries marked [OUTDATED] are expired — mention they may be outdated. Weight them at 0.3x.
Keep each insight to 1-2 sentences. Be specific with numbers, dates, and names."""


def build_vendor_context(db: Session, *, vendor_card_id: int) -> str:
    """Gather all relevant knowledge for a vendor and format for AI prompt."""
    from app.models.offers import Offer
    from app.models.vendors import VendorCard

    now = datetime.now(UTC)
    sections = []

    vendor = db.get(VendorCard, vendor_card_id)
    if not vendor:
        return ""

    sections.append(f"## Vendor: {vendor.display_name} (ID {vendor.id})")
    meta = []
    if vendor.domain:
        meta.append(f"Domain: {vendor.domain}")
    if vendor.industry:
        meta.append(f"Industry: {vendor.industry}")
    if vendor.ghost_rate is not None:
        meta.append(f"Ghost rate: {vendor.ghost_rate:.0%}")
    if vendor.total_responses is not None and vendor.total_outreach:
        meta.append(
            f"Response rate: {vendor.total_responses}/{vendor.total_outreach} ({vendor.total_responses / max(vendor.total_outreach, 1):.0%})"
        )
    if vendor.cancellation_rate is not None:
        meta.append(f"Cancellation rate: {vendor.cancellation_rate:.0%}")
    if meta:
        sections.append("## Vendor stats\n" + "\n".join("- " + m for m in meta))

    # Knowledge entries
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.vendor_card_id == vendor_card_id)
        .filter(KnowledgeEntry.entry_type != "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(40)
        .all()
    )
    if entries:
        lines = []
        for e in entries:
            prefix = "[OUTDATED] " if _is_expired(e.expires_at, now) else ""
            lines.append("- {}{}: {} ({})".format(prefix, e.entry_type, e.content, e.created_at.strftime("%Y-%m-%d")))
        sections.append("## Knowledge entries\n" + "\n".join(lines))

    # Offer history
    offers = (
        db.query(Offer).filter(Offer.vendor_card_id == vendor_card_id).order_by(Offer.created_at.desc()).limit(30).all()
    )
    if offers:
        lines = []
        for o in offers:
            price_str = f"${float(o.unit_price):.4f}" if o.unit_price else "N/A"
            lines.append(
                "- {} {} qty:{} lead:{} status={} (req #{}, {})".format(
                    o.mpn,
                    price_str,
                    o.qty_available or "?",
                    o.lead_time or "?",
                    o.status,
                    o.requisition_id,
                    o.created_at.strftime("%Y-%m-%d"),
                )
            )
        sections.append("## Recent offers\n" + "\n".join(lines))

    return "\n\n".join(sections)


def build_pipeline_context(db: Session) -> str:
    """Gather pipeline-level context for AI analysis."""
    from app.models.sourcing import Requisition

    now = datetime.now(UTC)
    sections = []

    # 1. Status breakdown
    all_reqs = db.query(Requisition).order_by(Requisition.created_at.desc()).limit(200).all()
    if not all_reqs:
        return ""

    status_counts: dict[str, int] = {}
    for r in all_reqs:
        status_counts[r.status or "unknown"] = status_counts.get(r.status or "unknown", 0) + 1
    lines = [f"- {s}: {c}" for s, c in sorted(status_counts.items(), key=lambda x: -x[1])]
    sections.append("## Pipeline status breakdown (last 200 reqs)\n" + "\n".join(lines))

    # 2. Active reqs summary
    active = [
        r
        for r in all_reqs
        if r.status
        in (
            RequisitionStatus.OPEN,
            RequisitionStatus.RFQS_SENT,
            RequisitionStatus.QUOTED,
        )
    ]
    if active:
        lines = []
        for r in active[:30]:
            age_days = (
                (now - r.created_at.replace(tzinfo=UTC) if r.created_at.tzinfo is None else now - r.created_at).days
                if r.created_at
                else 0
            )
            lines.append(
                "- Req #{} '{}' — {} days old, deadline: {}".format(
                    r.id,
                    r.name,
                    age_days,
                    r.deadline or "none",
                )
            )
        sections.append("## Active requisitions\n" + "\n".join(lines))

    # 3. Stale deals (active but no update in 14+ days)
    stale_threshold = now - timedelta(days=14)
    stale = []
    for r in active:
        if not r.updated_at:
            continue
        ts = r.updated_at if r.updated_at.tzinfo else r.updated_at.replace(tzinfo=UTC)
        if ts < stale_threshold:
            stale.append(r)
    if stale:
        lines = []
        for r in stale[:20]:
            lines.append(
                "- Req #{} '{}' — last updated {}".format(
                    r.id,
                    r.name,
                    r.updated_at.strftime("%Y-%m-%d") if r.updated_at else "never",
                )
            )
        sections.append("## Stale deals (no update in 14+ days)\n" + "\n".join(lines))

    return "\n\n".join(sections)


def build_company_context(db: Session, *, company_id: int) -> str:
    """Gather all relevant knowledge for a company and format for AI prompt."""
    from app.models.crm import Company, CustomerSite
    from app.models.sourcing import Requisition

    now = datetime.now(UTC)
    sections = []

    company = db.get(Company, company_id)
    if not company:
        return ""

    # Company header
    meta = [f"Name: {company.name}"]
    if company.industry:
        meta.append(f"Industry: {company.industry}")
    if company.account_type:
        meta.append(f"Account type: {company.account_type}")
    if company.is_strategic:
        meta.append("Strategic account: Yes")
    if company.last_activity_at:
        meta.append("Last activity: {}".format(company.last_activity_at.strftime("%Y-%m-%d")))
    sections.append("## Company profile\n" + "\n".join("- " + m for m in meta))

    # Knowledge entries
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.company_id == company_id)
        .filter(KnowledgeEntry.entry_type != "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(40)
        .all()
    )
    if entries:
        lines = []
        for e in entries:
            prefix = "[OUTDATED] " if _is_expired(e.expires_at, now) else ""
            lines.append("- {}{}: {} ({})".format(prefix, e.entry_type, e.content, e.created_at.strftime("%Y-%m-%d")))
        sections.append("## Knowledge entries\n" + "\n".join(lines))

    # Open requisitions via customer sites
    site_ids = [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company_id).all()]
    if site_ids:
        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.customer_site_id.in_(site_ids),
                Requisition.status.in_(
                    [
                        RequisitionStatus.OPEN,
                        RequisitionStatus.RFQS_SENT,
                        RequisitionStatus.QUOTED,
                    ]
                ),
            )
            .order_by(Requisition.created_at.desc())
            .limit(20)
            .all()
        )
        if reqs:
            lines = []
            for r in reqs:
                lines.append(
                    "- Req #{} '{}' status={} ({})".format(
                        r.id,
                        r.name,
                        r.status,
                        r.created_at.strftime("%Y-%m-%d"),
                    )
                )
            sections.append("## Open requisitions\n" + "\n".join(lines))

    if not sections:
        return ""
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Entity-scoped insight generators
# ---------------------------------------------------------------------------


async def generate_vendor_insights(
    db: Session, vendor_card_id: int, *, interactive: bool = False
) -> list[KnowledgeEntry]:
    """Generate AI insights for a vendor using the context engine.

    ``interactive=True`` (the HTMX refresh endpoint) tightens the Claude call
    budget — see ``_regenerate_insights``. Defaults to False (background job).
    """
    context = build_vendor_context(db, vendor_card_id=vendor_card_id)
    return await _regenerate_insights(
        db,
        context=context,
        delete_filters=(
            KnowledgeEntry.vendor_card_id == vendor_card_id,
            KnowledgeEntry.entry_type == "ai_insight",
        ),
        prompt=f"Analyze this knowledge base for this vendor and generate insights:\n\n{context}",
        system=VENDOR_INSIGHT_PROMPT,
        entry_kwargs={"vendor_card_id": vendor_card_id},
        no_context_log=f"No context for vendor {vendor_card_id} — skipping insight generation",
        unavailable_log="Claude not configured — skipping vendor insight generation",
        failed_log="Claude AI failed for vendor insight generation: {}",
        no_results_log=f"AI insight generation returned no results for vendor {vendor_card_id}",
        generated_log="Generated {} insights for vendor {}",
        generated_args=(vendor_card_id,),
        interactive=interactive,
    )


async def generate_pipeline_insights(db: Session, *, interactive: bool = False) -> list[KnowledgeEntry]:
    """Generate AI insights for the overall pipeline health.

    ``interactive=True`` (the HTMX refresh endpoint) tightens the Claude call
    budget — see ``_regenerate_insights``. Defaults to False (background job).
    """
    context = build_pipeline_context(db)
    return await _regenerate_insights(
        db,
        context=context,
        # Pipeline insights are stored under the sentinel mpn='__pipeline__'.
        delete_filters=(
            KnowledgeEntry.mpn == "__pipeline__",
            KnowledgeEntry.entry_type == "ai_insight",
        ),
        prompt=f"Analyze this pipeline summary and generate insights:\n\n{context}",
        system=PIPELINE_INSIGHT_PROMPT,
        entry_kwargs={"mpn": "__pipeline__"},
        no_context_log="No context for pipeline — skipping insight generation",
        unavailable_log="Claude not configured — skipping pipeline insight generation",
        failed_log="Claude AI failed for pipeline insight generation: {}",
        no_results_log="AI insight generation returned no results for pipeline",
        generated_log="Generated {} pipeline insights",
        interactive=interactive,
    )


async def generate_company_insights(db: Session, company_id: int, *, interactive: bool = False) -> list[KnowledgeEntry]:
    """Generate AI insights for a company using the context engine.

    ``interactive=True`` (the HTMX refresh endpoint) tightens the Claude call
    budget — see ``_regenerate_insights``. Defaults to False (background job).
    """
    context = build_company_context(db, company_id=company_id)
    return await _regenerate_insights(
        db,
        context=context,
        delete_filters=(
            KnowledgeEntry.company_id == company_id,
            KnowledgeEntry.entry_type == "ai_insight",
        ),
        prompt=f"Analyze this knowledge base for this company and generate insights:\n\n{context}",
        system=COMPANY_INSIGHT_PROMPT,
        entry_kwargs={"company_id": company_id},
        no_context_log=f"No context for company {company_id} — skipping insight generation",
        unavailable_log="Claude not configured — skipping company insight generation",
        failed_log="Claude AI failed for company insight generation: {}",
        no_results_log=f"AI insight generation returned no results for company {company_id}",
        generated_log="Generated {} insights for company {}",
        generated_args=(company_id,),
        interactive=interactive,
    )


# ---------------------------------------------------------------------------
# Entity-scoped cached insight getters
# ---------------------------------------------------------------------------


def get_cached_vendor_insights(db: Session, vendor_card_id: int) -> list[KnowledgeEntry]:
    """Return pre-computed AI insights for a vendor."""
    return (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.vendor_card_id == vendor_card_id,
            KnowledgeEntry.entry_type == "ai_insight",
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )


def get_cached_pipeline_insights(db: Session) -> list[KnowledgeEntry]:
    """Return pre-computed AI insights for the pipeline."""
    return (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.mpn == "__pipeline__",
            KnowledgeEntry.entry_type == "ai_insight",
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )


def get_cached_company_insights(db: Session, company_id: int) -> list[KnowledgeEntry]:
    """Return pre-computed AI insights for a company."""
    return (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.company_id == company_id,
            KnowledgeEntry.entry_type == "ai_insight",
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )
