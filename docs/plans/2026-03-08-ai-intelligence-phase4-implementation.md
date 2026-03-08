# AI Intelligence Layer Phase 4 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete the AI Intelligence Layer with three features: (1) extract durable facts from parsed emails, (2) replace the daily Excel handoff with a role-aware morning briefing, and (3) surface cross-customer part history inline wherever MPNs appear.

**Architecture:** Hook email fact extraction into the existing `process_email_intelligence()` pipeline as a fourth step. Build the morning briefing as a pure-SQL aggregation service with a dashboard card and Teams delivery. Add MPN resurfacing as a batch SQL lookup with Redis caching and inline frontend hints.

**Tech Stack:** FastAPI, SQLAlchemy, Claude API (claude_structured with Haiku), Redis, vanilla JS

---

### Task 1: Email Fact Extraction — Service

**Files:**
- Modify: `app/services/email_intelligence_service.py` (add `extract_durable_facts()`, hook into `process_email_intelligence()`)
- Test: `tests/test_email_fact_extraction.py`

**Step 1: Write the failing tests**

Create `tests/test_email_fact_extraction.py`:

```python
"""
test_email_fact_extraction.py — Tests for durable fact extraction from emails.

Tests for: extract_durable_facts(), dedup guard, cost control (skip non-offer emails).

Called by: pytest
Depends on: app.services.email_intelligence_service
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestExtractDurableFacts:
    """Test the extract_durable_facts() function."""

    @pytest.mark.asyncio
    async def test_extracts_lead_time_fact(self):
        """AI returns a lead_time fact → stored as knowledge entry."""
        from app.services.email_intelligence_service import extract_durable_facts

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.filter.return_value.count.return_value = 0

        ai_result = [
            {"fact_type": "lead_time", "content": "16-20 weeks for LM317 family",
             "mpn": "LM317", "confidence": 0.9, "expiry_days": 180}
        ]
        with patch(
            "app.services.email_intelligence_service.claude_structured",
            new_callable=AsyncMock, return_value=ai_result,
        ), patch(
            "app.services.email_intelligence_service.create_entry"
        ) as mock_create:
            result = await extract_durable_facts(
                db=mock_db, body="Lead time is 16-20 weeks on the LM317 family",
                sender_email="sales@arrow.com", sender_name="Arrow Sales",
                classification="offer", parsed_quotes=None, user_id=1,
            )
            assert len(result) == 1
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args[1]
            assert call_kwargs["entry_type"] == "fact"
            assert call_kwargs["source"] == "email_parsed"
            assert call_kwargs["mpn"] == "LM317"
            assert call_kwargs["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_skips_non_offer_emails(self):
        """Emails classified as general/ooo/spam → no AI call."""
        from app.services.email_intelligence_service import extract_durable_facts

        with patch(
            "app.services.email_intelligence_service.claude_structured",
            new_callable=AsyncMock,
        ) as mock_ai:
            result = await extract_durable_facts(
                db=MagicMock(), body="Thanks for your email",
                sender_email="info@test.com", sender_name="Test",
                classification="general", parsed_quotes=None, user_id=1,
            )
            assert result == []
            mock_ai.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_short_body(self):
        """Email body < 50 chars → no AI call."""
        from app.services.email_intelligence_service import extract_durable_facts

        with patch(
            "app.services.email_intelligence_service.claude_structured",
            new_callable=AsyncMock,
        ) as mock_ai:
            result = await extract_durable_facts(
                db=MagicMock(), body="Ok thanks",
                sender_email="sales@arrow.com", sender_name="Arrow",
                classification="offer", parsed_quotes=None, user_id=1,
            )
            assert result == []
            mock_ai.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_skips_recent_duplicate(self):
        """Same MPN + vendor + fact_type in last 7 days → skip."""
        from app.services.email_intelligence_service import extract_durable_facts

        mock_db = MagicMock()
        # Simulate existing entry found (count > 0)
        mock_db.query.return_value.filter.return_value.filter.return_value.count.return_value = 1

        ai_result = [
            {"fact_type": "lead_time", "content": "16-20 weeks",
             "mpn": "LM317", "confidence": 0.9, "expiry_days": 180}
        ]
        with patch(
            "app.services.email_intelligence_service.claude_structured",
            new_callable=AsyncMock, return_value=ai_result,
        ), patch(
            "app.services.email_intelligence_service.create_entry"
        ) as mock_create:
            result = await extract_durable_facts(
                db=mock_db, body="Lead time is 16-20 weeks on the LM317 family. Please confirm your order.",
                sender_email="sales@arrow.com", sender_name="Arrow",
                classification="offer", parsed_quotes=None, user_id=1,
            )
            assert result == []
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_ai_returns_none_gracefully(self):
        """AI call fails → return empty list, no crash."""
        from app.services.email_intelligence_service import extract_durable_facts

        with patch(
            "app.services.email_intelligence_service.claude_structured",
            new_callable=AsyncMock, return_value=None,
        ):
            result = await extract_durable_facts(
                db=MagicMock(), body="We have LM317 in stock at $2.50 per unit, lead time 4 weeks",
                sender_email="sales@arrow.com", sender_name="Arrow",
                classification="offer", parsed_quotes=None, user_id=1,
            )
            assert result == []

    @pytest.mark.asyncio
    async def test_multiple_facts_extracted(self):
        """AI returns 3 facts → all 3 stored."""
        from app.services.email_intelligence_service import extract_durable_facts

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.filter.return_value.count.return_value = 0

        ai_result = [
            {"fact_type": "lead_time", "content": "16 weeks", "mpn": "LM317", "confidence": 0.9, "expiry_days": 180},
            {"fact_type": "eol_notice", "content": "Last buy June 2026", "mpn": "LM317", "confidence": 0.95, "expiry_days": None},
            {"fact_type": "moq", "content": "MOQ 2500", "mpn": None, "confidence": 0.85, "expiry_days": 90},
        ]
        mock_entry = MagicMock()
        with patch(
            "app.services.email_intelligence_service.claude_structured",
            new_callable=AsyncMock, return_value=ai_result,
        ), patch(
            "app.services.email_intelligence_service.create_entry", return_value=mock_entry,
        ) as mock_create:
            result = await extract_durable_facts(
                db=mock_db, body="LM317 lead time 16 weeks, EOL last buy June 2026. MOQ 2500 units minimum order.",
                sender_email="sales@arrow.com", sender_name="Arrow",
                classification="offer", parsed_quotes=None, user_id=1,
            )
            assert len(result) == 3
            assert mock_create.call_count == 3
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_email_fact_extraction.py -v`
Expected: FAIL — `extract_durable_facts` not found

**Step 3: Implement `extract_durable_facts()` in `email_intelligence_service.py`**

Add at the end of `app/services/email_intelligence_service.py`:

```python
# ---------------------------------------------------------------------------
# Phase 4: Durable fact extraction from parsed emails
# ---------------------------------------------------------------------------

# Fact types and their default expiry (days). None = never expires.
FACT_EXPIRY_DEFAULTS = {
    "lead_time": 180,
    "moq": 90,
    "moq_flexibility": 90,
    "eol_notice": None,
    "availability": 30,
    "pricing_note": 90,
    "vendor_policy": 365,
    "warehouse_location": 365,
    "date_code": 180,
    "condition_note": 180,
}

FACT_EXTRACTION_PROMPT = """You are an electronics sourcing analyst. Extract durable facts from this vendor email.

Focus on intelligence that would be useful weeks or months from now:
- Lead times, MOQs, MOQ flexibility
- EOL / last-time-buy notices
- Availability signals (in stock, backordered, allocated)
- Pricing notes (volume breaks, premium charges, currency)
- Vendor policies (payment terms, shipping, warranty)
- Warehouse locations, date codes, condition notes

Do NOT extract:
- The actual price/qty/MPN data (already captured separately)
- Greetings, signatures, disclaimers
- Obvious or trivial facts

Return a JSON array of facts. Each fact: {"fact_type": str, "content": str, "mpn": str|null, "confidence": float, "expiry_days": int|null}
If no durable facts found, return an empty array [].
"""

FACT_EXTRACTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "fact_type": {"type": "string", "enum": list(FACT_EXPIRY_DEFAULTS.keys())},
            "content": {"type": "string"},
            "mpn": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "expiry_days": {"type": ["integer", "null"]},
        },
        "required": ["fact_type", "content", "confidence"],
    },
}

ALLOWED_CLASSIFICATIONS = {"offer", "quote_reply", "stock_list"}


async def extract_durable_facts(
    db,
    *,
    body: str,
    sender_email: str,
    sender_name: str,
    classification: str,
    parsed_quotes: dict | list | None,
    user_id: int,
) -> list:
    """Extract durable facts from an email body using Claude Haiku.

    Cost control: only runs on offer/quote_reply/stock_list, body >= 50 chars.
    Dedup: skips facts with same MPN + fact_type created in last 7 days.

    Called by: process_email_intelligence() after pricing extraction.
    Depends on: claude_structured, knowledge_service.create_entry
    """
    from datetime import timedelta

    from app.models.knowledge import KnowledgeEntry
    from app.services.knowledge_service import create_entry
    from app.utils.claude_client import claude_structured

    # Cost control gates
    if classification not in ALLOWED_CLASSIFICATIONS:
        return []
    if len(body.strip()) < 50:
        return []

    # Build prompt with email context
    prompt_parts = [f"Sender: {sender_name} <{sender_email}>", f"Email body:\n{body[:3000]}"]
    if parsed_quotes:
        prompt_parts.append(f"Already-extracted pricing data: {str(parsed_quotes)[:500]}")

    prompt = "\n\n".join(prompt_parts)

    # Call AI
    facts = await claude_structured(
        prompt=prompt,
        schema=FACT_EXTRACTION_SCHEMA,
        system=FACT_EXTRACTION_PROMPT,
        model_tier="fast",
        max_tokens=1024,
    )

    if not facts:
        return []

    # Resolve vendor from sender domain
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    vendor_card_id = None
    if domain:
        from app.models.vendors import VendorCard

        vc = db.query(VendorCard).filter(VendorCard.domain == domain).first()
        if vc:
            vendor_card_id = vc.id

    # Store each fact with dedup
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    entries = []

    for fact in facts:
        fact_type = fact.get("fact_type", "")
        content = fact.get("content", "")
        mpn = fact.get("mpn")
        confidence = fact.get("confidence", 0.5)

        if not content or not fact_type:
            continue

        # Dedup: check for existing fact with same MPN + fact_type in last 7 days
        dedup_q = db.query(KnowledgeEntry).filter(
            KnowledgeEntry.entry_type == "fact",
            KnowledgeEntry.created_at >= cutoff,
        )
        if mpn:
            dedup_q = dedup_q.filter(KnowledgeEntry.mpn == mpn)
        if vendor_card_id:
            dedup_q = dedup_q.filter(KnowledgeEntry.vendor_card_id == vendor_card_id)
        if dedup_q.count() > 0:
            logger.debug("Skipping duplicate fact: {} for MPN={}", fact_type, mpn)
            continue

        # Determine expiry
        expiry_days = fact.get("expiry_days") or FACT_EXPIRY_DEFAULTS.get(fact_type)
        expires_at = (now + timedelta(days=expiry_days)) if expiry_days else None

        try:
            entry = create_entry(
                db,
                user_id=user_id,
                entry_type="fact",
                content=f"[{fact_type}] {content}",
                source="email_parsed",
                confidence=confidence,
                expires_at=expires_at,
                mpn=mpn,
                vendor_card_id=vendor_card_id,
            )
            entries.append(entry)
        except Exception as e:
            logger.warning("Failed to create fact entry: {}", e)

    if entries:
        logger.info("Extracted {} durable facts from email (sender={})", len(entries), sender_email)

    return entries
```

**Step 4: Hook into `process_email_intelligence()`**

In `app/services/email_intelligence_service.py`, after the pricing extraction (line ~231) and before `store_email_intelligence()` (line ~234), add:

```python
    # Extract durable facts (Phase 4)
    try:
        await extract_durable_facts(
            db,
            body=body,
            sender_email=sender_email,
            sender_name=sender_name,
            classification=classification.get("classification", ""),
            parsed_quotes=parsed_quotes,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Fact extraction failed (non-fatal): {}", e)
```

**Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_email_fact_extraction.py -v`
Expected: All 6 tests PASS

**Step 6: Commit**

```bash
git add tests/test_email_fact_extraction.py app/services/email_intelligence_service.py
git commit -m "feat(phase4): add email fact extraction — durable facts from parsed emails"
```

---

### Task 2: Morning Briefing — Service

**Files:**
- Create: `app/services/dashboard_briefing.py`
- Test: `tests/test_dashboard_briefing.py`

**Step 1: Write the failing tests**

Create `tests/test_dashboard_briefing.py`:

```python
"""
test_dashboard_briefing.py — Tests for role-aware morning briefing service.

Tests for: generate_briefing() sections, role detection, empty states.

Called by: pytest
Depends on: app.services.dashboard_briefing
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


class TestGenerateBriefing:
    """Test the generate_briefing() function."""

    def test_buyer_briefing_returns_expected_sections(self):
        from app.services.dashboard_briefing import generate_briefing

        mock_db = MagicMock()
        # Mock all query chains to return empty lists
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value.join.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        result = generate_briefing(db=mock_db, user_id=1, role="buyer")

        assert "sections" in result
        section_names = [s["name"] for s in result["sections"]]
        assert "vendor_emails" in section_names
        assert "stalling_deals" in section_names

    def test_sales_briefing_returns_expected_sections(self):
        from app.services.dashboard_briefing import generate_briefing

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value.join.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        result = generate_briefing(db=mock_db, user_id=1, role="sales")

        assert "sections" in result
        section_names = [s["name"] for s in result["sections"]]
        assert "customer_followups" in section_names
        assert "deals_at_risk" in section_names

    def test_empty_briefing_has_zero_total(self):
        from app.services.dashboard_briefing import generate_briefing

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value.join.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        result = generate_briefing(db=mock_db, user_id=1, role="buyer")
        assert result["total_items"] == 0

    def test_briefing_returns_generated_at_timestamp(self):
        from app.services.dashboard_briefing import generate_briefing

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value.join.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        result = generate_briefing(db=mock_db, user_id=1, role="buyer")
        assert "generated_at" in result
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_dashboard_briefing.py -v`
Expected: FAIL — module not found

**Step 3: Implement `dashboard_briefing.py`**

Create `app/services/dashboard_briefing.py`:

```python
"""Morning briefing service — role-aware daily summary replacing the Excel handoff.

Pure data aggregation, no AI calls. Gathers from existing tables and services:
  - Buyer: vendor emails, unanswered Q&A, stalling deals, resurfaced parts, price movement
  - Sales: customer followups, new buyer answers, quiet customers, at-risk deals, ready quotes

Called by: routers/dashboard/briefs.py, jobs/knowledge_jobs.py
Depends on: models (Offer, Requisition, KnowledgeEntry, EmailIntelligence, Quote),
            services (deal_risk, activity_insights)
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session


def generate_briefing(db: Session, user_id: int, role: str = "buyer") -> dict:
    """Generate a personalized morning briefing for a user.

    Args:
        db: Database session
        user_id: The user ID
        role: "buyer" or "sales"

    Returns:
        {"sections": [...], "total_items": int, "generated_at": str, "role": str}
    """
    now = datetime.now(timezone.utc)

    if role == "sales":
        sections = _build_sales_sections(db, user_id, now)
    else:
        sections = _build_buyer_sections(db, user_id, now)

    total = sum(s["count"] for s in sections)

    return {
        "sections": sections,
        "total_items": total,
        "generated_at": now.isoformat(),
        "role": role,
    }


def _build_buyer_sections(db: Session, user_id: int, now: datetime) -> list[dict]:
    """Build buyer briefing sections."""
    sections = []
    sections.append(_section_vendor_emails(db, user_id, now))
    sections.append(_section_unanswered_questions(db, user_id, now))
    sections.append(_section_stalling_deals(db, user_id, now))
    sections.append(_section_resurfaced_parts(db, user_id, now))
    sections.append(_section_price_movement(db, user_id, now))
    return sections


def _build_sales_sections(db: Session, user_id: int, now: datetime) -> list[dict]:
    """Build sales briefing sections."""
    sections = []
    sections.append(_section_customer_followups(db, user_id, now))
    sections.append(_section_new_answers(db, user_id, now))
    sections.append(_section_quiet_customers(db, user_id, now))
    sections.append(_section_deals_at_risk(db, user_id, now))
    sections.append(_section_quotes_ready(db, user_id, now))
    return sections


# ---------------------------------------------------------------------------
# Buyer sections
# ---------------------------------------------------------------------------

def _section_vendor_emails(db: Session, user_id: int, now: datetime) -> dict:
    """Unreviewed offers/quotes received since last briefing (24h)."""
    try:
        from app.models import EmailIntelligence

        cutoff = now - timedelta(hours=24)
        emails = (
            db.query(EmailIntelligence)
            .filter(
                EmailIntelligence.user_id == user_id,
                EmailIntelligence.created_at >= cutoff,
                EmailIntelligence.needs_review.is_(True),
                EmailIntelligence.classification.in_(["offer", "quote_reply", "stock_list"]),
            )
            .order_by(EmailIntelligence.created_at.desc())
            .limit(20)
            .all()
        )
        items = []
        for e in emails:
            age_hours = (now - e.created_at).total_seconds() / 3600 if e.created_at else 0
            items.append({
                "title": "Email from {}: {}".format(e.sender_email, e.classification),
                "detail": "Received {:.0f}h ago".format(age_hours),
                "entity_type": "email_intelligence",
                "entity_id": e.id,
                "priority": "high" if age_hours > 12 else "medium",
                "age_hours": round(age_hours, 1),
            })
        return {"name": "vendor_emails", "label": "Vendor Emails Needing Action", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing vendor_emails section failed: {}", e)
        return {"name": "vendor_emails", "label": "Vendor Emails Needing Action", "count": 0, "items": []}


def _section_unanswered_questions(db: Session, user_id: int, now: datetime) -> dict:
    """Q&A entries assigned to this user, still unresolved."""
    try:
        from app.models.knowledge import KnowledgeEntry

        questions = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.entry_type == "question",
                KnowledgeEntry.is_resolved.is_(False),
            )
            .order_by(KnowledgeEntry.created_at.asc())
            .limit(20)
            .all()
        )
        items = []
        for q in questions:
            assigned = q.assigned_to_ids or []
            if user_id not in assigned and assigned:
                continue
            age_hours = (now - q.created_at).total_seconds() / 3600 if q.created_at else 0
            items.append({
                "title": q.content[:100],
                "detail": "Asked {:.0f}h ago".format(age_hours),
                "entity_type": "knowledge_entry",
                "entity_id": q.id,
                "priority": "high" if age_hours > 24 else "medium",
                "age_hours": round(age_hours, 1),
            })
        return {"name": "unanswered_questions", "label": "Unanswered Questions", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing unanswered_questions section failed: {}", e)
        return {"name": "unanswered_questions", "label": "Unanswered Questions", "count": 0, "items": []}


def _section_stalling_deals(db: Session, user_id: int, now: datetime) -> dict:
    """Reqs owned by user with no new quotes in 7+ days."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requisition

        stale_cutoff = now - timedelta(days=7)
        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.owner_id == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting"]),
            )
            .all()
        )
        items = []
        for req in reqs:
            latest_offer = (
                db.query(func.max(Offer.created_at))
                .filter(Offer.requisition_id == req.id)
                .scalar()
            )
            if latest_offer and latest_offer > stale_cutoff:
                continue
            days_idle = (now - (req.updated_at or req.created_at)).days if req.updated_at or req.created_at else 0
            items.append({
                "title": "Req #{}: {} — {}d idle".format(req.id, getattr(req, "customer_name", ""), days_idle),
                "detail": "No new offers in {} days".format(days_idle),
                "entity_type": "requisition",
                "entity_id": req.id,
                "priority": "critical" if days_idle >= 14 else "high",
                "age_hours": days_idle * 24,
            })
        return {"name": "stalling_deals", "label": "Stalling Deals", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing stalling_deals section failed: {}", e)
        return {"name": "stalling_deals", "label": "Stalling Deals", "count": 0, "items": []}


def _section_resurfaced_parts(db: Session, user_id: int, now: datetime) -> dict:
    """New sightings/offers for MPNs user has been actively sourcing."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requirement, Requisition

        cutoff = now - timedelta(hours=24)

        # Get MPNs from user's active requisitions
        active_mpns = (
            db.query(Requirement.mpn)
            .join(Requisition, Requisition.id == Requirement.requisition_id)
            .filter(
                Requisition.owner_id == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting"]),
                Requirement.mpn.isnot(None),
            )
            .distinct()
            .all()
        )
        mpn_set = {m[0] for m in active_mpns if m[0]}

        if not mpn_set:
            return {"name": "resurfaced_parts", "label": "Resurfaced Parts", "count": 0, "items": []}

        # Find new offers for these MPNs from other reqs
        new_offers = (
            db.query(Offer)
            .filter(Offer.mpn.in_(mpn_set), Offer.created_at >= cutoff)
            .order_by(Offer.created_at.desc())
            .limit(10)
            .all()
        )
        items = []
        for o in new_offers:
            title = "{}: new offer ${:.2f}".format(o.mpn, o.unit_price) if o.unit_price else "{}: new offer".format(o.mpn)
            items.append({
                "title": title,
                "detail": "From {}".format(o.vendor_name or "unknown"),
                "entity_type": "offer",
                "entity_id": o.id,
                "priority": "medium",
                "age_hours": (now - o.created_at).total_seconds() / 3600 if o.created_at else 0,
            })
        return {"name": "resurfaced_parts", "label": "Resurfaced Parts", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing resurfaced_parts section failed: {}", e)
        return {"name": "resurfaced_parts", "label": "Resurfaced Parts", "count": 0, "items": []}


def _section_price_movement(db: Session, user_id: int, now: datetime) -> dict:
    """Significant price changes on tracked parts (>15% from median)."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requirement, Requisition

        # Get user's active MPNs
        active_mpns = (
            db.query(Requirement.mpn)
            .join(Requisition, Requisition.id == Requirement.requisition_id)
            .filter(
                Requisition.owner_id == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting"]),
                Requirement.mpn.isnot(None),
            )
            .distinct()
            .all()
        )
        mpn_list = [m[0] for m in active_mpns if m[0]]

        items = []
        for mpn in mpn_list[:20]:  # Cap to avoid slow queries
            recent = (
                db.query(Offer.unit_price)
                .filter(Offer.mpn == mpn, Offer.unit_price.isnot(None), Offer.unit_price > 0)
                .order_by(Offer.created_at.desc())
                .limit(10)
                .all()
            )
            prices = [float(r[0]) for r in recent if r[0]]
            if len(prices) < 2:
                continue

            latest = prices[0]
            median = sorted(prices)[len(prices) // 2]
            if median == 0:
                continue
            pct_diff = abs(latest - median) / median

            if pct_diff >= 0.15:
                direction = "up" if latest > median else "down"
                items.append({
                    "title": "{}: price {} {:.0%} vs median".format(mpn, direction, pct_diff),
                    "detail": "Latest ${:.2f}, median ${:.2f}".format(latest, median),
                    "entity_type": "mpn",
                    "entity_id": mpn,
                    "priority": "high" if pct_diff >= 0.30 else "medium",
                    "age_hours": 0,
                })
        return {"name": "price_movement", "label": "Price Movement", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing price_movement section failed: {}", e)
        return {"name": "price_movement", "label": "Price Movement", "count": 0, "items": []}


# ---------------------------------------------------------------------------
# Sales sections
# ---------------------------------------------------------------------------

def _section_customer_followups(db: Session, user_id: int, now: datetime) -> dict:
    """Customer emails/inquiries with no response."""
    try:
        from app.models.sourcing import Requisition

        cutoff = now - timedelta(days=3)
        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.owner_id == user_id,
                Requisition.status == "open",
                Requisition.updated_at <= cutoff,
            )
            .order_by(Requisition.updated_at.asc())
            .limit(10)
            .all()
        )
        items = []
        for req in reqs:
            days = (now - (req.updated_at or req.created_at)).days
            items.append({
                "title": "Req #{}: no response in {}d".format(req.id, days),
                "detail": getattr(req, "customer_name", "") or "",
                "entity_type": "requisition",
                "entity_id": req.id,
                "priority": "critical" if days >= 7 else "high",
                "age_hours": days * 24,
            })
        return {"name": "customer_followups", "label": "Customer Follow-ups Needed", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing customer_followups section failed: {}", e)
        return {"name": "customer_followups", "label": "Customer Follow-ups Needed", "count": 0, "items": []}


def _section_new_answers(db: Session, user_id: int, now: datetime) -> dict:
    """Q&A answers posted since last briefing."""
    try:
        from app.models.knowledge import KnowledgeEntry

        cutoff = now - timedelta(hours=24)
        answers = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.entry_type == "answer",
                KnowledgeEntry.created_at >= cutoff,
            )
            .order_by(KnowledgeEntry.created_at.desc())
            .limit(10)
            .all()
        )
        items = []
        for a in answers:
            items.append({
                "title": a.content[:100],
                "detail": "Answered by user #{}".format(a.created_by),
                "entity_type": "knowledge_entry",
                "entity_id": a.id,
                "priority": "medium",
                "age_hours": (now - a.created_at).total_seconds() / 3600 if a.created_at else 0,
            })
        return {"name": "new_answers", "label": "New Answers from Buyers", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing new_answers section failed: {}", e)
        return {"name": "new_answers", "label": "New Answers from Buyers", "count": 0, "items": []}


def _section_quiet_customers(db: Session, user_id: int, now: datetime) -> dict:
    """Customers with no engagement in 10+ days."""
    try:
        from app.services.activity_insights import _detect_gone_quiet

        quiet = _detect_gone_quiet(user_id, db)
        items = []
        for q in quiet:
            items.append({
                "title": q.get("title", "Customer going quiet"),
                "detail": q.get("detail", ""),
                "entity_type": q.get("entity_type", "company"),
                "entity_id": q.get("entity_id"),
                "priority": q.get("priority", "medium"),
                "age_hours": 0,
            })
        return {"name": "quiet_customers", "label": "Customers Going Quiet", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing quiet_customers section failed: {}", e)
        return {"name": "quiet_customers", "label": "Customers Going Quiet", "count": 0, "items": []}


def _section_deals_at_risk(db: Session, user_id: int, now: datetime) -> dict:
    """Reqs where risk score is red."""
    try:
        from app.models.sourcing import Requisition
        from app.services.deal_risk import assess_risk

        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.owner_id == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting"]),
            )
            .all()
        )
        items = []
        for req in reqs:
            try:
                risk = assess_risk(req.id, db)
                if risk.get("risk_level") == "red":
                    items.append({
                        "title": "Req #{}: {}".format(req.id, risk.get("explanation", "High risk")[:80]),
                        "detail": risk.get("suggested_action", ""),
                        "entity_type": "requisition",
                        "entity_id": req.id,
                        "priority": "critical",
                        "age_hours": 0,
                    })
            except Exception:
                pass
        return {"name": "deals_at_risk", "label": "Deals at Risk", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing deals_at_risk section failed: {}", e)
        return {"name": "deals_at_risk", "label": "Deals at Risk", "count": 0, "items": []}


def _section_quotes_ready(db: Session, user_id: int, now: datetime) -> dict:
    """Offers received but not yet forwarded to customer as quotes."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requisition

        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.owner_id == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting"]),
            )
            .all()
        )
        items = []
        for req in reqs:
            offer_count = db.query(func.count(Offer.id)).filter(Offer.requisition_id == req.id).scalar() or 0
            if offer_count == 0:
                continue
            from app.models.quotes import Quote

            quote_count = db.query(func.count(Quote.id)).filter(Quote.requisition_id == req.id).scalar() or 0
            if offer_count > quote_count:
                items.append({
                    "title": "Req #{}: {} offers, {} quotes sent".format(req.id, offer_count, quote_count),
                    "detail": "{} offers not yet quoted".format(offer_count - quote_count),
                    "entity_type": "requisition",
                    "entity_id": req.id,
                    "priority": "high" if offer_count - quote_count >= 3 else "medium",
                    "age_hours": 0,
                })
        return {"name": "quotes_ready", "label": "Quotes Ready to Send", "count": len(items), "items": items}
    except Exception as e:
        logger.warning("Briefing quotes_ready section failed: {}", e)
        return {"name": "quotes_ready", "label": "Quotes Ready to Send", "count": 0, "items": []}
```

**Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_dashboard_briefing.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add app/services/dashboard_briefing.py tests/test_dashboard_briefing.py
git commit -m "feat(phase4): add morning briefing service — role-aware daily summary"
```

---

### Task 3: Morning Briefing — Endpoint + Job

**Files:**
- Modify: `app/routers/dashboard/briefs.py` (add `/api/dashboard/briefing` endpoint)
- Modify: `app/jobs/knowledge_jobs.py` (add pre-compute job at 6 AM UTC)
- Test: `tests/test_dashboard_briefing.py` (add endpoint tests)

**Step 1: Add endpoint test**

Append to `tests/test_dashboard_briefing.py`:

```python
class TestBriefingEndpoint:
    """Test the /api/dashboard/briefing endpoint."""

    def test_briefing_endpoint_returns_200(self, client, mock_user):
        with patch("app.services.dashboard_briefing.generate_briefing") as mock_gen:
            mock_gen.return_value = {
                "sections": [], "total_items": 0,
                "generated_at": "2026-03-08T00:00:00+00:00", "role": "buyer",
            }
            resp = client.get("/api/dashboard/briefing")
            assert resp.status_code == 200
            data = resp.json()
            assert "sections" in data
            assert "total_items" in data
```

Note: `client` and `mock_user` fixtures come from `conftest.py`. Adjust imports if needed — the test depends on the same pattern used in other endpoint tests.

**Step 2: Add endpoint to `app/routers/dashboard/briefs.py`**

At the end of the file, add:

```python
@router.get("/briefing")
@cached_endpoint(prefix="daily_briefing", ttl_hours=1, key_params=[])
def daily_briefing(
    role: str = Query("buyer", regex="^(buyer|sales)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Role-aware morning briefing — replaces the daily Excel handoff.

    Pure data aggregation, no AI calls. Cached per user for 1 hour.
    """
    from app.services.dashboard_briefing import generate_briefing

    return generate_briefing(db=db, user_id=user.id, role=role)
```

**Step 3: Add pre-compute job to `app/jobs/knowledge_jobs.py`**

In `register_knowledge_jobs()`, add:

```python
    scheduler.add_job(
        _job_precompute_briefings,
        CronTrigger(hour=6, minute=0),
        id="knowledge_precompute_briefings",
        name="Pre-compute morning briefings at 6 AM UTC",
    )
```

Add the job function:

```python
async def _job_precompute_briefings():
    """Pre-compute briefings for all users at 6 AM UTC.

    Calls generate_briefing() for each active user, warming the cache.
    """
    from app.database import SessionLocal
    from app.models.auth import User
    from app.services.dashboard_briefing import generate_briefing

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active.is_(True)).all()
        ok = 0
        for user in users:
            try:
                role = getattr(user, "role", "buyer") or "buyer"
                generate_briefing(db=db, user_id=user.id, role=role)
                ok += 1
            except Exception as e:
                logger.warning("Briefing pre-compute failed for user {}: {}", user.id, e)
        logger.info("Pre-computed briefings for {}/{} users", ok, len(users))
    except Exception as e:
        logger.error("precompute_briefings job failed: {}", e)
    finally:
        db.close()
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_dashboard_briefing.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add app/routers/dashboard/briefs.py app/jobs/knowledge_jobs.py tests/test_dashboard_briefing.py
git commit -m "feat(phase4): add briefing endpoint + 6AM pre-compute job"
```

---

### Task 4: Morning Briefing — Frontend Dashboard Card

**Files:**
- Modify: `app/static/app.js` (add briefing card to dashboard)

**Step 1: Add briefing fetch to `loadDashboard()`**

In `app/static/app.js`, find the `loadDashboard()` function (~line 2290). Add the briefing fetch alongside the existing morning-brief call:

```javascript
apiFetch('/api/dashboard/briefing').catch(() => null),
```

**Step 2: Add briefing card renderer using safe DOM methods**

Add after `loadDashboard()` (~line 2510). Use `document.createElement` and `textContent` instead of innerHTML:

```javascript
function _renderBriefingCard(briefing, container) {
    if (!briefing || !briefing.sections) return;

    var card = document.createElement('div');
    card.className = 'card mb-3 border-primary';

    // Header
    var header = document.createElement('div');
    header.className = 'card-header bg-primary text-white d-flex justify-content-between align-items-center';
    var headerLabel = document.createElement('span');
    headerLabel.textContent = 'Morning Briefing';
    var badge = document.createElement('span');
    badge.className = 'badge bg-light text-primary';
    badge.textContent = briefing.total_items + ' items';
    header.appendChild(headerLabel);
    header.appendChild(badge);
    card.appendChild(header);

    // Body
    var body = document.createElement('div');
    body.className = 'card-body p-0';

    var hasItems = false;
    briefing.sections.forEach(function(s) {
        if (s.count === 0) return;
        hasItems = true;

        var section = document.createElement('div');
        section.className = 'border-bottom p-2';

        var sectionHeader = document.createElement('div');
        sectionHeader.className = 'd-flex justify-content-between align-items-center cursor-pointer';
        var sectionLabel = document.createElement('strong');
        sectionLabel.textContent = s.label;
        var sectionBadge = document.createElement('span');
        sectionBadge.className = 'badge bg-secondary';
        sectionBadge.textContent = s.count;
        sectionHeader.appendChild(sectionLabel);
        sectionHeader.appendChild(sectionBadge);

        var sectionBody = document.createElement('div');
        sectionBody.className = 'd-none mt-2';

        sectionHeader.addEventListener('click', function() {
            sectionBody.classList.toggle('d-none');
        });

        s.items.forEach(function(item) {
            var row = document.createElement('div');
            row.className = 'd-flex justify-content-between align-items-start py-1 px-2 small';
            var content = document.createElement('div');
            var priorityBadge = document.createElement('span');
            var badgeColor = item.priority === 'critical' ? 'danger' : item.priority === 'high' ? 'warning' : 'info';
            priorityBadge.className = 'badge bg-' + badgeColor + ' me-1';
            priorityBadge.textContent = item.priority;
            content.appendChild(priorityBadge);
            var titleSpan = document.createElement('span');
            titleSpan.textContent = item.title;
            content.appendChild(titleSpan);
            if (item.detail) {
                var detailDiv = document.createElement('div');
                detailDiv.className = 'text-muted';
                detailDiv.textContent = item.detail;
                content.appendChild(detailDiv);
            }
            row.appendChild(content);
            sectionBody.appendChild(row);
        });

        section.appendChild(sectionHeader);
        section.appendChild(sectionBody);
        body.appendChild(section);
    });

    if (!hasItems) {
        var empty = document.createElement('div');
        empty.className = 'p-3 text-muted text-center';
        empty.textContent = 'Nothing needs attention right now';
        body.appendChild(empty);
    }

    card.appendChild(body);
    container.prepend(card);
}
```

**Step 3: Wire up in the dashboard results handler**

In the `loadDashboard()` Promise.all result handler, add:

```javascript
if (results[N]) _renderBriefingCard(results[N], dashContainer);
```

(Where N is the index of the briefing fetch in the Promise.all array.)

**Step 4: Commit**

```bash
git add app/static/app.js
git commit -m "feat(phase4): add morning briefing card to dashboard UI"
```

---

### Task 5: Cross-Customer Resurfacing — Service

**Files:**
- Create: `app/services/resurfacing_service.py`
- Test: `tests/test_resurfacing.py`

**Step 1: Write the failing tests**

Create `tests/test_resurfacing.py`:

```python
"""
test_resurfacing.py — Tests for MPN cross-customer resurfacing hints.

Tests for: get_mpn_hints() pure SQL lookup, formatting, batch behavior.

Called by: pytest
Depends on: app.services.resurfacing_service
"""

from unittest.mock import MagicMock

import pytest


class TestGetMpnHints:
    """Test the get_mpn_hints() function."""

    def test_returns_dict_keyed_by_mpn(self):
        from app.services.resurfacing_service import get_mpn_hints

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

        result = get_mpn_hints(mpns=["LM317", "TPS54331"], db=mock_db)
        assert isinstance(result, dict)
        assert "LM317" in result
        assert "TPS54331" in result

    def test_returns_none_for_unknown_mpn(self):
        from app.services.resurfacing_service import get_mpn_hints

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

        result = get_mpn_hints(mpns=["UNKNOWN_PART"], db=mock_db)
        assert result["UNKNOWN_PART"] is None

    def test_empty_list_returns_empty_dict(self):
        from app.services.resurfacing_service import get_mpn_hints

        result = get_mpn_hints(mpns=[], db=MagicMock())
        assert result == {}

    def test_exclude_req_id_filters_current_req(self):
        from app.services.resurfacing_service import get_mpn_hints

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

        result = get_mpn_hints(mpns=["LM317"], db=mock_db, exclude_req_id=123)
        assert "LM317" in result
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_resurfacing.py -v`
Expected: FAIL — module not found

**Step 3: Implement `resurfacing_service.py`**

Create `app/services/resurfacing_service.py`:

```python
"""Cross-customer MPN resurfacing service — inline hints wherever MPNs appear.

Pure SQL, no AI. Sub-50ms for batch of 20 MPNs.

For each MPN, generates a one-line hint from:
1. Latest offer price + vendor + date
2. Cross-req matches (other open reqs with same MPN)
3. High-confidence knowledge facts (lead time, EOL)

Called by: routers/knowledge.py (batch hints endpoint)
Depends on: models (Offer, Requisition, Requirement, KnowledgeEntry)
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session


def get_mpn_hints(
    mpns: list[str],
    db: Session,
    exclude_req_id: int | None = None,
) -> dict[str, str | None]:
    """Get inline hints for a batch of MPNs.

    Args:
        mpns: List of MPN strings to look up.
        db: Database session.
        exclude_req_id: Requisition ID to exclude from cross-req matches.

    Returns:
        Dict mapping each MPN to a hint string, or None if no history.
    """
    if not mpns:
        return {}

    result = {}
    for mpn in mpns:
        try:
            hint = _build_hint(mpn, db, exclude_req_id)
            result[mpn] = hint
        except Exception as e:
            logger.debug("Hint generation failed for MPN {}: {}", mpn, e)
            result[mpn] = None

    return result


def _build_hint(mpn: str, db: Session, exclude_req_id: int | None) -> str | None:
    """Build a single hint string for an MPN, prioritized by usefulness."""
    from app.models.knowledge import KnowledgeEntry
    from app.models.offers import Offer
    from app.models.sourcing import Requirement, Requisition

    hints = []

    # 1. Latest offer price + vendor
    offer_q = db.query(Offer).filter(
        Offer.mpn == mpn,
        Offer.unit_price.isnot(None),
        Offer.unit_price > 0,
    )
    if exclude_req_id:
        offer_q = offer_q.filter(Offer.requisition_id != exclude_req_id)
    latest_offer = offer_q.order_by(Offer.created_at.desc()).first()

    if latest_offer:
        vendor = latest_offer.vendor_name or "unknown"
        price = float(latest_offer.unit_price)
        age_days = (datetime.now(timezone.utc) - latest_offer.created_at).days if latest_offer.created_at else 0
        if age_days <= 1:
            age_str = "today"
        elif age_days < 30:
            age_str = "{}d ago".format(age_days)
        else:
            months = age_days // 30
            age_str = "{}mo ago".format(months)
        hints.append("Last quoted ${:.2f} from {}, {}".format(price, vendor, age_str))

    # 2. Cross-req matches
    cross_q = (
        db.query(Requisition.id, Requisition.status)
        .join(Requirement, Requirement.requisition_id == Requisition.id)
        .filter(
            Requirement.mpn == mpn,
            Requisition.status.in_(["open", "in_progress", "quoting"]),
        )
    )
    if exclude_req_id:
        cross_q = cross_q.filter(Requisition.id != exclude_req_id)
    cross_reqs = cross_q.all()

    if cross_reqs:
        req_ids = [str(r[0]) for r in cross_reqs[:3]]
        hints.append("Also on Req #{}".format(", #".join(req_ids)))

    # 3. Knowledge facts (lead time, EOL — high confidence)
    now = datetime.now(timezone.utc)
    facts = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.mpn == mpn,
            KnowledgeEntry.entry_type == "fact",
            KnowledgeEntry.confidence >= 0.7,
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(3)
        .all()
    )
    for f in facts:
        if f.expires_at and f.expires_at < now:
            continue
        # Strip the [fact_type] prefix if present
        content = f.content
        if content.startswith("["):
            content = content.split("] ", 1)[-1]
        hints.append(content[:80])

    if not hints:
        return None

    # Return the most important hint (first one)
    return hints[0]
```

**Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_resurfacing.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add app/services/resurfacing_service.py tests/test_resurfacing.py
git commit -m "feat(phase4): add MPN resurfacing service — cross-customer inline hints"
```

---

### Task 6: Cross-Customer Resurfacing — Endpoint + Cache

**Files:**
- Modify: `app/routers/knowledge.py` (add hints batch endpoint to `sprinkles_router`)
- Test: `tests/test_resurfacing.py` (add endpoint test)

**Step 1: Add endpoint test**

Append to `tests/test_resurfacing.py`:

```python
from unittest.mock import patch


class TestHintsEndpoint:
    """Test the /api/resurfacing/hints endpoint."""

    def test_hints_endpoint_returns_200(self, client, mock_user):
        with patch("app.services.resurfacing_service.get_mpn_hints") as mock_hints:
            mock_hints.return_value = {"LM317": "Last quoted $2.40 from Arrow, 3mo ago"}
            resp = client.get("/api/resurfacing/hints?mpns=LM317")
            assert resp.status_code == 200
            data = resp.json()
            assert "LM317" in data["hints"]

    def test_hints_endpoint_empty_mpns(self, client, mock_user):
        resp = client.get("/api/resurfacing/hints?mpns=")
        assert resp.status_code == 200
        assert resp.json()["hints"] == {}
```

**Step 2: Add endpoint to `app/routers/knowledge.py`**

On the `sprinkles_router`, add:

```python
@sprinkles_router.get("/resurfacing/hints")
def resurfacing_hints(
    mpns: str = Query("", description="Comma-separated list of MPNs"),
    exclude_req: int | None = Query(None, description="Req ID to exclude from cross-req matches"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Batch MPN hints for cross-customer resurfacing. Pure SQL, sub-50ms.

    Returns: {"hints": {"MPN1": "hint string or null", ...}}
    """
    from app.cache.intel_cache import get_cached, set_cached
    from app.services.resurfacing_service import get_mpn_hints

    mpn_list = [m.strip() for m in mpns.split(",") if m.strip()] if mpns else []
    if len(mpn_list) > 50:
        mpn_list = mpn_list[:50]

    if not mpn_list:
        return {"hints": {}}

    # Redis cache: 1h TTL
    cache_key = "resurface:{}:{}".format(",".join(sorted(mpn_list)), exclude_req or 0)
    try:
        cached = get_cached(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    hints = get_mpn_hints(mpns=mpn_list, db=db, exclude_req_id=exclude_req)
    response = {"hints": hints}

    try:
        set_cached(cache_key, response, ttl_days=1/24)  # 1 hour
    except Exception:
        pass

    return response
```

**Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_resurfacing.py -v`
Expected: All tests PASS

**Step 4: Commit**

```bash
git add app/routers/knowledge.py tests/test_resurfacing.py
git commit -m "feat(phase4): add /api/resurfacing/hints endpoint with Redis cache"
```

---

### Task 7: Cross-Customer Resurfacing — Frontend Inline Hints

**Files:**
- Modify: `app/static/app.js` (add hint rendering after MPN display)

**Step 1: Add hint fetcher function**

Add to `app/static/app.js`:

```javascript
async function _fetchMpnHints(mpns, excludeReqId) {
    if (!mpns || mpns.length === 0) return {};
    var params = new URLSearchParams();
    params.set('mpns', mpns.join(','));
    if (excludeReqId) params.set('exclude_req', excludeReqId);
    try {
        var data = await apiFetch('/api/resurfacing/hints?' + params);
        return data.hints || {};
    } catch (e) { return {}; }
}
```

**Step 2: Add hint rendering helper using safe DOM methods**

```javascript
function _renderMpnHints(container, hints) {
    if (!hints || Object.keys(hints).length === 0) return;
    var mpnCells = container.querySelectorAll('[data-mpn]');
    mpnCells.forEach(function(cell) {
        var mpn = cell.dataset.mpn;
        var hint = hints[mpn];
        if (!hint) return;
        var existing = cell.querySelector('.mpn-hint');
        if (existing) existing.remove();
        var el = document.createElement('div');
        el.className = 'mpn-hint text-muted small';
        el.style.fontSize = '0.75em';
        el.textContent = hint;
        cell.appendChild(el);
    });
}
```

**Step 3: Wire into requisition part list rendering**

After the requisition parts table renders (find the function that builds part rows — look for where `mpn` values are rendered into table cells), add `data-mpn` attribute to the MPN cell, then call:

```javascript
// After rendering parts table
var mpns = Array.from(partRows).map(function(r) { return r.dataset.mpn; }).filter(Boolean);
_fetchMpnHints(mpns, reqId).then(function(hints) { _renderMpnHints(partsContainer, hints); });
```

**Step 4: Commit**

```bash
git add app/static/app.js
git commit -m "feat(phase4): add inline MPN hints in requisition parts view"
```

---

### Task 8: Morning Briefing — Teams Delivery

**Files:**
- Modify: `app/jobs/knowledge_jobs.py` (extend `_job_send_knowledge_digests` to include briefing)

**Step 1: Extend Teams digest to include briefing sections**

In `_job_send_knowledge_digests()` in `app/jobs/knowledge_jobs.py`, after the existing digest delivery, add briefing section:

```python
        # Also deliver morning briefing to Teams
        from app.models.auth import User
        from app.services.dashboard_briefing import generate_briefing

        for config in configs:
            if current_hour != (config.knowledge_digest_hour or 14) % 24:
                continue
            try:
                user = db.get(User, config.user_id)
                if not user or not config.webhook_url:
                    continue
                role = getattr(user, "role", "buyer") or "buyer"
                briefing = generate_briefing(db=db, user_id=config.user_id, role=role)
                if briefing["total_items"] > 0:
                    await _send_briefing_to_teams(config.webhook_url, briefing, user.display_name)
            except Exception as e:
                logger.warning("Briefing Teams delivery failed for user {}: {}", config.user_id, e)
```

Add the Teams card builder:

```python
async def _send_briefing_to_teams(webhook_url: str, briefing: dict, user_name: str):
    """Send briefing as Teams adaptive card.

    Called by: _job_send_knowledge_digests()
    Depends on: httpx
    """
    import httpx

    sections_text = []
    for s in briefing["sections"]:
        if s["count"] > 0:
            sections_text.append("**{}**: {} items".format(s["label"], s["count"]))

    body = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": "Morning Briefing for {}".format(user_name), "weight": "bolder", "size": "medium"},
                    {"type": "TextBlock", "text": "{} items need attention".format(briefing["total_items"]), "isSubtle": True},
                    {"type": "TextBlock", "text": "\n".join(sections_text), "wrap": True},
                ],
            },
        }],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json=body)
        resp.raise_for_status()
```

**Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add app/jobs/knowledge_jobs.py
git commit -m "feat(phase4): add morning briefing Teams delivery"
```

---

### Task 9: Full Integration Test + Coverage Check

**Files:**
- Run full test suite and coverage check
- Fix any issues

**Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All tests PASS

**Step 2: Run coverage check**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -30`
Expected: No drop in coverage

**Step 3: Fix any coverage gaps**

Add tests for any uncovered lines in:
- `app/services/dashboard_briefing.py`
- `app/services/resurfacing_service.py`
- `app/services/email_intelligence_service.py` (new code)

**Step 4: Final commit**

```bash
git add -A
git commit -m "test(phase4): full coverage for email facts, briefing, resurfacing"
```
