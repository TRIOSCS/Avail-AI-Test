# AI Sprinkles Phase 3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add four pre-computed AI insight widgets (MPN sourcing history, vendor inline, pipeline health, company activity) across Material Card, Vendor Card, Dashboard, and Company drawer.

**Architecture:** Extend existing `knowledge_service.py` with 4 new context builders + 4 generators, add 8 convenience API endpoints via a new `sprinkles_router`, expand the 6h background job, and add frontend widgets reusing the `_renderInsightsCard` pattern.

**Tech Stack:** FastAPI, SQLAlchemy, Claude API (claude_structured), vanilla JS DOM manipulation

---

### Task 1: Context Builders

**Files:**
- Modify: `app/services/knowledge_service.py` (append after line 563)

**Step 1: Add `build_mpn_context(db, mpn)`**

Append to `knowledge_service.py` after `get_cached_insights`:

```python
# ---------------------------------------------------------------------------
# Phase 3: AI Sprinkles — Context Builders
# ---------------------------------------------------------------------------

def build_mpn_context(db: Session, *, mpn: str) -> str:
    """Gather all knowledge + offers + quotes for a specific MPN."""
    from app.models.offers import Offer
    from app.models.sourcing import Requirement

    now = datetime.now(timezone.utc)
    sections = []

    # Knowledge entries for this MPN
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.mpn == mpn, KnowledgeEntry.entry_type != "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(50)
        .all()
    )
    if entries:
        lines = []
        for e in entries:
            prefix = "[OUTDATED] " if e.expires_at and e.expires_at < now else ""
            lines.append("- {}{}: {} (source: {}, {})".format(
                prefix, e.entry_type, e.content, e.source, e.created_at.strftime('%Y-%m-%d')
            ))
        sections.append("## Knowledge entries for MPN {}\n{}".format(mpn, "\n".join(lines)))

    # Offer history
    offers = (
        db.query(Offer)
        .filter(Offer.mpn == mpn)
        .order_by(Offer.created_at.desc())
        .limit(20)
        .all()
    )
    if offers:
        lines = []
        for o in offers:
            price_str = "${:.4f}".format(float(o.unit_price)) if o.unit_price else "N/A"
            vendor = o.vendor_name or "unknown"
            date = o.created_at.strftime('%Y-%m-%d') if o.created_at else "?"
            lines.append("- {} from {}, qty {}, {} ({})".format(
                price_str, vendor, o.quantity or "?", o.status or "?", date
            ))
        sections.append("## Offer history for {}\n{}".format(mpn, "\n".join(lines)))

    # Requisitions containing this MPN
    req_ids = [
        r.requisition_id for r in
        db.query(Requirement.requisition_id)
        .filter(Requirement.mpn == mpn)
        .distinct()
        .limit(20)
        .all()
    ]
    if req_ids:
        sections.append("## Appears in {} requisition(s): {}".format(
            len(req_ids), ", ".join("#{}".format(rid) for rid in req_ids)
        ))

    return "\n\n".join(sections) if sections else ""


def build_vendor_context(db: Session, *, vendor_card_id: int) -> str:
    """Gather vendor knowledge + offer history + response rate."""
    from app.models.offers import Offer
    from app.models.sourcing import Requisition
    from app.models.vendor import VendorCard

    now = datetime.now(timezone.utc)
    sections = []

    vendor = db.get(VendorCard, vendor_card_id)
    if not vendor:
        return ""

    sections.append("## Vendor: {} (ID: {})".format(vendor.name, vendor_card_id))

    # Knowledge entries
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.vendor_card_id == vendor_card_id, KnowledgeEntry.entry_type != "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(30)
        .all()
    )
    if entries:
        lines = []
        for e in entries:
            prefix = "[OUTDATED] " if e.expires_at and e.expires_at < now else ""
            lines.append("- {}{}: {} ({})".format(prefix, e.entry_type, e.content, e.created_at.strftime('%Y-%m-%d')))
        sections.append("## Knowledge\n{}".format("\n".join(lines)))

    # Recent offers
    offers = (
        db.query(Offer)
        .filter(Offer.vendor_card_id == vendor_card_id)
        .order_by(Offer.created_at.desc())
        .limit(30)
        .all()
    )
    if offers:
        lines = []
        for o in offers:
            price_str = "${:.4f}".format(float(o.unit_price)) if o.unit_price else "N/A"
            lines.append("- {} {} qty {} {} ({})".format(
                o.mpn or "?", price_str, o.quantity or "?", o.status or "?",
                o.created_at.strftime('%Y-%m-%d') if o.created_at else "?"
            ))
        sections.append("## Recent offers ({})\n{}".format(len(offers), "\n".join(lines)))

    # Response rate: count RFQs sent vs offers received
    from sqlalchemy import func
    rfq_count = (
        db.query(func.count(Requisition.id))
        .filter(Requisition.status.in_(["sent", "quoted", "closed"]))
        .scalar() or 0
    )
    offer_count = len(offers)
    if rfq_count:
        sections.append("## Stats\n- {} offers from {} total RFQs in system".format(offer_count, rfq_count))

    return "\n\n".join(sections) if sections else ""


def build_pipeline_context(db: Session) -> str:
    """Gather active reqs, quote coverage, deal ages, win/loss stats."""
    from app.models.sourcing import Requisition
    from sqlalchemy import func

    now = datetime.now(timezone.utc)
    sections = []

    # Active requisitions summary
    active_reqs = (
        db.query(Requisition)
        .filter(Requisition.status.in_(["open", "sent", "quoting"]))
        .order_by(Requisition.created_at.desc())
        .limit(50)
        .all()
    )
    if active_reqs:
        lines = []
        for r in active_reqs:
            age = (now - r.created_at).days if r.created_at else 0
            company_name = r.company.name if r.company else "No company"
            lines.append("- Req #{}: {} — status: {}, age: {}d, company: {}".format(
                r.id, r.name or "untitled", r.status, age, company_name
            ))
        sections.append("## Active pipeline ({} reqs)\n{}".format(len(active_reqs), "\n".join(lines)))

    # Win/loss in last 90 days
    cutoff_90 = now - timedelta(days=90)
    status_counts = (
        db.query(Requisition.status, func.count(Requisition.id))
        .filter(Requisition.updated_at >= cutoff_90)
        .group_by(Requisition.status)
        .all()
    )
    if status_counts:
        lines = ["- {}: {}".format(s, c) for s, c in status_counts]
        sections.append("## Status breakdown (last 90 days)\n{}".format("\n".join(lines)))

    # Stale deals (open > 14 days without update)
    stale_cutoff = now - timedelta(days=14)
    stale = (
        db.query(Requisition)
        .filter(
            Requisition.status.in_(["open", "sent"]),
            Requisition.updated_at < stale_cutoff,
        )
        .order_by(Requisition.updated_at.asc())
        .limit(10)
        .all()
    )
    if stale:
        lines = []
        for r in stale:
            days_stale = (now - r.updated_at).days if r.updated_at else 0
            lines.append("- Req #{}: {} — {} days since update".format(r.id, r.name or "untitled", days_stale))
        sections.append("## Stalling deals (no update in 14+ days)\n{}".format("\n".join(lines)))

    return "\n\n".join(sections) if sections else ""


def build_company_context(db: Session, *, company_id: int) -> str:
    """Gather company knowledge + activity + open reqs."""
    from app.models.crm import Company
    from app.models.sourcing import Requisition
    from sqlalchemy import func

    now = datetime.now(timezone.utc)
    sections = []

    company = db.get(Company, company_id)
    if not company:
        return ""

    sections.append("## Company: {} (ID: {})".format(company.name, company_id))

    # Knowledge entries
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.company_id == company_id, KnowledgeEntry.entry_type != "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(30)
        .all()
    )
    if entries:
        lines = []
        for e in entries:
            prefix = "[OUTDATED] " if e.expires_at and e.expires_at < now else ""
            lines.append("- {}{}: {} ({})".format(prefix, e.entry_type, e.content, e.created_at.strftime('%Y-%m-%d')))
        sections.append("## Knowledge\n{}".format("\n".join(lines)))

    # Open requisitions
    open_reqs = (
        db.query(Requisition)
        .filter(Requisition.company_id == company_id, Requisition.status.in_(["open", "sent", "quoting"]))
        .order_by(Requisition.created_at.desc())
        .limit(20)
        .all()
    )
    if open_reqs:
        lines = []
        for r in open_reqs:
            age = (now - r.created_at).days if r.created_at else 0
            lines.append("- Req #{}: {} — {}, {}d old".format(r.id, r.name or "untitled", r.status, age))
        sections.append("## Open deals ({})\n{}".format(len(open_reqs), "\n".join(lines)))

    # Recent activity (last 30 days)
    cutoff = now - timedelta(days=30)
    recent_reqs = (
        db.query(func.count(Requisition.id))
        .filter(Requisition.company_id == company_id, Requisition.updated_at >= cutoff)
        .scalar() or 0
    )
    sections.append("## Activity\n- {} requisitions updated in last 30 days".format(recent_reqs))

    return "\n\n".join(sections) if sections else ""
```

**Step 2: Verify no syntax errors**

Run: `cd /root/availai && python -c "from app.services.knowledge_service import build_mpn_context, build_vendor_context, build_pipeline_context, build_company_context; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "feat(phase3): add context builders for MPN, vendor, pipeline, company"
```

---

### Task 2: Insight Generators

**Files:**
- Modify: `app/services/knowledge_service.py` (append after context builders)

**Step 1: Add system prompts and generator functions**

Append after the context builders:

```python
# ---------------------------------------------------------------------------
# Phase 3: AI Sprinkles — Insight Generators
# ---------------------------------------------------------------------------

MPN_INSIGHT_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given history for a specific MPN (part number), generate 2-4 concise insights about:
- Pricing trends (increasing, stable, decreasing)
- Quote frequency and sourcing patterns
- Vendor diversity for this part
- Availability signals
Keep each insight to 1 sentence. Be specific with numbers and dates."""

VENDOR_INSIGHT_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given data about a specific vendor, generate 3-5 concise insights about:
- Response patterns and reliability
- Pricing competitiveness
- Part specialization areas
- Any red flags or standout strengths
Keep each insight to 1-2 sentences. Be specific with numbers."""

PIPELINE_INSIGHT_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given a snapshot of the active pipeline, generate 3-5 actionable insights about:
- Stalling deals that need attention
- Coverage gaps (parts without quotes)
- Win/loss trends
- Pipeline health overall
Keep each insight to 1-2 sentences. Be specific with req numbers and ages."""

COMPANY_INSIGHT_PROMPT = """You are a procurement intelligence analyst for an electronic component sourcing company.
Given data about a specific customer company, generate 3-5 concise insights about:
- Engagement trends (increasing, decreasing, stalled)
- Open deal status and age
- Response time patterns
- Relationship health indicators
Keep each insight to 1-2 sentences. Be specific with numbers and dates."""


async def generate_mpn_insights(db: Session, mpn: str) -> list[KnowledgeEntry]:
    """Generate AI insights for a specific MPN."""
    from app.utils.claude_client import claude_structured

    context = build_mpn_context(db, mpn=mpn)
    if not context:
        return []

    # Delete old MPN insights
    old = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.mpn == mpn, KnowledgeEntry.entry_type == "ai_insight",
                KnowledgeEntry.requisition_id.is_(None))
        .all()
    )
    for o in old:
        db.delete(o)
    db.flush()

    result = await claude_structured(
        prompt="Analyze this MPN sourcing history:\n\n{}".format(context),
        schema=INSIGHT_SCHEMA,
        system=MPN_INSIGHT_PROMPT,
        model_tier="smart",
        max_tokens=1024,
        thinking_budget=3000,
    )

    if not result or "insights" not in result:
        return []

    entries = []
    now = datetime.now(timezone.utc)
    for insight in result["insights"][:4]:
        entry = create_entry(
            db, user_id=0, entry_type="ai_insight", content=insight["content"],
            source="ai_generated", confidence=insight.get("confidence", 0.8),
            expires_at=now + timedelta(days=EXPIRY_AI_INSIGHT), mpn=mpn,
        )
        entries.append(entry)
    logger.info("Generated {} MPN insights for {}", len(entries), mpn)
    return entries


async def generate_vendor_insights(db: Session, vendor_card_id: int) -> list[KnowledgeEntry]:
    """Generate AI insights for a specific vendor."""
    from app.utils.claude_client import claude_structured

    context = build_vendor_context(db, vendor_card_id=vendor_card_id)
    if not context:
        return []

    old = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.vendor_card_id == vendor_card_id, KnowledgeEntry.entry_type == "ai_insight")
        .all()
    )
    for o in old:
        db.delete(o)
    db.flush()

    result = await claude_structured(
        prompt="Analyze this vendor data:\n\n{}".format(context),
        schema=INSIGHT_SCHEMA,
        system=VENDOR_INSIGHT_PROMPT,
        model_tier="smart",
        max_tokens=1024,
        thinking_budget=3000,
    )

    if not result or "insights" not in result:
        return []

    entries = []
    now = datetime.now(timezone.utc)
    for insight in result["insights"][:5]:
        entry = create_entry(
            db, user_id=0, entry_type="ai_insight", content=insight["content"],
            source="ai_generated", confidence=insight.get("confidence", 0.8),
            expires_at=now + timedelta(days=EXPIRY_AI_INSIGHT), vendor_card_id=vendor_card_id,
        )
        entries.append(entry)
    logger.info("Generated {} vendor insights for vendor {}", len(entries), vendor_card_id)
    return entries


async def generate_pipeline_insights(db: Session) -> list[KnowledgeEntry]:
    """Generate AI insights for pipeline-wide health."""
    from app.utils.claude_client import claude_structured

    context = build_pipeline_context(db)
    if not context:
        return []

    # Delete old pipeline insights (special marker mpn='__pipeline__')
    old = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.mpn == "__pipeline__", KnowledgeEntry.entry_type == "ai_insight")
        .all()
    )
    for o in old:
        db.delete(o)
    db.flush()

    result = await claude_structured(
        prompt="Analyze this pipeline snapshot:\n\n{}".format(context),
        schema=INSIGHT_SCHEMA,
        system=PIPELINE_INSIGHT_PROMPT,
        model_tier="smart",
        max_tokens=2048,
        thinking_budget=5000,
    )

    if not result or "insights" not in result:
        return []

    entries = []
    now = datetime.now(timezone.utc)
    for insight in result["insights"][:5]:
        entry = create_entry(
            db, user_id=0, entry_type="ai_insight", content=insight["content"],
            source="ai_generated", confidence=insight.get("confidence", 0.8),
            expires_at=now + timedelta(days=EXPIRY_AI_INSIGHT), mpn="__pipeline__",
        )
        entries.append(entry)
    logger.info("Generated {} pipeline insights", len(entries))
    return entries


async def generate_company_insights(db: Session, company_id: int) -> list[KnowledgeEntry]:
    """Generate AI insights for a specific company."""
    from app.utils.claude_client import claude_structured

    context = build_company_context(db, company_id=company_id)
    if not context:
        return []

    old = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.company_id == company_id, KnowledgeEntry.entry_type == "ai_insight")
        .all()
    )
    for o in old:
        db.delete(o)
    db.flush()

    result = await claude_structured(
        prompt="Analyze this company data:\n\n{}".format(context),
        schema=INSIGHT_SCHEMA,
        system=COMPANY_INSIGHT_PROMPT,
        model_tier="smart",
        max_tokens=1024,
        thinking_budget=3000,
    )

    if not result or "insights" not in result:
        return []

    entries = []
    now = datetime.now(timezone.utc)
    for insight in result["insights"][:5]:
        entry = create_entry(
            db, user_id=0, entry_type="ai_insight", content=insight["content"],
            source="ai_generated", confidence=insight.get("confidence", 0.8),
            expires_at=now + timedelta(days=EXPIRY_AI_INSIGHT), company_id=company_id,
        )
        entries.append(entry)
    logger.info("Generated {} company insights for company {}", len(entries), company_id)
    return entries


def get_cached_mpn_insights(db: Session, mpn: str) -> list[KnowledgeEntry]:
    """Return pre-computed AI insights for an MPN (cross-req, no requisition_id)."""
    return (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.mpn == mpn, KnowledgeEntry.entry_type == "ai_insight",
                KnowledgeEntry.requisition_id.is_(None))
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )


def get_cached_vendor_insights(db: Session, vendor_card_id: int) -> list[KnowledgeEntry]:
    """Return pre-computed AI insights for a vendor."""
    return (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.vendor_card_id == vendor_card_id, KnowledgeEntry.entry_type == "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )


def get_cached_pipeline_insights(db: Session) -> list[KnowledgeEntry]:
    """Return pre-computed pipeline health insights."""
    return (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.mpn == "__pipeline__", KnowledgeEntry.entry_type == "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )


def get_cached_company_insights(db: Session, company_id: int) -> list[KnowledgeEntry]:
    """Return pre-computed AI insights for a company."""
    return (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.company_id == company_id, KnowledgeEntry.entry_type == "ai_insight")
        .order_by(KnowledgeEntry.created_at.desc())
        .all()
    )
```

**Step 2: Verify imports**

Run: `cd /root/availai && python -c "from app.services.knowledge_service import generate_mpn_insights, get_cached_pipeline_insights; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "feat(phase3): add insight generators for MPN, vendor, pipeline, company"
```

---

### Task 3: API Endpoints

**Files:**
- Modify: `app/routers/knowledge.py` (add sprinkles_router after insights_router)
- Modify: `app/main.py` (register sprinkles_router)

**Step 1: Add sprinkles_router to knowledge.py**

Append after the `refresh_insights` endpoint (line ~255):

```python
# --- Phase 3: AI Sprinkles endpoints ---

sprinkles_router = APIRouter(prefix="/api", tags=["ai-sprinkles"])


@sprinkles_router.get("/vendors/{vendor_id}/insights")
def get_vendor_insights(
    vendor_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_vendor_insights(db, vendor_id)
    now = datetime.now(timezone.utc)
    return {
        "vendor_card_id": vendor_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/vendors/{vendor_id}/insights/refresh")
async def refresh_vendor_insights(
    vendor_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_vendor_insights(db, vendor_id)
    now = datetime.now(timezone.utc)
    return {
        "vendor_card_id": vendor_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }


@sprinkles_router.get("/companies/{company_id}/insights")
def get_company_insights(
    company_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_company_insights(db, company_id)
    now = datetime.now(timezone.utc)
    return {
        "company_id": company_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/companies/{company_id}/insights/refresh")
async def refresh_company_insights(
    company_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_company_insights(db, company_id)
    now = datetime.now(timezone.utc)
    return {
        "company_id": company_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }


@sprinkles_router.get("/dashboard/pipeline-insights")
def get_pipeline_insights(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_pipeline_insights(db)
    now = datetime.now(timezone.utc)
    return {
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/dashboard/pipeline-insights/refresh")
async def refresh_pipeline_insights(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_pipeline_insights(db)
    now = datetime.now(timezone.utc)
    return {
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }


@sprinkles_router.get("/materials/insights")
def get_mpn_insights(
    mpn: str = Query(...),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_mpn_insights(db, mpn)
    now = datetime.now(timezone.utc)
    return {
        "mpn": mpn,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/materials/insights/refresh")
async def refresh_mpn_insights(
    mpn: str = Query(...),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_mpn_insights(db, mpn)
    now = datetime.now(timezone.utc)
    return {
        "mpn": mpn,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }
```

**Step 2: Register in main.py**

Find the line (around 1024):
```python
from .routers.knowledge import insights_router as knowledge_insights_router
```

Change it to:
```python
from .routers.knowledge import insights_router as knowledge_insights_router
from .routers.knowledge import sprinkles_router as sprinkles_router
```

And after `app.include_router(knowledge_insights_router)`, add:
```python
app.include_router(sprinkles_router)
```

**Step 3: Verify**

Run: `cd /root/availai && python -c "from app.routers.knowledge import sprinkles_router; print('routes:', len(sprinkles_router.routes))"`
Expected: `routes: 8`

**Step 4: Commit**

```bash
git add app/routers/knowledge.py app/main.py
git commit -m "feat(phase3): add 8 sprinkle API endpoints for vendor/company/pipeline/MPN insights"
```

---

### Task 4: Expand Background Job

**Files:**
- Modify: `app/jobs/knowledge_jobs.py` (expand `_job_refresh_insights`)

**Step 1: Add Phase 3 entity refresh to the job**

Replace the `_job_refresh_insights` function (lines 45-73) with:

```python
async def _job_refresh_insights():
    """Re-generate insights for active reqs, vendors, companies, MPNs, and pipeline."""
    from app.database import SessionLocal
    from app.models.sourcing import Requisition
    from app.services import knowledge_service

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        # 1. Requisition insights (existing — top 50 recently active)
        active_reqs = (
            db.query(Requisition.id)
            .filter(Requisition.updated_at >= cutoff)
            .order_by(Requisition.updated_at.desc())
            .limit(50)
            .all()
        )
        req_count = 0
        for (req_id,) in active_reqs:
            try:
                entries = await knowledge_service.generate_insights(db, req_id)
                if entries:
                    req_count += 1
            except Exception as e:
                logger.warning("Insight generation failed for req {}: {}", req_id, e)
        logger.info("Refreshed insights for {}/{} active reqs", req_count, len(active_reqs))

        # 2. Pipeline insights (1 per run)
        try:
            await knowledge_service.generate_pipeline_insights(db)
            logger.info("Refreshed pipeline insights")
        except Exception as e:
            logger.warning("Pipeline insight generation failed: {}", e)

        # 3. Top 20 most active vendors
        from app.models.offers import Offer
        from sqlalchemy import func
        top_vendors = (
            db.query(Offer.vendor_card_id, func.count(Offer.id).label("cnt"))
            .filter(Offer.vendor_card_id.isnot(None), Offer.created_at >= cutoff)
            .group_by(Offer.vendor_card_id)
            .order_by(func.count(Offer.id).desc())
            .limit(20)
            .all()
        )
        vendor_count = 0
        for vendor_id, _ in top_vendors:
            try:
                entries = await knowledge_service.generate_vendor_insights(db, vendor_id)
                if entries:
                    vendor_count += 1
            except Exception as e:
                logger.warning("Vendor insight generation failed for {}: {}", vendor_id, e)
        logger.info("Refreshed insights for {}/{} vendors", vendor_count, len(top_vendors))

        # 4. Top 20 most active companies
        from app.models.sourcing import Requisition as Req2
        top_companies = (
            db.query(Req2.company_id, func.count(Req2.id).label("cnt"))
            .filter(Req2.company_id.isnot(None), Req2.updated_at >= cutoff)
            .group_by(Req2.company_id)
            .order_by(func.count(Req2.id).desc())
            .limit(20)
            .all()
        )
        company_count = 0
        for company_id, _ in top_companies:
            try:
                entries = await knowledge_service.generate_company_insights(db, company_id)
                if entries:
                    company_count += 1
            except Exception as e:
                logger.warning("Company insight generation failed for {}: {}", company_id, e)
        logger.info("Refreshed insights for {}/{} companies", company_count, len(top_companies))

        # 5. Top 50 most-quoted MPNs
        from app.models.offers import Offer as Offer2
        top_mpns = (
            db.query(Offer2.mpn, func.count(Offer2.id).label("cnt"))
            .filter(Offer2.mpn.isnot(None), Offer2.mpn != "")
            .group_by(Offer2.mpn)
            .order_by(func.count(Offer2.id).desc())
            .limit(50)
            .all()
        )
        mpn_count = 0
        for mpn, _ in top_mpns:
            try:
                entries = await knowledge_service.generate_mpn_insights(db, mpn)
                if entries:
                    mpn_count += 1
            except Exception as e:
                logger.warning("MPN insight generation failed for {}: {}", mpn, e)
        logger.info("Refreshed insights for {}/{} MPNs", mpn_count, len(top_mpns))

    except Exception as e:
        logger.error("refresh_active_insights job failed: {}", e)
    finally:
        db.close()
```

**Step 2: Verify syntax**

Run: `cd /root/availai && python -c "from app.jobs.knowledge_jobs import _job_refresh_insights; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/jobs/knowledge_jobs.py
git commit -m "feat(phase3): expand insight refresh job to cover vendors, companies, MPNs, pipeline"
```

---

### Task 5: Frontend — Vendor Card Insights

**Files:**
- Modify: `app/static/app.js` (add insights card to `openVendorPopup`)

**Step 1: Add a reusable `_renderEntityInsightsCard` function**

Find the end of the `_renderInsightsCard` function (around line 3625) and add after it:

```javascript
async function _renderEntityInsightsCard(entityType, entityId, container, opts) {
    // entityType: 'vendors', 'companies', 'materials', 'dashboard'
    // opts: { title, queryParam } — queryParam for MPN (e.g. '?mpn=X')
    var title = (opts && opts.title) || 'AI Insights';
    var queryParam = (opts && opts.queryParam) || '';
    var storageKey = 'sprinkle_collapsed_' + entityType;
    var collapsed = localStorage.getItem(storageKey) === '1';

    var wrap = document.createElement('div');
    wrap.className = 'insights-card';
    wrap.id = 'sprinkle-' + entityType + '-' + entityId;

    var hdr = document.createElement('div');
    hdr.className = 'insights-header';
    hdr.onclick = function() {
        var b = wrap.querySelector('.insights-body');
        var t = wrap.querySelector('.insights-toggle');
        if (b.style.display === 'none') {
            b.style.display = '';
            t.textContent = '\u25bc';
            localStorage.removeItem(storageKey);
        } else {
            b.style.display = 'none';
            t.textContent = '\u25b6';
            localStorage.setItem(storageKey, '1');
        }
    };

    var titleSpan = document.createElement('span');
    titleSpan.style.cssText = 'font-weight:600;font-size:12px';
    titleSpan.textContent = title;
    hdr.appendChild(titleSpan);

    var controls = document.createElement('span');
    controls.style.cssText = 'display:flex;gap:6px;align-items:center';

    var refreshBtn = document.createElement('button');
    refreshBtn.className = 'btn btn-ghost btn-sm';
    refreshBtn.style.fontSize = '10px';
    refreshBtn.textContent = '\u21bb Refresh';
    refreshBtn.onclick = function(e) {
        e.stopPropagation();
        _refreshEntityInsights(entityType, entityId, queryParam);
    };
    controls.appendChild(refreshBtn);

    var toggle = document.createElement('span');
    toggle.className = 'insights-toggle';
    toggle.textContent = collapsed ? '\u25b6' : '\u25bc';
    controls.appendChild(toggle);
    hdr.appendChild(controls);
    wrap.appendChild(hdr);

    var body = document.createElement('div');
    body.className = 'insights-body';
    body.style.display = collapsed ? 'none' : '';
    var loading = document.createElement('span');
    loading.style.cssText = 'font-size:11px;color:var(--muted)';
    loading.textContent = 'Loading\u2026';
    body.appendChild(loading);
    wrap.appendChild(body);

    container.prepend(wrap);

    // Build URL
    var url;
    if (entityType === 'dashboard') {
        url = '/api/dashboard/pipeline-insights';
    } else if (entityType === 'materials') {
        url = '/api/materials/insights' + queryParam;
    } else {
        url = '/api/' + entityType + '/' + entityId + '/insights';
    }

    try {
        var data = await apiFetch(url);
        _populateInsightsBody(body, data);
    } catch (e) {
        body.textContent = '';
        var errSpan = document.createElement('span');
        errSpan.style.cssText = 'font-size:11px;color:var(--red)';
        errSpan.textContent = 'Failed to load insights';
        body.appendChild(errSpan);
    }
}

async function _refreshEntityInsights(entityType, entityId, queryParam) {
    var wrap = document.getElementById('sprinkle-' + entityType + '-' + entityId);
    if (!wrap) return;
    var body = wrap.querySelector('.insights-body');
    body.textContent = '';
    var loading = document.createElement('span');
    loading.style.cssText = 'font-size:11px;color:var(--muted)';
    loading.textContent = 'Regenerating\u2026';
    body.appendChild(loading);

    var url;
    if (entityType === 'dashboard') {
        url = '/api/dashboard/pipeline-insights/refresh';
    } else if (entityType === 'materials') {
        url = '/api/materials/insights/refresh' + (queryParam || '');
    } else {
        url = '/api/' + entityType + '/' + entityId + '/insights/refresh';
    }

    try {
        var data = await apiFetch(url, { method: 'POST' });
        _populateInsightsBody(body, data);
    } catch (e) {
        body.textContent = '';
        var errSpan = document.createElement('span');
        errSpan.style.cssText = 'font-size:11px;color:var(--red)';
        errSpan.textContent = 'Refresh failed';
        body.appendChild(errSpan);
    }
}

function _populateInsightsBody(body, data) {
    body.textContent = '';
    if (!data.insights || !data.insights.length) {
        var empty = document.createElement('span');
        empty.style.cssText = 'font-size:11px;color:var(--muted)';
        empty.textContent = 'No insights yet. Click Refresh to generate.';
        body.appendChild(empty);
        return;
    }
    for (var i = 0; i < data.insights.length; i++) {
        var ins = data.insights[i];
        var item = document.createElement('div');
        item.className = 'insight-item' + (ins.is_expired ? ' insight-expired' : '');
        var text = document.createElement('span');
        text.style.fontSize = '11px';
        text.textContent = ins.content;
        item.appendChild(text);
        if (ins.is_expired) {
            var badge = document.createElement('span');
            badge.style.cssText = 'font-size:9px;color:var(--amber);margin-left:4px';
            badge.textContent = '(may be outdated)';
            item.appendChild(badge);
        }
        body.appendChild(item);
    }
    if (data.has_expired) {
        var warn = document.createElement('div');
        warn.style.cssText = 'font-size:10px;color:var(--amber);margin-top:4px';
        warn.textContent = 'Some insights based on outdated data';
        body.appendChild(warn);
    }
}
```

**Step 2: Wire into `openVendorPopup`**

Find `openVendorPopup` (around line 10341). After the popup content div is created (look for the first `appendChild` into the popup body), add:

```javascript
_renderEntityInsightsCard('vendors', vendorId, popupBody, { title: 'Vendor Intelligence' });
```

Where `vendorId` is the vendor card ID and `popupBody` is the container div the popup renders into.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "feat(phase3): add reusable entity insights card + vendor card widget"
```

---

### Task 6: Frontend — Pipeline Health Dashboard Card

**Files:**
- Modify: `app/static/app.js` (add pipeline health card to `loadDashboard`)

**Step 1: Add pipeline health card to dashboard**

Find the `loadDashboard` function (around line 2290). After the existing dashboard cards are rendered, add:

```javascript
// Pipeline Health AI card
var pipelineWrap = document.createElement('div');
pipelineWrap.className = 'card';
pipelineWrap.style.marginTop = '12px';
var pipelineTitle = document.createElement('h3');
pipelineTitle.style.cssText = 'font-size:14px;margin:0 0 8px';
pipelineTitle.textContent = 'Pipeline Health';
pipelineWrap.appendChild(pipelineTitle);
dashboardContainer.appendChild(pipelineWrap);
_renderEntityInsightsCard('dashboard', 'pipeline', pipelineWrap, { title: 'Pipeline Health AI' });
```

Where `dashboardContainer` is the element the dashboard renders into.

**Step 2: Commit**

```bash
git add app/static/app.js
git commit -m "feat(phase3): add pipeline health AI card to dashboard"
```

---

### Task 7: Frontend — MPN Sourcing History Badge

**Files:**
- Modify: `app/static/app.js` (add badge to `openMaterialPopup`)

**Step 1: Add sourcing history to Material Card popup**

Find `openMaterialPopup` (around line 11671). After the MPN heading is rendered, add:

```javascript
// Sourcing history badge
var mpnVal = card.mpn || '';
if (mpnVal) {
    var histBadge = document.createElement('div');
    histBadge.style.cssText = 'margin:4px 0;font-size:11px;color:var(--muted)';
    histBadge.textContent = 'Loading sourcing history\u2026';
    mpnHeading.parentNode.insertBefore(histBadge, mpnHeading.nextSibling);

    apiFetch('/api/materials/insights?mpn=' + encodeURIComponent(mpnVal)).then(function(data) {
        if (data.insights && data.insights.length) {
            histBadge.textContent = data.insights[0].content;
            histBadge.style.color = 'var(--primary)';
        } else {
            histBadge.textContent = 'No sourcing history';
        }
    }).catch(function() {
        histBadge.textContent = '';
    });
}
```

Where `mpnHeading` is the element that shows the MPN title and `card.mpn` is the MPN value.

**Step 2: Commit**

```bash
git add app/static/app.js
git commit -m "feat(phase3): add MPN sourcing history badge to Material Card"
```

---

### Task 8: Frontend — Company Insights in CRM Drawer

**Files:**
- Modify: `app/static/crm.js` (add insights card to company drawer Overview tab)

**Step 1: Add company insights card**

Find `_renderCustDrawerOverview` (around line 864). The existing "AI Account Intelligence" section is around line 906. After that section, add:

```javascript
// Knowledge-based company insights (Phase 3)
if (typeof _renderEntityInsightsCard === 'function') {
    _renderEntityInsightsCard('companies', companyId, overviewContainer, { title: 'AI Knowledge Insights' });
}
```

Where `companyId` is the company ID and `overviewContainer` is the overview tab container.

Note: `_renderEntityInsightsCard` is defined in `app.js` which loads before `crm.js`, so it should be available. The `typeof` guard is defensive.

**Step 2: Commit**

```bash
git add app/static/crm.js
git commit -m "feat(phase3): add company knowledge insights to CRM drawer"
```

---

### Task 9: Build, Deploy, Verify

**Step 1: Build and deploy**

```bash
cd /root/availai
docker compose up -d --build
```

**Step 2: Check logs**

```bash
docker compose logs -f app 2>&1 | head -60
```

Expected: Clean startup, no import errors.

**Step 3: Verify endpoints**

```bash
# Pipeline insights
curl -s http://localhost:8000/api/dashboard/pipeline-insights | python3 -m json.tool | head -5

# Vendor insights (use any vendor ID)
curl -s http://localhost:8000/api/vendors/1/insights | python3 -m json.tool | head -5

# MPN insights
curl -s "http://localhost:8000/api/materials/insights?mpn=TEST" | python3 -m json.tool | head -5

# Company insights
curl -s http://localhost:8000/api/companies/1/insights | python3 -m json.tool | head -5
```

**Step 4: Commit final**

```bash
git add -A
git commit -m "feat(phase3): AI Sprinkles — all 4 entity insight widgets complete"
git push
```
