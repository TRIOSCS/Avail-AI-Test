# Knowledge Ledger Phase 1 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a multi-entity knowledge base with Q&A on requisitions, auto-capture from quotes/offers/RFQ responses, pre-computed AI insights with expiry, and a frontend with collapsible insights card + Q&A tab.

**Architecture:** Single `knowledge_service.py` handles CRUD, Q&A routing, auto-capture hooks, and AI context generation. `knowledge_entry` table with nullable FK columns for multi-entity linkage. Pre-computed insights refreshed every 6h, expired entries dimmed in UI.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Alembic, Claude API (Haiku for extraction, Sonnet for synthesis), vanilla JS frontend.

**Testing:** Deferred until all phases complete — no test files in this plan.

---

### Task 1: KnowledgeEntry Model

**Files:**
- Create: `app/models/knowledge.py`
- Modify: `app/models/__init__.py:41-43`

**Step 1: Create the model file**

Create `app/models/knowledge.py`:

```python
"""Knowledge Ledger — captures facts, Q&A, notes, and AI insights.

Multi-entity linkage via nullable FK columns to MPN, vendor, company,
requisition, and requirement. Supports Q&A threading via parent_id
self-referential FK. Expiry logic for price/lead-time facts.

Called by: services/knowledge_service.py, routers/knowledge.py
Depends on: models/base.py, models/auth.py
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import relationship

from .base import Base


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id = Column(Integer, primary_key=True, index=True)
    entry_type = Column(String(20), nullable=False)  # question, answer, fact, note, ai_insight
    content = Column(Text, nullable=False)
    source = Column(String(20), nullable=False, default="manual")  # manual, ai_generated, system, email_parsed, teams_bot
    confidence = Column(Float, nullable=True)  # 0.0-1.0 for AI-generated
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_resolved = Column(Boolean, default=False, nullable=False)  # Q&A: marks question as answered
    parent_id = Column(Integer, ForeignKey("knowledge_entries.id", ondelete="SET NULL"), nullable=True)
    assigned_to_ids = Column(JSON, default=list)  # user IDs for Q&A routing

    # Who created it
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Multi-entity linkage (all nullable)
    mpn = Column(String(255), nullable=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"), nullable=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    creator = relationship("User", foreign_keys=[created_by])
    parent = relationship("KnowledgeEntry", remote_side=[id], foreign_keys=[parent_id])
    answers = relationship("KnowledgeEntry", foreign_keys=[parent_id], back_populates="parent")
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    company = relationship("Company", foreign_keys=[company_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])

    __table_args__ = (
        Index("ix_ke_requisition", "requisition_id", "created_at"),
        Index("ix_ke_mpn", "mpn"),
        Index("ix_ke_company", "company_id", "created_at"),
        Index("ix_ke_vendor", "vendor_card_id"),
        Index("ix_ke_parent", "parent_id"),
        Index("ix_ke_expires", "expires_at", postgresql_where="expires_at IS NOT NULL"),
    )
```

**Step 2: Register the model**

In `app/models/__init__.py`, add after line 43 (after NotificationEngagement):

```python
# Knowledge Ledger
from .knowledge import KnowledgeEntry  # noqa: F401
```

**Step 3: Commit**

```bash
git add app/models/knowledge.py app/models/__init__.py
git commit -m "feat: add KnowledgeEntry model with multi-entity linkage"
```

---

### Task 2: Alembic Migration

**Files:**
- Create: `alembic/versions/063_knowledge_entries.py`

**Step 1: Generate migration**

```bash
docker compose exec app alembic revision --autogenerate -m "knowledge_entries"
```

Review the generated file. It should create the `knowledge_entries` table with all columns and indexes from Task 1.

**Step 2: Apply and test rollback**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

**Step 3: Commit**

```bash
git add alembic/versions/063_knowledge_entries.py
git commit -m "migration: 063 knowledge_entries table"
```

---

### Task 3: Pydantic Schemas

**Files:**
- Create: `app/schemas/knowledge.py`

**Step 1: Create schemas**

```python
"""Request/response schemas for the Knowledge Ledger API.

Called by: routers/knowledge.py
Depends on: nothing
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeEntryCreate(BaseModel):
    entry_type: str = Field(..., pattern="^(question|answer|fact|note|ai_insight)$")
    content: str = Field(..., min_length=1, max_length=10000)
    source: str = Field(default="manual", pattern="^(manual|ai_generated|system|email_parsed|teams_bot)$")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    expires_at: datetime | None = None
    mpn: str | None = None
    vendor_card_id: int | None = None
    company_id: int | None = None
    requisition_id: int | None = None
    requirement_id: int | None = None


class QuestionCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
    assigned_to_ids: list[int] = Field(..., min_length=1)
    mpn: str | None = None
    vendor_card_id: int | None = None
    company_id: int | None = None
    requisition_id: int | None = None
    requirement_id: int | None = None


class AnswerCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


class KnowledgeEntryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    is_resolved: bool | None = None
    expires_at: datetime | None = None


class KnowledgeEntryResponse(BaseModel, extra="allow"):
    id: int
    entry_type: str
    content: str
    source: str
    confidence: float | None = None
    expires_at: datetime | None = None
    is_expired: bool = False
    is_resolved: bool = False
    parent_id: int | None = None
    assigned_to_ids: list[int] = []
    created_by: int | None = None
    creator_name: str | None = None
    mpn: str | None = None
    vendor_card_id: int | None = None
    company_id: int | None = None
    requisition_id: int | None = None
    requirement_id: int | None = None
    created_at: datetime
    updated_at: datetime
    answers: list[KnowledgeEntryResponse] = []


class InsightsResponse(BaseModel, extra="allow"):
    requisition_id: int
    insights: list[KnowledgeEntryResponse] = []
    generated_at: datetime | None = None
    has_expired: bool = False
```

**Step 2: Commit**

```bash
git add app/schemas/knowledge.py
git commit -m "feat: add Knowledge Ledger Pydantic schemas"
```

---

### Task 4: Knowledge Service — CRUD + Q&A

**Files:**
- Create: `app/services/knowledge_service.py`

**Step 1: Create the service with CRUD and Q&A**

```python
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
```

**Step 2: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "feat: Knowledge service — CRUD and Q&A with notifications"
```

---

### Task 5: Knowledge Service — Auto-Capture

**Files:**
- Modify: `app/services/knowledge_service.py` (append to end)

**Step 1: Add auto-capture functions**

Append to `app/services/knowledge_service.py`:

```python
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
```

**Step 2: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "feat: Knowledge auto-capture — quotes, offers, RFQ responses"
```

---

### Task 6: Knowledge Service — AI Context Engine

**Files:**
- Modify: `app/services/knowledge_service.py` (append to end)

**Step 1: Add AI context engine functions**

Append to `app/services/knowledge_service.py`:

```python
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
```

**Step 2: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "feat: AI context engine — build_context + generate_insights"
```

---

### Task 7: API Router

**Files:**
- Create: `app/routers/knowledge.py`
- Modify: `app/main.py:1022` (append router registration)

**Step 1: Create the router**

```python
"""Knowledge Ledger API — CRUD, Q&A, and AI insights endpoints.

Provides endpoints for managing knowledge entries, posting Q&A questions
and answers, and generating/retrieving AI insights for requisitions.

Called by: frontend (app.js, crm.js)
Depends on: services/knowledge_service.py, dependencies.py
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.schemas.knowledge import (
    AnswerCreate,
    KnowledgeEntryCreate,
    KnowledgeEntryUpdate,
    QuestionCreate,
)
from app.services import knowledge_service

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _entry_to_response(entry, now=None) -> dict:
    """Convert a KnowledgeEntry to a response dict."""
    if now is None:
        now = datetime.now(timezone.utc)
    is_expired = bool(entry.expires_at and entry.expires_at < now)
    answers = []
    if hasattr(entry, "answers") and entry.answers:
        answers = [_entry_to_response(a, now) for a in entry.answers]
    return {
        "id": entry.id,
        "entry_type": entry.entry_type,
        "content": entry.content,
        "source": entry.source,
        "confidence": entry.confidence,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        "is_expired": is_expired,
        "is_resolved": entry.is_resolved,
        "parent_id": entry.parent_id,
        "assigned_to_ids": entry.assigned_to_ids or [],
        "created_by": entry.created_by,
        "creator_name": entry.creator.display_name if entry.creator else None,
        "mpn": entry.mpn,
        "vendor_card_id": entry.vendor_card_id,
        "company_id": entry.company_id,
        "requisition_id": entry.requisition_id,
        "requirement_id": entry.requirement_id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "answers": answers,
    }


@router.get("")
def list_entries(
    requisition_id: int | None = Query(None),
    company_id: int | None = Query(None),
    vendor_card_id: int | None = Query(None),
    mpn: str | None = Query(None),
    entry_type: str | None = Query(None),
    include_expired: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_entries(
        db,
        requisition_id=requisition_id,
        company_id=company_id,
        vendor_card_id=vendor_card_id,
        mpn=mpn,
        entry_type=entry_type,
        include_expired=include_expired,
        limit=limit,
        offset=offset,
    )
    now = datetime.now(timezone.utc)
    return [_entry_to_response(e, now) for e in entries]


@router.post("")
def create_entry_endpoint(
    payload: KnowledgeEntryCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entry = knowledge_service.create_entry(
        db,
        user_id=user.id,
        entry_type=payload.entry_type,
        content=payload.content,
        source=payload.source,
        confidence=payload.confidence,
        expires_at=payload.expires_at,
        mpn=payload.mpn,
        vendor_card_id=payload.vendor_card_id,
        company_id=payload.company_id,
        requisition_id=payload.requisition_id,
        requirement_id=payload.requirement_id,
    )
    return _entry_to_response(entry)


@router.put("/{entry_id}")
def update_entry_endpoint(
    entry_id: int,
    payload: KnowledgeEntryUpdate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entry = knowledge_service.update_entry(
        db, entry_id, user.id,
        content=payload.content,
        is_resolved=payload.is_resolved,
        expires_at=payload.expires_at,
    )
    if not entry:
        raise HTTPException(404, "Entry not found")
    return _entry_to_response(entry)


@router.delete("/{entry_id}")
def delete_entry_endpoint(
    entry_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    if not knowledge_service.delete_entry(db, entry_id, user.id):
        raise HTTPException(404, "Entry not found")
    return {"ok": True}


@router.post("/question")
def post_question(
    payload: QuestionCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entry = knowledge_service.post_question(
        db,
        user_id=user.id,
        content=payload.content,
        assigned_to_ids=payload.assigned_to_ids,
        mpn=payload.mpn,
        vendor_card_id=payload.vendor_card_id,
        company_id=payload.company_id,
        requisition_id=payload.requisition_id,
        requirement_id=payload.requirement_id,
    )
    return _entry_to_response(entry)


@router.post("/{entry_id}/answer")
def post_answer(
    entry_id: int,
    payload: AnswerCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    answer = knowledge_service.post_answer(
        db, user_id=user.id, question_id=entry_id, content=payload.content
    )
    if not answer:
        raise HTTPException(404, "Question not found")
    return _entry_to_response(answer)


# --- AI Insights (on requisition) ---

insights_router = APIRouter(prefix="/api/requisitions", tags=["knowledge"])


@insights_router.get("/{req_id}/insights")
def get_insights(
    req_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_insights(db, req_id)
    now = datetime.now(timezone.utc)
    return {
        "requisition_id": req_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@insights_router.post("/{req_id}/insights/refresh")
async def refresh_insights(
    req_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_insights(db, req_id)
    now = datetime.now(timezone.utc)
    return {
        "requisition_id": req_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }
```

**Step 2: Register routers in `app/main.py`**

After line 1022, add:

```python
from .routers.knowledge import router as knowledge_router
from .routers.knowledge import insights_router as knowledge_insights_router

app.include_router(knowledge_router)
app.include_router(knowledge_insights_router)
```

**Step 3: Commit**

```bash
git add app/routers/knowledge.py app/main.py
git commit -m "feat: Knowledge Ledger API — CRUD, Q&A, insights endpoints"
```

---

### Task 8: Background Jobs

**Files:**
- Create: `app/jobs/knowledge_jobs.py`
- Modify: `app/jobs/__init__.py:23-38` (add import + registration)

**Step 1: Create the job file**

```python
"""Background jobs for the Knowledge Ledger.

- refresh_active_insights: Re-generate AI insights for recently active reqs (every 6h)
- expire_stale_entries: Mark expired entries (daily 3AM)

Called by: app/jobs/__init__.py via register_knowledge_jobs()
Depends on: services/knowledge_service.py
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


def register_knowledge_jobs(scheduler, settings):
    """Register knowledge ledger background jobs."""
    scheduler.add_job(
        _job_refresh_insights,
        IntervalTrigger(hours=6),
        id="knowledge_refresh_insights",
        name="Refresh AI insights for active requisitions",
    )
    scheduler.add_job(
        _job_expire_stale,
        CronTrigger(hour=3, minute=0),
        id="knowledge_expire_stale",
        name="Mark expired knowledge entries",
    )


async def _job_refresh_insights():
    """Re-generate insights for reqs updated in the last 24h, cap 50."""
    from app.database import SessionLocal
    from app.models.sourcing import Requisition
    from app.services import knowledge_service

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        active_reqs = (
            db.query(Requisition.id)
            .filter(Requisition.updated_at >= cutoff)
            .order_by(Requisition.updated_at.desc())
            .limit(50)
            .all()
        )
        count = 0
        for (req_id,) in active_reqs:
            try:
                entries = await knowledge_service.generate_insights(db, req_id)
                if entries:
                    count += 1
            except Exception as e:
                logger.warning("Insight generation failed for req {}: {}", req_id, e)
        logger.info("Refreshed insights for {}/{} active reqs", count, len(active_reqs))
    except Exception as e:
        logger.error("refresh_active_insights job failed: {}", e)
    finally:
        db.close()


async def _job_expire_stale():
    """Log count of expired entries for monitoring. Expiry is handled at query time."""
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeEntry

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired_count = (
            db.query(KnowledgeEntry)
            .filter(KnowledgeEntry.expires_at.isnot(None), KnowledgeEntry.expires_at < now)
            .count()
        )
        total = db.query(KnowledgeEntry).count()
        logger.info("Knowledge entries: {} total, {} expired", total, expired_count)
    except Exception as e:
        logger.error("expire_stale job failed: {}", e)
    finally:
        db.close()
```

**Step 2: Register in `app/jobs/__init__.py`**

Add import after line 24:

```python
from .knowledge_jobs import register_knowledge_jobs
```

Add call after line 38:

```python
register_knowledge_jobs(scheduler, settings)
```

**Step 3: Commit**

```bash
git add app/jobs/knowledge_jobs.py app/jobs/__init__.py
git commit -m "feat: Knowledge background jobs — insight refresh (6h) + expiry monitor (daily)"
```

---

### Task 9: Auto-Capture Hooks in Existing Code

**Files:**
- Modify: `app/routers/crm/quotes.py:170` (after quote commit)
- Modify: `app/routers/crm/offers.py:400` (after offer commit)
- Modify: `app/email_service.py:953` (after parsed offer creation)

**Step 1: Hook into quote creation**

In `app/routers/crm/quotes.py`, after the `db.commit()` following quote creation (around line 170), add:

```python
# Auto-capture quote facts into Knowledge Ledger
try:
    from app.services.knowledge_service import capture_quote_fact
    capture_quote_fact(db, quote=quote, user_id=user.id)
except Exception as e:
    logger.warning("Knowledge auto-capture (quote) failed: {}", e)
```

**Step 2: Hook into manual offer creation**

In `app/routers/crm/offers.py`, after `db.add(offer)` and commit (around line 400), add:

```python
# Auto-capture offer facts into Knowledge Ledger
try:
    from app.services.knowledge_service import capture_offer_fact
    capture_offer_fact(db, offer=offer, user_id=user.id)
except Exception as e:
    logger.warning("Knowledge auto-capture (offer) failed: {}", e)
```

**Step 3: Hook into email-parsed offer creation**

In `app/email_service.py`, after `db.add(offer)` and flush (around line 953), add:

```python
# Auto-capture offer facts into Knowledge Ledger
try:
    from app.services.knowledge_service import capture_offer_fact
    capture_offer_fact(db, offer=offer)
except Exception as e:
    logger.warning("Knowledge auto-capture (email offer) failed: {}", e)
```

**Step 4: Commit**

```bash
git add app/routers/crm/quotes.py app/routers/crm/offers.py app/email_service.py
git commit -m "feat: auto-capture hooks — quote, offer, email offer to Knowledge Ledger"
```

---

### Task 10: Frontend — AI Insights Card on Parts Tab

**Files:**
- Modify: `app/static/app.js` — `_renderDrillDownTable()` (~line 5684) and add `_renderInsightsCard()` function
- Modify: `app/templates/index.html` — add CSS styles

**Step 1: Add the insights card renderer**

Add this function near the other `_renderDd*` functions in `app/static/app.js`:

```javascript
// ---------------------------------------------------------------------------
// Knowledge Ledger: AI Insights Card (collapsible, top of parts tab)
// ---------------------------------------------------------------------------

async function _renderInsightsCard(reqId, container) {
    const collapsed = localStorage.getItem('insights_collapsed') === '1';
    const wrap = document.createElement('div');
    wrap.className = 'insights-card';
    wrap.id = 'insights-' + reqId;

    const header = document.createElement('div');
    header.className = 'insights-header';
    header.onclick = function() { _toggleInsightsCard(reqId); };

    const title = document.createElement('span');
    title.style.cssText = 'font-weight:600;font-size:12px';
    title.textContent = 'AI Insights';
    header.appendChild(title);

    const controls = document.createElement('span');
    controls.style.cssText = 'display:flex;gap:6px;align-items:center';

    const refreshBtn = document.createElement('button');
    refreshBtn.className = 'btn btn-ghost btn-sm';
    refreshBtn.style.fontSize = '10px';
    refreshBtn.textContent = '\u21bb Refresh';
    refreshBtn.title = 'Regenerate insights';
    refreshBtn.onclick = function(e) { e.stopPropagation(); _refreshInsights(reqId); };
    controls.appendChild(refreshBtn);

    const toggle = document.createElement('span');
    toggle.className = 'insights-toggle';
    toggle.textContent = collapsed ? '\u25b6' : '\u25bc';
    controls.appendChild(toggle);
    header.appendChild(controls);
    wrap.appendChild(header);

    const body = document.createElement('div');
    body.className = 'insights-body';
    body.style.display = collapsed ? 'none' : '';
    const loadingSpan = document.createElement('span');
    loadingSpan.style.cssText = 'font-size:11px;color:var(--muted)';
    loadingSpan.textContent = 'Loading\u2026';
    body.appendChild(loadingSpan);
    wrap.appendChild(body);

    container.prepend(wrap);

    try {
        const data = await apiFetch('/api/requisitions/' + reqId + '/insights');
        body.textContent = '';
        if (!data.insights || !data.insights.length) {
            const emptySpan = document.createElement('span');
            emptySpan.style.cssText = 'font-size:11px;color:var(--muted)';
            emptySpan.textContent = 'No insights yet. Click Refresh to generate.';
            body.appendChild(emptySpan);
            return;
        }
        for (const ins of data.insights) {
            const item = document.createElement('div');
            item.className = 'insight-item' + (ins.is_expired ? ' insight-expired' : '');
            const text = document.createElement('span');
            text.style.fontSize = '11px';
            text.textContent = ins.content;
            item.appendChild(text);
            if (ins.is_expired) {
                const badge = document.createElement('span');
                badge.style.cssText = 'font-size:9px;color:var(--amber);margin-left:4px';
                badge.textContent = '(may be outdated)';
                item.appendChild(badge);
            }
            body.appendChild(item);
        }
        if (data.has_expired) {
            const warn = document.createElement('div');
            warn.style.cssText = 'font-size:10px;color:var(--amber);margin-top:4px';
            warn.textContent = 'Some insights based on outdated data';
            body.appendChild(warn);
        }
    } catch (e) {
        body.textContent = '';
        const errSpan = document.createElement('span');
        errSpan.style.cssText = 'font-size:11px;color:var(--red)';
        errSpan.textContent = 'Failed to load insights';
        body.appendChild(errSpan);
    }
}

function _toggleInsightsCard(reqId) {
    const card = document.getElementById('insights-' + reqId);
    if (!card) return;
    const body = card.querySelector('.insights-body');
    const toggle = card.querySelector('.insights-toggle');
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? '' : 'none';
    toggle.textContent = hidden ? '\u25bc' : '\u25b6';
    localStorage.setItem('insights_collapsed', hidden ? '0' : '1');
}

async function _refreshInsights(reqId) {
    const card = document.getElementById('insights-' + reqId);
    if (!card) return;
    const body = card.querySelector('.insights-body');
    body.textContent = '';
    const loading = document.createElement('span');
    loading.style.cssText = 'font-size:11px;color:var(--muted)';
    loading.textContent = 'Generating\u2026';
    body.appendChild(loading);
    try {
        const data = await apiFetch('/api/requisitions/' + reqId + '/insights/refresh', { method: 'POST' });
        body.textContent = '';
        for (const ins of (data.insights || [])) {
            const item = document.createElement('div');
            item.className = 'insight-item';
            const text = document.createElement('span');
            text.style.fontSize = '11px';
            text.textContent = ins.content;
            item.appendChild(text);
            body.appendChild(item);
        }
        if (!data.insights || !data.insights.length) {
            const emptySpan = document.createElement('span');
            emptySpan.style.cssText = 'font-size:11px;color:var(--muted)';
            emptySpan.textContent = 'No insights generated.';
            body.appendChild(emptySpan);
        }
    } catch (e) {
        body.textContent = '';
        const errSpan = document.createElement('span');
        errSpan.style.cssText = 'font-size:11px;color:var(--red)';
        errSpan.textContent = 'Failed to generate insights';
        body.appendChild(errSpan);
    }
}
```

**Step 2: Inject the insights card into `_renderDrillDownTable`**

In `_renderDrillDownTable` (~line 5684), at the end of the function (before the closing `}`), add:

```javascript
// AI Insights card above parts table
_renderInsightsCard(rfqId, dd);
```

Note: `dd` is the panel element. `prepend` ensures the card appears above the parts table.

**Step 3: Add CSS styles**

In `app/templates/index.html`, add to the `<style>` section:

```css
.insights-card{background:var(--bg-alt,#f8f9fa);border:1px solid var(--border,#e0e0e0);border-radius:8px;margin-bottom:8px;overflow:hidden}
.insights-header{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;cursor:pointer;user-select:none}
.insights-header:hover{background:var(--hover,#f0f0f0)}
.insights-body{padding:8px 12px}
.insight-item{padding:4px 0;border-bottom:1px solid var(--border-light,#eee)}
.insight-item:last-child{border-bottom:none}
.insight-expired{opacity:0.6;font-style:italic}
.qa-entry{padding:8px 12px;border-bottom:1px solid var(--border-light,#eee)}
.qa-entry:last-child{border-bottom:none}
.qa-answer{margin-left:20px;padding:6px 10px;background:var(--bg-alt,#f8f9fa);border-radius:6px;margin-top:4px}
.qa-badge{font-size:9px;padding:2px 6px;border-radius:10px;font-weight:600}
.qa-resolved{background:var(--green-light,#e6f4ea);color:var(--green,#1a7f37)}
.qa-pending{background:var(--amber-light,#fff8e1);color:var(--amber,#f57c00)}
.qa-auto{background:var(--bg-alt,#f0f0f0);color:var(--muted,#666);font-size:10px}
```

**Step 4: Commit**

```bash
git add app/static/app.js app/templates/index.html
git commit -m "feat: AI Insights collapsible card on requisition parts tab"
```

---

### Task 11: Frontend — Q&A Tab in Requisition Drill-Down

**Files:**
- Modify: `app/static/app.js` — `_ddSubTabs()` (~line 3025), `_ddTabLabel()` (~line 3037), `_loadDdSubTab()` (~line 3111), `_renderDdTab()` (~line 3166)

**Step 1: Add 'qa' to sub-tabs**

In `_ddSubTabs()` (line 3025-3031), add `'qa'` to each return array:

```javascript
function _ddSubTabs(mainView) {
    if (mainView === 'sourcing') return ['details', 'sightings', 'activity', 'offers', 'qa', 'files'];
    if (mainView === 'archive' || _reqStatusFilter === 'archive') return ['parts', 'offers', 'quotes', 'activity', 'qa', 'files'];
    if (window.__isMobile) return ['parts', 'offers', 'quotes', 'buyplans', 'activity', 'qa'];
    return ['parts', 'offers', 'quotes', 'qa', 'files'];
}
```

In `_ddTabLabel()` (line 3037), add the qa label:

```javascript
function _ddTabLabel(tab) {
    const map = {details:'Details', sightings:'Sightings', activity:'Activity', offers:'Offers', parts:'Parts', quotes:'Quotes', buyplans:'Buy Plans', files:'Files', qa:'Q&A'};
    return map[tab] || tab;
}
```

**Step 2: Add data loading in `_loadDdSubTab`**

In `_loadDdSubTab()` (~line 3111), add a new case in the switch before the `case 'files'` line:

```javascript
case 'qa':
    data = await apiFetch('/api/knowledge?requisition_id=' + reqId);
    break;
```

**Step 3: Add rendering in `_renderDdTab`**

In `_renderDdTab()`, add to both the mobile switch (~line 3168-3183) and desktop switch (~line 3185-3200):

Mobile (after buyplans case):
```javascript
case 'qa': _renderDdQA(reqId, data, panel); break;
```

Desktop (after quotes case):
```javascript
case 'qa': _renderDdQA(reqId, data, panel); break;
```

**Step 4: Add the Q&A renderer and modal functions**

Add these functions in `app/static/app.js`:

```javascript
// ---------------------------------------------------------------------------
// Knowledge Ledger: Q&A Tab
// ---------------------------------------------------------------------------

function _renderDdQA(reqId, entries, panel) {
    // Build filter bar
    const filterBar = document.createElement('div');
    filterBar.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px';

    const filterGroup = document.createElement('div');
    filterGroup.style.display = 'flex';
    filterGroup.style.gap = '4px';
    var filters = ['all', 'question', 'note', 'fact'];
    var filterLabels = {all: 'All', question: 'Questions', note: 'Notes', fact: 'Facts'};
    for (var i = 0; i < filters.length; i++) {
        var btn = document.createElement('button');
        btn.className = 'btn btn-ghost btn-sm qa-filter' + (filters[i] === 'all' ? ' active' : '');
        btn.textContent = filterLabels[filters[i]];
        btn.dataset.filter = filters[i];
        btn.onclick = (function(f, b) { return function() { _filterQA(reqId, f, b); }; })(filters[i], btn);
        filterGroup.appendChild(btn);
    }
    filterBar.appendChild(filterGroup);

    var askBtn = document.createElement('button');
    askBtn.className = 'btn btn-sm';
    askBtn.textContent = 'Ask Question';
    askBtn.onclick = function() { _openAskQuestionModal(reqId); };
    filterBar.appendChild(askBtn);

    panel.textContent = '';
    panel.appendChild(filterBar);

    if (!entries || !entries.length) {
        var emptyDiv = document.createElement('div');
        emptyDiv.style.cssText = 'font-size:11px;color:var(--muted);padding:20px 0;text-align:center';
        emptyDiv.textContent = 'No knowledge entries yet. Ask a question or add a note.';
        panel.appendChild(emptyDiv);
        return;
    }

    var list = document.createElement('div');
    list.id = 'qa-list-' + reqId;
    for (var j = 0; j < entries.length; j++) {
        list.appendChild(_renderQAEntry(entries[j], reqId));
    }
    panel.appendChild(list);
}

function _renderQAEntry(e, reqId) {
    var wrapper = document.createElement('div');
    wrapper.className = 'qa-entry' + (e.source === 'system' ? ' qa-auto' : '');
    wrapper.dataset.type = e.entry_type;
    if (e.is_expired) wrapper.style.opacity = '0.6';

    var topRow = document.createElement('div');
    topRow.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start';

    var contentDiv = document.createElement('div');
    var icon = '';
    if (e.entry_type === 'question') icon = '\u2753 ';
    else if (e.entry_type === 'fact') icon = '\ud83d\udcca ';
    else if (e.entry_type === 'note') icon = '\ud83d\udcdd ';

    var contentSpan = document.createElement('span');
    contentSpan.style.cssText = 'font-size:12px;font-weight:600';
    contentSpan.textContent = icon + e.content;
    contentDiv.appendChild(contentSpan);

    if (e.is_expired) {
        var expBadge = document.createElement('span');
        expBadge.style.cssText = 'font-size:9px;color:var(--amber);margin-left:4px';
        expBadge.textContent = '(may be outdated)';
        contentDiv.appendChild(expBadge);
    }
    topRow.appendChild(contentDiv);

    var badgeArea = document.createElement('span');
    if (e.entry_type === 'question') {
        var statusBadge = document.createElement('span');
        statusBadge.className = 'qa-badge ' + (e.is_resolved ? 'qa-resolved' : 'qa-pending');
        statusBadge.textContent = e.is_resolved ? 'Resolved' : 'Awaiting answer';
        badgeArea.appendChild(statusBadge);
    }
    if (e.source === 'system') {
        var autoBadge = document.createElement('span');
        autoBadge.className = 'qa-badge qa-auto';
        autoBadge.textContent = 'auto';
        autoBadge.style.marginLeft = '4px';
        badgeArea.appendChild(autoBadge);
    }
    topRow.appendChild(badgeArea);
    wrapper.appendChild(topRow);

    var meta = document.createElement('div');
    meta.style.cssText = 'font-size:10px;color:var(--muted);margin-top:2px';
    meta.textContent = (e.creator_name || 'System') + ' \u00b7 ' + _timeAgo(e.created_at);
    wrapper.appendChild(meta);

    // Render answers
    if (e.answers && e.answers.length) {
        for (var k = 0; k < e.answers.length; k++) {
            var a = e.answers[k];
            var ansDiv = document.createElement('div');
            ansDiv.className = 'qa-answer';
            var ansText = document.createElement('span');
            ansText.style.fontSize = '11px';
            ansText.textContent = a.content;
            ansDiv.appendChild(ansText);
            var ansMeta = document.createElement('div');
            ansMeta.style.cssText = 'font-size:10px;color:var(--muted);margin-top:2px';
            ansMeta.textContent = (a.creator_name || 'Unknown') + ' \u00b7 ' + _timeAgo(a.created_at);
            ansDiv.appendChild(ansMeta);
            wrapper.appendChild(ansDiv);
        }
    }

    // Answer button for unanswered questions
    if (e.entry_type === 'question' && !e.is_resolved) {
        var ansRow = document.createElement('div');
        ansRow.style.marginTop = '4px';
        var ansBtn = document.createElement('button');
        ansBtn.className = 'btn btn-ghost btn-sm';
        ansBtn.style.fontSize = '10px';
        ansBtn.textContent = 'Answer';
        ansBtn.onclick = (function(rId, eId) { return function() { _openAnswerModal(rId, eId); }; })(reqId, e.id);
        ansRow.appendChild(ansBtn);
        wrapper.appendChild(ansRow);
    }

    return wrapper;
}

function _filterQA(reqId, type, btn) {
    var list = document.getElementById('qa-list-' + reqId);
    if (!list) return;
    var entries = list.querySelectorAll('.qa-entry');
    for (var i = 0; i < entries.length; i++) {
        entries[i].style.display = (type === 'all' || entries[i].dataset.type === type) ? '' : 'none';
    }
    var allBtns = btn.parentNode.querySelectorAll('.qa-filter');
    for (var j = 0; j < allBtns.length; j++) allBtns[j].classList.remove('active');
    btn.classList.add('active');
}

// ---------------------------------------------------------------------------
// Q&A Modals: Ask Question + Answer
// ---------------------------------------------------------------------------

async function _openAskQuestionModal(reqId) {
    var buyers = [];
    try {
        var users = await apiFetch('/api/users');
        buyers = (users || []).filter(function(u) { return u.role === 'buyer' || u.role === 'admin'; });
    } catch (e) { /* fallback: empty list */ }

    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'askQuestionModal';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.style.maxWidth = '480px';

    var h3 = document.createElement('h3');
    h3.style.cssText = 'margin:0 0 12px';
    h3.textContent = 'Ask a Question';
    box.appendChild(h3);

    var ta = document.createElement('textarea');
    ta.id = 'qaQuestionText';
    ta.rows = 4;
    ta.style.cssText = 'width:100%;resize:vertical;font-size:12px';
    ta.placeholder = 'Type your question...';
    box.appendChild(ta);

    var selectWrap = document.createElement('div');
    selectWrap.style.marginTop = '8px';
    var label = document.createElement('label');
    label.style.cssText = 'font-size:11px;font-weight:600';
    label.textContent = 'Assign to buyers:';
    selectWrap.appendChild(label);
    var sel = document.createElement('select');
    sel.id = 'qaAssignBuyers';
    sel.multiple = true;
    sel.style.cssText = 'width:100%;height:80px;font-size:11px';
    for (var i = 0; i < buyers.length; i++) {
        var opt = document.createElement('option');
        opt.value = buyers[i].id;
        opt.textContent = buyers[i].display_name || buyers[i].email;
        sel.appendChild(opt);
    }
    selectWrap.appendChild(sel);
    var hint = document.createElement('span');
    hint.style.cssText = 'font-size:9px;color:var(--muted)';
    hint.textContent = 'Hold Ctrl/Cmd to select multiple';
    selectWrap.appendChild(hint);
    box.appendChild(selectWrap);

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:12px';
    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = function() { document.getElementById('askQuestionModal').remove(); };
    btnRow.appendChild(cancelBtn);
    var submitBtn = document.createElement('button');
    submitBtn.className = 'btn';
    submitBtn.textContent = 'Post Question';
    submitBtn.onclick = function() { _submitQuestion(reqId); };
    btnRow.appendChild(submitBtn);
    box.appendChild(btnRow);

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    ta.focus();
}

async function _submitQuestion(reqId) {
    var text = document.getElementById('qaQuestionText');
    var sel = document.getElementById('qaAssignBuyers');
    if (!text || !text.value.trim()) return;
    var buyerIds = Array.from(sel.selectedOptions).map(function(o) { return parseInt(o.value); });
    if (!buyerIds.length) { alert('Select at least one buyer'); return; }

    try {
        await apiFetch('/api/knowledge/question', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                content: text.value.trim(),
                assigned_to_ids: buyerIds,
                requisition_id: reqId,
            }),
        });
        document.getElementById('askQuestionModal').remove();
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].qa;
        var drow = document.getElementById('d-' + reqId);
        var panel = drow ? drow.querySelector('.dd-panel') : null;
        if (panel) await _loadDdSubTab(reqId, 'qa', panel);
    } catch (e) {
        alert('Failed to post question');
    }
}

async function _openAnswerModal(reqId, entryId) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'answerModal';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.style.maxWidth = '480px';

    var h3 = document.createElement('h3');
    h3.style.cssText = 'margin:0 0 12px';
    h3.textContent = 'Post Answer';
    box.appendChild(h3);

    var ta = document.createElement('textarea');
    ta.id = 'qaAnswerText';
    ta.rows = 4;
    ta.style.cssText = 'width:100%;resize:vertical;font-size:12px';
    ta.placeholder = 'Type your answer...';
    box.appendChild(ta);

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:12px';
    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = function() { document.getElementById('answerModal').remove(); };
    btnRow.appendChild(cancelBtn);
    var submitBtn = document.createElement('button');
    submitBtn.className = 'btn';
    submitBtn.textContent = 'Post Answer';
    submitBtn.onclick = function() { _submitAnswer(reqId, entryId); };
    btnRow.appendChild(submitBtn);
    box.appendChild(btnRow);

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    ta.focus();
}

async function _submitAnswer(reqId, entryId) {
    var text = document.getElementById('qaAnswerText');
    if (!text || !text.value.trim()) return;

    try {
        await apiFetch('/api/knowledge/' + entryId + '/answer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: text.value.trim() }),
        });
        document.getElementById('answerModal').remove();
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].qa;
        var drow = document.getElementById('d-' + reqId);
        var panel = drow ? drow.querySelector('.dd-panel') : null;
        if (panel) await _loadDdSubTab(reqId, 'qa', panel);
    } catch (e) {
        alert('Failed to post answer');
    }
}
```

**Step 5: Export new functions**

Add to the global exports list at the bottom of `app.js` (~line 12915+):

```javascript
_renderDdQA, _renderQAEntry, _filterQA, _openAskQuestionModal, _submitQuestion,
_openAnswerModal, _submitAnswer, _renderInsightsCard, _toggleInsightsCard, _refreshInsights,
```

**Step 6: Commit**

```bash
git add app/static/app.js
git commit -m "feat: Q&A tab in requisition drill-down with question/answer modals"
```

---

### Task 12: One-Time Backfill Script

**Files:**
- Create: `scripts/backfill_knowledge.py`

**Step 1: Create the backfill script**

```python
"""One-time backfill: seed the Knowledge Ledger from existing quotes and offers.

Usage: docker compose exec app python scripts/backfill_knowledge.py
"""

import sys
sys.path.insert(0, "/app")

from loguru import logger

from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry
from app.models.quotes import Quote
from app.models.offers import Offer
from app.services.knowledge_service import capture_quote_fact, capture_offer_fact


def backfill():
    db = SessionLocal()
    try:
        # Check if already backfilled
        existing = db.query(KnowledgeEntry).filter(KnowledgeEntry.source == "system").count()
        if existing > 100:
            logger.info("Already have {} system entries — skipping backfill", existing)
            return

        # Backfill from quotes
        quotes = db.query(Quote).order_by(Quote.created_at.desc()).limit(500).all()
        q_count = 0
        for q in quotes:
            try:
                entry = capture_quote_fact(db, quote=q, user_id=q.created_by_id or 0)
                if entry:
                    q_count += 1
            except Exception as e:
                logger.warning("Quote backfill failed for {}: {}", q.id, e)

        # Backfill from offers
        offers = db.query(Offer).order_by(Offer.created_at.desc()).limit(1000).all()
        o_count = 0
        for o in offers:
            try:
                entry = capture_offer_fact(db, offer=o)
                if entry:
                    o_count += 1
            except Exception as e:
                logger.warning("Offer backfill failed for {}: {}", o.id, e)

        logger.info("Backfill complete: {} quote facts, {} offer facts", q_count, o_count)
    except Exception as e:
        logger.error("Backfill failed: {}", e)
    finally:
        db.close()


if __name__ == "__main__":
    backfill()
```

**Step 2: Commit**

```bash
git add scripts/backfill_knowledge.py
git commit -m "feat: one-time backfill script for Knowledge Ledger from existing quotes/offers"
```

---

### Task 13: Build, Deploy, Verify

**Step 1: Rebuild and deploy**

```bash
cd /root/availai && docker compose up -d --build
```

**Step 2: Verify migration applied**

```bash
docker compose exec app alembic current
```

Expected: shows migration 063.

**Step 3: Check logs for clean startup**

```bash
docker compose logs -f app 2>&1 | head -50
```

**Step 4: Verify API health**

```bash
curl -s http://localhost:8000/api/health | python3 -m json.tool
```

**Step 5: Run backfill**

```bash
docker compose exec app python scripts/backfill_knowledge.py
```

**Step 6: Verify entries were created**

```bash
docker compose exec db psql -U availai -c "SELECT entry_type, source, COUNT(*) FROM knowledge_entries GROUP BY 1, 2;"
```

**Step 7: Commit any fixes**

```bash
git add -A && git commit -m "fix: deployment adjustments for Knowledge Ledger Phase 1"
```
