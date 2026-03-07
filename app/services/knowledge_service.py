"""Knowledge Ledger service — CRUD, Q&A, auto-capture, AI context engine.

Central service for the knowledge base. Handles entry creation, Q&A
threading, notification triggers, auto-capture from quotes/offers,
and AI insight generation.

Called by: routers/knowledge.py, jobs/knowledge_jobs.py
Depends on: models/knowledge.py, utils/claude_client.py, services/notification_service.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.models.knowledge import KnowledgeEntry
from app.services.notification_service import create_notification

# Expiry defaults (days)
EXPIRY_PRICE_FACT = 90
EXPIRY_LEAD_TIME_FACT = 180
EXPIRY_AI_INSIGHT = 30


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
    db.commit()
    db.refresh(entry)
    logger.info("Knowledge entry created: id={} type={} source={}", entry.id, entry_type, source)
    return entry


def get_entries(
    db: Session,
    *,
    requisition_id: int | None = None,
    company_id: int | None = None,
    vendor_card_id: int | None = None,
    mpn: str | None = None,
    entry_type: str | None = None,
    include_expired: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[KnowledgeEntry]:
    """Query knowledge entries with flexible filters."""
    q = db.query(KnowledgeEntry)
    if requisition_id is not None:
        q = q.filter(KnowledgeEntry.requisition_id == requisition_id)
    if company_id is not None:
        q = q.filter(KnowledgeEntry.company_id == company_id)
    if vendor_card_id is not None:
        q = q.filter(KnowledgeEntry.vendor_card_id == vendor_card_id)
    if mpn is not None:
        q = q.filter(KnowledgeEntry.mpn == mpn)
    if entry_type is not None:
        q = q.filter(KnowledgeEntry.entry_type == entry_type)
    if not include_expired:
        now = datetime.now(timezone.utc)
        q = q.filter(or_(KnowledgeEntry.expires_at.is_(None), KnowledgeEntry.expires_at > now))
    # Exclude answers from top-level listing (they appear nested under questions)
    q = q.filter(KnowledgeEntry.parent_id.is_(None))
    q = q.options(joinedload(KnowledgeEntry.answers), joinedload(KnowledgeEntry.creator))
    q = q.order_by(KnowledgeEntry.created_at.desc())
    return q.offset(offset).limit(limit).all()


def get_entry(db: Session, entry_id: int) -> KnowledgeEntry | None:
    """Get a single entry with answers loaded."""
    return (
        db.query(KnowledgeEntry)
        .options(joinedload(KnowledgeEntry.answers), joinedload(KnowledgeEntry.creator))
        .filter(KnowledgeEntry.id == entry_id)
        .first()
    )


def update_entry(db: Session, entry_id: int, user_id: int, **kwargs) -> KnowledgeEntry | None:
    """Update an entry. Only the creator can update."""
    entry = db.get(KnowledgeEntry, entry_id)
    if not entry:
        return None
    for key, value in kwargs.items():
        if value is not None and hasattr(entry, key):
            setattr(entry, key, value)
    db.commit()
    db.refresh(entry)
    return entry


def delete_entry(db: Session, entry_id: int, user_id: int) -> bool:
    """Delete an entry. Returns True if deleted."""
    entry = db.get(KnowledgeEntry, entry_id)
    if not entry:
        return False
    db.delete(entry)
    db.commit()
    logger.info("Knowledge entry deleted: id={} by user={}", entry_id, user_id)
    return True


def post_question(
    db: Session,
    *,
    user_id: int,
    content: str,
    assigned_to_ids: list[int],
    mpn: str | None = None,
    vendor_card_id: int | None = None,
    company_id: int | None = None,
    requisition_id: int | None = None,
    requirement_id: int | None = None,
) -> KnowledgeEntry:
    """Post a Q&A question and notify assigned buyers."""
    # Check daily question cap
    from app.services.teams_qa_service import check_question_quota

    quota = check_question_quota(db, user_id)
    if not quota["allowed"]:
        raise ValueError("Daily question limit reached ({}/{})".format(quota["used"], quota["limit"]))

    entry = create_entry(
        db,
        user_id=user_id,
        entry_type="question",
        content=content,
        source="manual",
        assigned_to_ids=assigned_to_ids,
        mpn=mpn,
        vendor_card_id=vendor_card_id,
        company_id=company_id,
        requisition_id=requisition_id,
        requirement_id=requirement_id,
    )
    # Notify each assigned buyer
    for buyer_id in assigned_to_ids:
        try:
            create_notification(
                db=db,
                user_id=buyer_id,
                event_type="knowledge_question",
                title="New question on Req #{}".format(requisition_id) if requisition_id else "New question",
                body=content[:200],
            )
        except Exception as e:
            logger.warning("Failed to notify buyer {}: {}", buyer_id, e)
    return entry


def post_answer(
    db: Session,
    *,
    user_id: int,
    question_id: int,
    content: str,
    answered_via: str = "web",
) -> KnowledgeEntry | None:
    """Answer a question. Marks question resolved and notifies asker."""
    question = db.get(KnowledgeEntry, question_id)
    if not question or question.entry_type != "question":
        return None

    answer = create_entry(
        db,
        user_id=user_id,
        entry_type="answer",
        content=content,
        source="manual",
        parent_id=question_id,
        mpn=question.mpn,
        vendor_card_id=question.vendor_card_id,
        company_id=question.company_id,
        requisition_id=question.requisition_id,
        requirement_id=question.requirement_id,
    )

    # Track answer source
    answer.answered_via = answered_via

    # Mark question as resolved
    question.is_resolved = True
    db.commit()

    # Notify the original asker
    if question.created_by:
        try:
            create_notification(
                db=db,
                user_id=question.created_by,
                event_type="knowledge_answer",
                title="Your question was answered on Req #{}".format(question.requisition_id) if question.requisition_id else "Your question was answered",
                body=content[:200],
            )
        except Exception as e:
            logger.warning("Failed to notify asker {}: {}", question.created_by, e)

    return answer


# ---------------------------------------------------------------------------
# Auto-capture: extract facts from quotes, offers, and RFQ responses
# ---------------------------------------------------------------------------


def capture_quote_fact(db: Session, *, quote, user_id: int) -> KnowledgeEntry | None:
    """Auto-capture price facts when a quote is created.

    Called from: app/routers/crm/quotes.py after quote creation.
    """
    try:
        line_items = quote.line_items or []
        if not line_items:
            return None

        facts = []
        for item in line_items:
            mpn = item.get("mpn") or item.get("part_number", "")
            price = item.get("unit_sell") or item.get("sell_price")
            qty = item.get("qty") or item.get("quantity")
            vendor = item.get("vendor_name", "")
            if mpn and price:
                facts.append("{}: ${:.2f}".format(mpn, float(price)) + (" x{}".format(qty) if qty else "") + (" from {}".format(vendor) if vendor else ""))

        if not facts:
            return None

        content = "Quote #{} — {}".format(quote.quote_number, "; ".join(facts))
        return create_entry(
            db,
            user_id=user_id,
            entry_type="fact",
            content=content,
            source="system",
            confidence=1.0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=EXPIRY_PRICE_FACT),
            requisition_id=quote.requisition_id,
        )
    except Exception as e:
        logger.warning("Failed to capture quote fact: {}", e)
        return None


def capture_offer_fact(db: Session, *, offer, user_id: int | None = None) -> KnowledgeEntry | None:
    """Auto-capture facts when an offer is created (manual or parsed).

    Called from: app/routers/crm/offers.py, app/email_service.py
    """
    try:
        mpn = getattr(offer, "mpn", None) or ""
        price = getattr(offer, "unit_price", None)
        qty = getattr(offer, "quantity", None)
        vendor_name = getattr(offer, "vendor_name", None) or ""
        lead_time = getattr(offer, "lead_time", None)

        content_parts = []
        if mpn:
            content_parts.append("MPN: {}".format(mpn))
        if price:
            content_parts.append("${:.2f}".format(float(price)))
        if qty:
            content_parts.append("qty {}".format(qty))
        if vendor_name:
            content_parts.append("from {}".format(vendor_name))
        if lead_time:
            content_parts.append("lead time: {}".format(lead_time))

        if not content_parts:
            return None

        content = "Offer — " + ", ".join(content_parts)
        return create_entry(
            db,
            user_id=user_id or 0,
            entry_type="fact",
            content=content,
            source="system",
            confidence=1.0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=EXPIRY_PRICE_FACT),
            mpn=mpn or None,
            vendor_card_id=getattr(offer, "vendor_card_id", None),
            requisition_id=getattr(offer, "requisition_id", None),
        )
    except Exception as e:
        logger.warning("Failed to capture offer fact: {}", e)
        return None


def capture_rfq_response_fact(
    db: Session, *, parsed: dict, vendor_name: str, requisition_id: int | None = None
) -> list[KnowledgeEntry]:
    """Auto-capture facts from a parsed RFQ vendor response.

    Called from: app/services/response_parser.py or app/email_service.py
    """
    entries = []
    try:
        parts = parsed.get("parts", [])
        for part in parts:
            mpn = part.get("mpn", "")
            status = part.get("status", "")
            price = part.get("unit_price")
            qty = part.get("qty_available")
            lead = part.get("lead_time_weeks") or part.get("lead_time")

            content_parts = ["Vendor response from {}: {}".format(vendor_name, mpn)]
            if status:
                content_parts.append("status={}".format(status))
            if price:
                content_parts.append("${}".format(price))
            if qty:
                content_parts.append("qty {} available".format(qty))
            if lead:
                content_parts.append("lead time {}".format(lead))

            content = ", ".join(content_parts)

            # Price facts expire in 90 days, lead time facts in 180
            expiry_days = EXPIRY_PRICE_FACT if price else EXPIRY_LEAD_TIME_FACT
            entry = create_entry(
                db,
                user_id=0,  # system
                entry_type="fact",
                content=content,
                source="email_parsed",
                confidence=parsed.get("confidence", 0.8),
                expires_at=datetime.now(timezone.utc) + timedelta(days=expiry_days),
                mpn=mpn or None,
                requisition_id=requisition_id,
            )
            entries.append(entry)
    except Exception as e:
        logger.warning("Failed to capture RFQ response facts: {}", e)
    return entries


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

    now = datetime.now(timezone.utc)
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
            prefix = "[OUTDATED] " if e.expires_at and e.expires_at < now else ""
            lines.append("- {}{}: {} (source: {}, {})".format(prefix, e.entry_type, e.content, e.source, e.created_at.strftime('%Y-%m-%d')))
        sections.append("## Direct knowledge for this requisition\n" + "\n".join(lines))

    # 2. MPN knowledge from other reqs
    mpns = [r.mpn for r in db.query(Requirement.mpn).filter(Requirement.requisition_id == requisition_id).all() if r.mpn]
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
                prefix = "[OUTDATED] " if e.expires_at and e.expires_at < now else ""
                lines.append("- {}{}: {} (req #{}, {})".format(prefix, e.mpn, e.content, e.requisition_id, e.created_at.strftime('%Y-%m-%d')))
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
                prefix = "[OUTDATED] " if e.expires_at and e.expires_at < now else ""
                lines.append("- {}Vendor #{}: {} ({})".format(prefix, e.vendor_card_id, e.content, e.created_at.strftime('%Y-%m-%d')))
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
                prefix = "[OUTDATED] " if e.expires_at and e.expires_at < now else ""
                lines.append("- {}{} ({})".format(prefix, e.content, e.created_at.strftime('%Y-%m-%d')))
            sections.append("## Customer intelligence\n" + "\n".join(lines))

    if not sections:
        return ""

    return "\n\n".join(sections)


async def generate_insights(db: Session, requisition_id: int) -> list[KnowledgeEntry]:
    """Generate AI insights for a requisition using the context engine."""
    from app.utils.claude_client import claude_structured

    context = build_context(db, requisition_id=requisition_id)
    if not context:
        logger.debug("No context for req {} — skipping insight generation", requisition_id)
        return []

    # Delete old AI insights for this req
    old_insights = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.requisition_id == requisition_id,
            KnowledgeEntry.entry_type == "ai_insight",
        )
        .all()
    )
    for old in old_insights:
        db.delete(old)
    db.flush()

    result = await claude_structured(
        prompt="Analyze this knowledge base and generate insights:\n\n{}".format(context),
        schema=INSIGHT_SCHEMA,
        system=INSIGHT_SYSTEM_PROMPT,
        model_tier="smart",
        max_tokens=2048,
        thinking_budget=5000,
    )

    if not result or "insights" not in result:
        logger.warning("AI insight generation returned no results for req {}", requisition_id)
        return []

    entries = []
    now = datetime.now(timezone.utc)
    for insight in result["insights"][:5]:  # Cap at 5
        entry = create_entry(
            db,
            user_id=0,  # system
            entry_type="ai_insight",
            content=insight["content"],
            source="ai_generated",
            confidence=insight.get("confidence", 0.8),
            expires_at=now + timedelta(days=EXPIRY_AI_INSIGHT),
            requisition_id=requisition_id,
        )
        entries.append(entry)

    logger.info("Generated {} insights for req {}", len(entries), requisition_id)
    return entries


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
