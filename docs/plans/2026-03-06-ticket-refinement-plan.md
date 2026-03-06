# Trouble Ticket Refinement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refine the existing trouble ticket system with 6 improvements: Vinod admin user, AI thread consolidation, report templates, disable auto-close, better AI prompts, and exhaustive "Find Trouble" automated testing.

**Architecture:** Extends existing `TroubleTicket` model with new columns. Adds a consolidation service (Haiku similarity). Adds Playwright-based site sweep as server subprocess. Frontend gets templates and "Find Trouble" button.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Playwright (new dep), Claude Haiku (similarity), vanilla JS frontend.

---

### Task 1: Seed Vinod Admin User

**Files:**
- Modify: `app/startup.py:18-47`

**Step 1: Write the failing test**

File: `tests/test_vinod_seed.py`

```python
"""Test Vinod admin user seeding."""
import pytest
from app.startup import _seed_vinod_user
from app.models.auth import User


def test_seed_vinod_creates_user(db_session):
    _seed_vinod_user(db_session)
    user = db_session.query(User).filter_by(email="vinod@trioscs.com").first()
    assert user is not None
    assert user.role == "admin"
    assert user.name == "Vinod"


def test_seed_vinod_idempotent(db_session):
    _seed_vinod_user(db_session)
    _seed_vinod_user(db_session)
    count = db_session.query(User).filter_by(email="vinod@trioscs.com").count()
    assert count == 1
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_vinod_seed.py -v`
Expected: FAIL — `_seed_vinod_user` doesn't exist yet

**Step 3: Write minimal implementation**

Add to `app/startup.py` after the existing `_create_default_user_if_env_set` function:

```python
def _seed_vinod_user(db=None) -> None:
    """Seed Vinod's admin user account. Idempotent — skips if exists."""
    from .models.auth import User

    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        existing = db.query(User).filter_by(email="vinod@trioscs.com").first()
        if existing:
            return
        user = User(email="vinod@trioscs.com", name="Vinod", role="admin")
        db.add(user)
        db.commit()
        logger.info("Seeded admin user: vinod@trioscs.com")
    except Exception:
        logger.exception("Failed to seed Vinod user")
    finally:
        if close_db:
            db.close()
```

Call `_seed_vinod_user()` at the end of `run_startup_migrations()`.

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_vinod_seed.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/startup.py tests/test_vinod_seed.py
git commit -m "feat: seed Vinod admin user on startup (idempotent)"
```

---

### Task 2: Alembic Migration — New Columns

**Files:**
- Modify: `app/models/trouble_ticket.py`
- Create: `alembic/versions/051_ticket_refinement_columns.py` (via autogenerate)

**Step 1: Add columns to model**

Add to `TroubleTicket` class in `app/models/trouble_ticket.py`:

```python
# Thread consolidation
similarity_score = Column(Float, nullable=True)

# Agent testing context
tested_area = Column(String(50), nullable=True)
dom_snapshot = Column(Text, nullable=True)
network_errors = Column(JSON, nullable=True)
performance_timings = Column(JSON, nullable=True)
reproduction_steps = Column(JSON, nullable=True)
```

Add `Float` to the existing SQLAlchemy import if not already there.

**Step 2: Generate migration**

Run: `cd /root/availai && docker compose exec -T app alembic revision --autogenerate -m "ticket refinement columns"`

If running outside Docker:
Run: `cd /root/availai && PYTHONPATH=/root/availai alembic revision --autogenerate -m "ticket refinement columns"`

**Step 3: Review the generated migration** — verify it only adds the 6 new columns, nothing else.

**Step 4: Test migration round-trip**

Run: `docker compose exec -T app alembic upgrade head && docker compose exec -T app alembic downgrade -1 && docker compose exec -T app alembic upgrade head`

**Step 5: Commit**

```bash
git add app/models/trouble_ticket.py alembic/versions/
git commit -m "feat: add ticket refinement columns (similarity, agent context)"
```

---

### Task 3: AI Thread Consolidation Service

**Files:**
- Create: `app/services/ticket_consolidation.py`
- Test: `tests/test_ticket_consolidation.py`

**Step 1: Write the failing tests**

File: `tests/test_ticket_consolidation.py`

```python
"""Test AI ticket consolidation — similarity detection and linking."""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.ticket_consolidation import find_similar_ticket, consolidate_ticket
from app.models.trouble_ticket import TroubleTicket


@pytest.fixture
def parent_ticket(db_session):
    t = TroubleTicket(
        ticket_number="TT-20260306-001",
        submitted_by=1,
        title="Search returns no results for LM358",
        description="Searched for LM358 and got empty results page",
        status="submitted",
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture
def similar_ticket(db_session, parent_ticket):
    t = TroubleTicket(
        ticket_number="TT-20260306-002",
        submitted_by=2,
        title="Search is broken, nothing comes back",
        description="No results when I search for any part number",
        status="submitted",
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture
def unrelated_ticket(db_session, parent_ticket):
    t = TroubleTicket(
        ticket_number="TT-20260306-003",
        submitted_by=2,
        title="Dashboard teal color looks off",
        description="The header color changed after last update",
        status="submitted",
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.mark.asyncio
async def test_find_similar_high_confidence(db_session, parent_ticket, similar_ticket):
    """When AI returns >0.9 confidence, should return the parent ticket ID."""
    mock_response = {"match_id": parent_ticket.id, "confidence": 0.95}
    with patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock, return_value=mock_response):
        result = await find_similar_ticket(similar_ticket, db_session)
    assert result is not None
    assert result["match_id"] == parent_ticket.id
    assert result["confidence"] == 0.95


@pytest.mark.asyncio
async def test_find_similar_low_confidence_returns_none(db_session, parent_ticket, unrelated_ticket):
    """When AI returns <0.9 confidence, should return None (no link)."""
    mock_response = {"match_id": None, "confidence": 0.3}
    with patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock, return_value=mock_response):
        result = await find_similar_ticket(unrelated_ticket, db_session)
    assert result is None


@pytest.mark.asyncio
async def test_find_similar_no_open_tickets(db_session):
    """With no other open tickets, should return None."""
    ticket = TroubleTicket(
        ticket_number="TT-20260306-010",
        submitted_by=1,
        title="Something broke",
        description="Details here",
        status="submitted",
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    result = await find_similar_ticket(ticket, db_session)
    assert result is None


@pytest.mark.asyncio
async def test_consolidate_links_ticket(db_session, parent_ticket, similar_ticket):
    """consolidate_ticket should set parent_ticket_id and similarity_score."""
    mock_response = {"match_id": parent_ticket.id, "confidence": 0.95}
    with patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock, return_value=mock_response):
        await consolidate_ticket(similar_ticket.id, db_session)
    db_session.refresh(similar_ticket)
    assert similar_ticket.parent_ticket_id == parent_ticket.id
    assert similar_ticket.similarity_score == 0.95


@pytest.mark.asyncio
async def test_consolidate_ai_failure_does_not_crash(db_session, parent_ticket, similar_ticket):
    """If AI call fails, ticket stays unlinked — no crash."""
    with patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock, side_effect=Exception("API down")):
        await consolidate_ticket(similar_ticket.id, db_session)
    db_session.refresh(similar_ticket)
    assert similar_ticket.parent_ticket_id is None


@pytest.mark.asyncio
async def test_consolidate_skips_already_linked(db_session, parent_ticket, similar_ticket):
    """Already-linked tickets should be skipped."""
    similar_ticket.parent_ticket_id = parent_ticket.id
    db_session.commit()
    with patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock) as mock_ai:
        await consolidate_ticket(similar_ticket.id, db_session)
    mock_ai.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ticket_consolidation.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Write implementation**

File: `app/services/ticket_consolidation.py`

```python
"""Ticket consolidation service — AI-powered duplicate detection and linking.

Uses Claude Haiku to compare new tickets against open tickets.
Links duplicates via parent_ticket_id when confidence > 0.9.

Called by: routers/trouble_tickets.py (on submit), jobs/selfheal_jobs.py (daily batch)
Depends on: utils/claude_client.py, models/trouble_ticket.py
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.trouble_ticket import TroubleTicket
from app.utils.claude_client import claude_structured

SIMILARITY_THRESHOLD = 0.9

SIMILARITY_SCHEMA = {
    "type": "object",
    "properties": {
        "match_id": {
            "type": ["integer", "null"],
            "description": "ID of the matching ticket, or null if no match",
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0 confidence that this is the same issue",
        },
    },
    "required": ["match_id", "confidence"],
}

SIMILARITY_SYSTEM = """You are a bug triage specialist. Compare a new bug report against existing open tickets.
Determine if the new report describes the SAME underlying issue as any existing ticket.

Rules:
- Same root cause = match (even if described differently)
- Similar symptoms but different causes = no match
- Be conservative: only match when you are very confident (>0.9)
- Return match_id=null if no match found
- Return the ID of the BEST matching ticket if multiple could match"""


async def find_similar_ticket(
    ticket: TroubleTicket,
    db: Session,
) -> dict | None:
    """Check if a ticket matches any open ticket. Returns {match_id, confidence} or None."""
    # Get open tickets excluding this one and already-resolved
    open_tickets = (
        db.query(TroubleTicket)
        .filter(
            TroubleTicket.id != ticket.id,
            TroubleTicket.status.in_(["submitted", "diagnosed", "escalated", "in_progress", "open"]),
        )
        .order_by(TroubleTicket.created_at.desc())
        .limit(50)
        .all()
    )

    if not open_tickets:
        return None

    existing_list = "\n".join(
        f"- ID {t.id}: {t.title} ({t.description[:100] if t.description else 'no description'})"
        for t in open_tickets
    )

    prompt = f"""New ticket:
Title: {ticket.title}
Description: {ticket.description or 'No description'}

Existing open tickets:
{existing_list}

Is the new ticket about the same underlying issue as any existing ticket?"""

    try:
        result = await claude_structured(
            prompt=prompt,
            schema=SIMILARITY_SCHEMA,
            system=SIMILARITY_SYSTEM,
            model_tier="smart",
            max_tokens=256,
        )
        if not result:
            return None

        confidence = result.get("confidence", 0)
        match_id = result.get("match_id")

        if match_id and confidence >= SIMILARITY_THRESHOLD:
            # Verify the match_id actually exists in our open tickets
            valid_ids = {t.id for t in open_tickets}
            if match_id not in valid_ids:
                logger.warning("AI returned invalid match_id {} for ticket {}", match_id, ticket.id)
                return None
            return {"match_id": match_id, "confidence": confidence}

        return None

    except Exception as e:
        logger.warning("Similarity check failed for ticket {}: {}", ticket.id, e)
        return None


async def consolidate_ticket(ticket_id: int, db: Session) -> None:
    """Check and link a ticket to a similar open ticket if found."""
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return

    # Skip if already linked
    if ticket.parent_ticket_id is not None:
        return

    match = await find_similar_ticket(ticket, db)
    if match:
        ticket.parent_ticket_id = match["match_id"]
        ticket.similarity_score = match["confidence"]
        db.commit()
        logger.info(
            "Ticket {} linked to parent {} (confidence={:.2f})",
            ticket_id,
            match["match_id"],
            match["confidence"],
        )


async def batch_consolidate(db: Session) -> int:
    """Scan unlinked open tickets and consolidate duplicates. Returns count linked."""
    unlinked = (
        db.query(TroubleTicket)
        .filter(
            TroubleTicket.parent_ticket_id.is_(None),
            TroubleTicket.status.in_(["submitted", "diagnosed", "escalated", "in_progress", "open"]),
        )
        .order_by(TroubleTicket.created_at.asc())
        .all()
    )

    linked_count = 0
    for ticket in unlinked:
        try:
            await consolidate_ticket(ticket.id, db)
            db.refresh(ticket)
            if ticket.parent_ticket_id is not None:
                linked_count += 1
        except Exception:
            logger.exception("Batch consolidation failed for ticket {}", ticket.id)

    if linked_count:
        logger.info("Batch consolidation: linked {} tickets", linked_count)
    return linked_count
```

**Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ticket_consolidation.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/ticket_consolidation.py tests/test_ticket_consolidation.py
git commit -m "feat: AI ticket consolidation — similarity detection and linking"
```

---

### Task 4: Hook Consolidation Into Ticket Creation + Scheduler

**Files:**
- Modify: `app/routers/trouble_tickets.py:39-97` (create_ticket endpoint)
- Modify: `app/jobs/selfheal_jobs.py` (add daily batch job, remove auto-close)

**Step 1: Write the failing test**

File: `tests/test_ticket_consolidation_integration.py`

```python
"""Integration: consolidation fires on ticket creation."""
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_create_ticket_triggers_consolidation(client, auth_headers):
    """POST /api/trouble-tickets should trigger consolidation in background."""
    with patch("app.routers.trouble_tickets.consolidate_ticket", new_callable=AsyncMock) as mock_consol:
        resp = client.post(
            "/api/trouble-tickets",
            json={"title": "Test issue", "description": "Something broke"},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    # consolidate_ticket is called via asyncio.create_task, may not have run yet
    # Just verify the ticket was created
    assert resp.json()["ok"] is True
```

**Step 2: Modify the ticket creation endpoint**

In `app/routers/trouble_tickets.py`, add after the existing `asyncio.create_task(svc.auto_process_ticket(ticket.id))` line:

```python
# Also run thread consolidation
from app.services.ticket_consolidation import consolidate_ticket
asyncio.create_task(consolidate_ticket(ticket.id))
```

Move the import to the top of the function or file-level lazy import.

**Step 3: Remove auto-close job, add daily consolidation**

In `app/jobs/selfheal_jobs.py`:

- Remove the `_job_self_heal_auto_close` function entirely
- Remove its `scheduler.add_job` registration
- Add a daily consolidation job:

```python
scheduler.add_job(
    _job_consolidate_tickets,
    CronTrigger(hour=5),
    id="consolidate_tickets",
    name="Daily ticket consolidation sweep",
)


@_traced_job
async def _job_consolidate_tickets():  # pragma: no cover
    """Daily sweep: find and link duplicate open tickets."""
    from ..database import SessionLocal
    from ..services.ticket_consolidation import batch_consolidate

    db = SessionLocal()
    try:
        linked = await batch_consolidate(db)
        if linked:
            logger.info("Daily consolidation: linked {} duplicate tickets", linked)
    except Exception:
        logger.exception("Daily ticket consolidation failed")
    finally:
        db.close()
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ticket_consolidation_integration.py tests/test_ticket_consolidation.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routers/trouble_tickets.py app/jobs/selfheal_jobs.py tests/test_ticket_consolidation_integration.py
git commit -m "feat: hook consolidation into ticket creation + daily batch, remove auto-close"
```

---

### Task 5: Dashboard — Child Count Badge + Linked Tickets

**Files:**
- Modify: `app/services/trouble_ticket_service.py:109-143` (list_tickets)
- Modify: `app/routers/trouble_tickets.py:233-285` (get_ticket detail)
- Modify: `app/static/tickets.js:296-341` (buildTicketTable)
- Modify: `app/static/tickets.js:344-593` (showTicketDetail)

**Step 1: Add child_count to list endpoint**

In `trouble_ticket_service.py` `list_tickets()`, add child count to each item:

```python
child_count = (
    db.query(TroubleTicket)
    .filter(TroubleTicket.parent_ticket_id == t.id)
    .count()
)
# Add to item dict:
"child_count": child_count,
```

**Step 2: Add linked tickets to detail endpoint**

In `trouble_tickets.py` `get_ticket()`, add:

```python
# Get child tickets
children = (
    db.query(TroubleTicket)
    .filter(TroubleTicket.parent_ticket_id == ticket.id)
    .order_by(TroubleTicket.created_at.desc())
    .all()
)
# Add to response dict:
"child_tickets": [
    {
        "id": c.id,
        "ticket_number": c.ticket_number,
        "title": c.title,
        "status": c.status,
        "similarity_score": c.similarity_score,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
    for c in children
],
"child_count": len(children),
```

Also include `parent_ticket_id` and `similarity_score` in the response if present.

**Step 3: Update frontend table to show child count**

In `tickets.js` `buildTicketTable()`, add a "Linked" column for admin view. Show badge with count if `t.child_count > 0`.

**Step 4: Update frontend detail to show linked tickets**

In `tickets.js` `showTicketDetail()`, after the description section, if `t.child_tickets && t.child_tickets.length`, render a "Linked Reports" collapsible section listing each child with its title, similarity score, and created date. Each clickable to navigate to that ticket.

**Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_tickets.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add app/services/trouble_ticket_service.py app/routers/trouble_tickets.py app/static/tickets.js
git commit -m "feat: child count badges + linked tickets in dashboard"
```

---

### Task 6: Report Issue Templates

**Files:**
- Modify: `app/static/tickets.js:12-19` (COMMON_ISSUES) and `app/static/tickets.js:105-114` (quick-select)

**Step 1: Update COMMON_ISSUES with templates**

Replace the existing `COMMON_ISSUES` array in `tickets.js`:

```javascript
var COMMON_ISSUES = [
    { label: '', title: '', hint: '' },
    { label: 'Search not working', title: 'Search returns no/wrong results for [part]', hint: 'What part number did you search? What did you expect to see?' },
    { label: 'Page won\'t load', title: 'Page fails to load: [which page]', hint: 'Which page? Do you see an error message or blank screen?' },
    { label: 'Data looks wrong', title: 'Incorrect data on [what]', hint: 'What data is wrong? What should it be?' },
    { label: 'Slow performance', title: 'Slow response on [where]', hint: 'Which page is slow? How long does it take to load?' },
    { label: 'Email/RFQ issue', title: 'Email or RFQ problem: [describe]', hint: 'Which RFQ? What happened? Did you get an error?' },
    { label: 'Other', title: '', hint: 'Describe what happened and what you expected.' },
];
```

**Step 2: Update the select + form to use templates**

When a template is selected, pre-fill the title input AND update the description placeholder with the hint text. The user replaces `[bracketed]` placeholders with their specifics.

```javascript
commonSelect.onchange = function() {
    var sel = COMMON_ISSUES.find(function(i) { return i.label === commonSelect.value; });
    if (!sel) return;
    var titleInput = document.getElementById('ttTitle');
    var descArea = document.getElementById('ttDesc');
    if (sel.title && titleInput) titleInput.value = sel.title;
    if (sel.hint && descArea) descArea.placeholder = sel.hint;
};
```

Update the option rendering to use `i.label` instead of the raw string.

**Step 3: Manual test** — verify templates pre-fill correctly in the browser.

**Step 4: Commit**

```bash
git add app/static/tickets.js
git commit -m "feat: report issue templates with guided descriptions"
```

---

### Task 7: Proactive Prompt Quality

**Files:**
- Modify: `app/services/ai_trouble_prompt.py:15-26` (VIEW_FILE_MAP)
- Modify: `app/services/ai_trouble_prompt.py:28-48` (SYSTEM_PROMPT)

**Step 1: Expand VIEW_FILE_MAP**

Add missing areas to the map:

```python
VIEW_FILE_MAP = {
    "rfq": "app/static/app.js (RFQ section), app/routers/requisitions.py, app/routers/rfq.py, app/services/email_service.py",
    "sourcing": "app/static/app.js (sourcing section), app/routers/sources.py, app/services/search_service.py, app/connectors/",
    "archive": "app/static/app.js (archive section), app/routers/requisitions.py",
    "crm": "app/static/crm.js, app/routers/crm.py",
    "companies": "app/static/crm.js (companies section), app/routers/crm.py",
    "contacts": "app/static/crm.js (contacts section), app/routers/crm.py",
    "quotes": "app/static/crm.js (quotes section), app/routers/crm.py",
    "vendors": "app/static/app.js (vendors section), app/routers/vendors.py",
    "settings": "app/static/crm.js (settings section), app/routers/admin.py",
    "pipeline": "app/static/crm.js (pipeline section), app/routers/crm.py",
    "activity": "app/static/crm.js (activity section), app/services/activity_service.py",
    "prospecting": "app/static/crm.js (prospecting section), app/routers/prospecting.py, app/services/prospecting_service.py",
    "tagging": "app/routers/tags.py, app/routers/tagging_admin.py, app/services/tagging.py",
    "tickets": "app/static/tickets.js, app/routers/trouble_tickets.py, app/services/trouble_ticket_service.py",
    "apollo": "app/routers/apollo_sync.py, app/services/apollo_sync_service.py",
    "notifications": "app/static/tickets.js (notifications section), app/services/notification_service.py",
    "upload": "app/static/app.js (upload section), app/routers/upload.py",
    "search": "app/static/app.js (search section), app/routers/sources.py, app/services/search_service.py, app/connectors/",
}
```

**Step 2: Improve SYSTEM_PROMPT**

Update the system prompt to produce more structured, actionable prompts:

```python
SYSTEM_PROMPT = """\
You are a senior engineer triaging trouble reports for AvailAI, an electronic component \
sourcing platform. Your job is to translate a user's plain-language trouble report into a \
concise, actionable prompt that can be pasted into Claude Code CLI to investigate and fix the issue.

Architecture context:
- Stack: FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Jinja2 + vanilla JS
- Frontend: app/static/app.js (search, RFQ, vendors, upload), app/static/crm.js \
(CRM, quotes, activity, settings, prospecting), app/static/tickets.js (tickets). \
Single template: app/templates/index.html
- Backend: app/routers/ for HTTP endpoints, app/services/ for business logic, app/models/ for ORM
- Tests: pytest with in-memory SQLite, run with TESTING=1 PYTHONPATH=/root/availai
- Always use Loguru for logging, never print()
- Error responses use: {"error": str, "status_code": int, "request_id": str}
- Database: use db.get(Model, id) not db.query(Model).get(id)

Return ONLY valid JSON (no markdown fences, no extra text) with exactly two keys:
- "title": a short (max 80 chars) summary of the issue suitable for a ticket title
- "prompt": a multi-line Claude Code prompt (200-500 words) structured as:
  1. CONTEXT: What the user reported (summarized) + reproduction info
  2. DIAGNOSIS: Likely root cause based on the error context
  3. FILES TO READ FIRST: Specific file paths to examine before making changes
  4. FIX INSTRUCTIONS: Concrete steps to fix the issue
  5. TEST: How to write a test for the fix and verify it works
  6. RULES: Make minimal changes. Write tests. Use Loguru. Run full test suite after.
"""
```

**Step 3: Run existing tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "trouble_prompt or ai_trouble" -v`
Expected: PASS

**Step 4: Commit**

```bash
git add app/services/ai_trouble_prompt.py
git commit -m "feat: expanded file maps + structured prompt template for better AI fix quality"
```

---

### Task 8: Coordination Endpoints (for agents)

**Files:**
- Modify: `app/routers/trouble_tickets.py`
- Modify: `app/schemas/trouble_ticket.py`
- Test: `tests/test_ticket_coordination.py`

**Step 1: Write failing tests**

File: `tests/test_ticket_coordination.py`

```python
"""Test agent coordination endpoints."""
import pytest
from app.models.trouble_ticket import TroubleTicket


def test_active_areas_empty(client, admin_headers):
    resp = client.get("/api/trouble-tickets/active-areas", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["areas"] == []


def test_active_areas_returns_tested_areas(client, admin_headers, db_session):
    t = TroubleTicket(
        ticket_number="TT-20260306-001",
        submitted_by=1,
        title="Search broken",
        description="...",
        source="playwright",
        tested_area="search",
        status="submitted",
    )
    db_session.add(t)
    db_session.commit()
    resp = client.get("/api/trouble-tickets/active-areas", headers=admin_headers)
    assert "search" in resp.json()["areas"]


def test_similar_check_endpoint(client, admin_headers, db_session):
    t = TroubleTicket(
        ticket_number="TT-20260306-001",
        submitted_by=1,
        title="Search returns no results",
        description="Empty results page",
        status="submitted",
    )
    db_session.add(t)
    db_session.commit()
    resp = client.get(
        "/api/trouble-tickets/similar",
        params={"title": "Search broken", "description": "Nothing shows up"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert "matches" in resp.json()
```

**Step 2: Implement endpoints**

Add to `app/routers/trouble_tickets.py`:

```python
@router.get("/api/trouble-tickets/active-areas")
async def active_areas(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Areas currently under automated test (last hour)."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = (
        db.query(TroubleTicket.tested_area)
        .filter(
            TroubleTicket.tested_area.isnot(None),
            TroubleTicket.source.in_(["playwright", "agent"]),
            TroubleTicket.created_at >= cutoff,
        )
        .distinct()
        .all()
    )
    return {"areas": [r[0] for r in rows]}


@router.get("/api/trouble-tickets/similar")
async def check_similar(
    title: str = Query(..., min_length=3),
    description: str = Query(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Check for similar open tickets (agent pre-submit check)."""
    from app.services.ticket_consolidation import find_similar_ticket
    temp = TroubleTicket(
        id=-1,
        title=title,
        description=description or title,
        status="submitted",
    )
    match = await find_similar_ticket(temp, db)
    if match:
        parent = db.get(TroubleTicket, match["match_id"])
        return {
            "matches": [{
                "id": parent.id,
                "ticket_number": parent.ticket_number,
                "title": parent.title,
                "confidence": match["confidence"],
            }] if parent else [],
        }
    return {"matches": []}
```

**Important:** These endpoints must be registered BEFORE the `/{ticket_id}` path parameter routes to avoid FastAPI matching "active-areas" or "similar" as a ticket_id. Check the current order in the router and place them accordingly (they should go near `stats`, `my-tickets`, etc.).

**Step 3: Update schema to accept agent fields**

In `app/schemas/trouble_ticket.py` `TroubleTicketCreate`, add:

```python
tested_area: str | None = None
dom_snapshot: str | None = None
network_errors: list[dict] | None = None
performance_timings: dict | None = None
reproduction_steps: list[str] | None = None
```

In `app/services/trouble_ticket_service.py` `create_ticket()`, accept and pass through these fields.

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ticket_coordination.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routers/trouble_tickets.py app/schemas/trouble_ticket.py app/services/trouble_ticket_service.py tests/test_ticket_coordination.py
git commit -m "feat: agent coordination endpoints — active-areas + similar check"
```

---

### Task 9: Playwright Site Tester Service

**Files:**
- Create: `app/services/site_tester.py`
- Test: `tests/test_site_tester.py`

**Step 1: Install Playwright**

Run: `cd /root/availai && pip install playwright && playwright install chromium`

Add `playwright` to `requirements.txt`.

**Step 2: Write failing tests**

File: `tests/test_site_tester.py`

```python
"""Test Playwright site tester service."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.site_tester import SiteTester, TEST_AREAS


def test_test_areas_comprehensive():
    """Verify all app areas are covered."""
    area_names = [a["name"] for a in TEST_AREAS]
    assert "search" in area_names
    assert "crm_companies" in area_names
    assert "crm_contacts" in area_names
    assert "requisitions" in area_names
    assert "rfq" in area_names
    assert "prospecting" in area_names
    assert "vendors" in area_names
    assert "tagging" in area_names
    assert "tickets" in area_names
    assert "admin_api_health" in area_names
    assert "auth" in area_names
    assert "notifications" in area_names


def test_site_tester_init():
    tester = SiteTester(base_url="http://localhost:8000", session_cookie="test123")
    assert tester.base_url == "http://localhost:8000"
    assert tester.issues == []


@pytest.mark.asyncio
async def test_record_issue_creates_ticket():
    """When an issue is found, it should be recorded for ticket creation."""
    tester = SiteTester(base_url="http://localhost:8000", session_cookie="test")
    tester.record_issue(
        area="search",
        title="Console error on search page",
        description="TypeError: Cannot read property 'map' of undefined",
        url="http://localhost:8000/#view-sourcing",
        screenshot_b64=None,
        network_errors=[],
        console_errors=["TypeError: Cannot read property 'map' of undefined"],
    )
    assert len(tester.issues) == 1
    assert tester.issues[0]["area"] == "search"
    assert tester.issues[0]["title"] == "Console error on search page"
```

**Step 3: Write implementation**

File: `app/services/site_tester.py`

```python
"""Playwright-based exhaustive site tester — clicks every button, checks every page.

Runs headless Chromium, navigates every route, clicks every interactive element,
captures console errors, network failures, slow responses, and empty states.

Called by: routers/trouble_tickets.py (POST /api/trouble-tickets/find-trouble)
Depends on: playwright (pip install playwright && playwright install chromium)
"""

import asyncio
import json
import time

from loguru import logger

# Every testable area with its entry point and key interactions
TEST_AREAS = [
    {"name": "search", "hash": "#view-sourcing", "description": "Part number search + results"},
    {"name": "requisitions", "hash": "#view-requisitions", "description": "Requisition CRUD + parts"},
    {"name": "rfq", "hash": "#view-rfq", "description": "RFQ creation + sending + responses"},
    {"name": "crm_companies", "hash": "#view-companies", "description": "Companies list + drawer + tabs"},
    {"name": "crm_contacts", "hash": "#view-contacts", "description": "Contacts list + create + enrich"},
    {"name": "crm_quotes", "hash": "#view-quotes", "description": "Quotes + line items + status"},
    {"name": "prospecting", "hash": "#view-suggested", "description": "Discovery pool + filters + cards"},
    {"name": "vendors", "hash": "#view-vendors", "description": "Vendor cards + offers + intelligence"},
    {"name": "tagging", "hash": "#view-tagging", "description": "Material tags + admin operations"},
    {"name": "tickets", "hash": "#view-tickets", "description": "Trouble tickets dashboard"},
    {"name": "admin_api_health", "hash": "#view-api-health", "description": "API health dashboard"},
    {"name": "admin_settings", "hash": "#view-settings", "description": "System settings panels"},
    {"name": "notifications", "hash": "#", "description": "Bell icon + notification panel"},
    {"name": "auth", "hash": "#", "description": "Login/logout + session + role enforcement"},
    {"name": "upload", "hash": "#view-upload", "description": "BOM + stock list upload"},
    {"name": "pipeline", "hash": "#view-pipeline", "description": "Sales pipeline + stages"},
    {"name": "activity", "hash": "#view-activity", "description": "Activity feed + tracking"},
]


class SiteTester:
    """Orchestrates a full Playwright sweep of the application."""

    def __init__(self, base_url: str, session_cookie: str):
        self.base_url = base_url.rstrip("/")
        self.session_cookie = session_cookie
        self.issues: list[dict] = []
        self.progress: list[str] = []
        self.areas_tested = 0
        self.total_areas = len(TEST_AREAS)

    def record_issue(
        self,
        area: str,
        title: str,
        description: str,
        url: str | None = None,
        screenshot_b64: str | None = None,
        network_errors: list | None = None,
        console_errors: list | None = None,
        performance_ms: float | None = None,
    ):
        """Record a discovered issue for later ticket creation."""
        self.issues.append({
            "area": area,
            "title": title,
            "description": description,
            "url": url,
            "screenshot_b64": screenshot_b64,
            "network_errors": network_errors or [],
            "console_errors": console_errors or [],
            "performance_ms": performance_ms,
        })

    async def run_full_sweep(self) -> list[dict]:
        """Run exhaustive test of every area. Returns list of issues found."""
        from playwright.async_api import async_playwright

        self.progress.append("Starting full site audit...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )

            # Set auth session cookie
            await context.add_cookies([{
                "name": "session",
                "value": self.session_cookie,
                "domain": self.base_url.split("//")[1].split(":")[0].split("/")[0],
                "path": "/",
            }])

            page = await context.new_page()

            # Capture console errors
            console_errors: list[str] = []
            page.on("console", lambda msg: (
                console_errors.append(f"{msg.type}: {msg.text}")
                if msg.type in ("error", "warning") else None
            ))

            # Capture network failures
            network_errors: list[dict] = []
            page.on("response", lambda resp: (
                network_errors.append({"url": resp.url, "status": resp.status})
                if resp.status >= 400 else None
            ))

            for area in TEST_AREAS:
                self.progress.append(f"Testing {area['name']}...")
                console_errors.clear()
                network_errors.clear()

                try:
                    await self._test_area(page, area, console_errors, network_errors)
                except Exception as e:
                    self.record_issue(
                        area=area["name"],
                        title=f"Crash testing {area['name']}: {str(e)[:100]}",
                        description=f"Playwright crashed while testing {area['description']}: {e}",
                        url=f"{self.base_url}/{area['hash']}",
                    )
                    logger.warning("Playwright crash on {}: {}", area["name"], e)

                self.areas_tested += 1

            await browser.close()

        self.progress.append(f"Audit complete. {len(self.issues)} issues found across {self.areas_tested} areas.")
        return self.issues

    async def _test_area(self, page, area: dict, console_errors: list, network_errors: list):
        """Test a single area: navigate, wait, click everything, check for errors."""
        url = f"{self.base_url}/{area['hash']}"
        start = time.time()

        await page.goto(url, wait_until="networkidle", timeout=15000)
        load_time = (time.time() - start) * 1000

        # Check slow load
        if load_time > 3000:
            self.record_issue(
                area=area["name"],
                title=f"Slow page load: {area['name']} ({load_time:.0f}ms)",
                description=f"{area['description']} took {load_time:.0f}ms to load (threshold: 3000ms)",
                url=url,
                performance_ms=load_time,
            )

        # Wait for dynamic content
        await page.wait_for_timeout(1000)

        # Check for console errors after initial load
        if console_errors:
            self.record_issue(
                area=area["name"],
                title=f"Console errors on {area['name']} load",
                description=f"Errors during page load:\n" + "\n".join(console_errors[:10]),
                url=url,
                console_errors=list(console_errors),
            )

        # Check for network errors
        if network_errors:
            self.record_issue(
                area=area["name"],
                title=f"Network errors on {area['name']}",
                description=f"Failed requests:\n" + "\n".join(
                    f"  {e['status']} {e['url']}" for e in network_errors[:10]
                ),
                url=url,
                network_errors=list(network_errors),
            )

        # Click every button and interactive element
        console_errors.clear()
        network_errors.clear()

        buttons = await page.query_selector_all("button:visible, .btn:visible, [onclick]:visible")
        for btn in buttons[:30]:  # Cap to avoid infinite loops
            try:
                btn_text = await btn.text_content()
                if not btn_text or btn_text.strip() in ("", "x", "X"):
                    continue
                # Skip destructive actions
                lower = (btn_text or "").lower().strip()
                if any(word in lower for word in ["delete", "remove", "drop", "reset", "logout", "sign out"]):
                    continue

                await btn.click(timeout=3000)
                await page.wait_for_timeout(500)

                if console_errors:
                    self.record_issue(
                        area=area["name"],
                        title=f"Error clicking '{btn_text.strip()[:50]}' on {area['name']}",
                        description=f"Console errors after clicking button:\n" + "\n".join(console_errors[:5]),
                        url=url,
                        console_errors=list(console_errors),
                    )
                    console_errors.clear()

            except Exception:
                pass  # Button may have navigated away or become stale

        # Check for empty states that shouldn't be empty (tables with 0 rows, "No data" messages)
        empty_indicators = await page.query_selector_all(".empty, [class*='no-data'], [class*='empty-state']")
        for indicator in empty_indicators:
            text = await indicator.text_content()
            if text and "no" in text.lower() and "loading" not in text.lower():
                logger.debug("Empty state on {}: {}", area["name"], text.strip()[:80])


async def create_tickets_from_issues(issues: list[dict], db) -> int:
    """Create TroubleTickets from Playwright findings. Returns count created."""
    from app.services.trouble_ticket_service import create_ticket

    created = 0
    for issue in issues:
        try:
            ticket = create_ticket(
                db=db,
                user_id=1,  # System/admin user
                title=issue["title"][:200],
                description=issue["description"],
                current_page=issue.get("url"),
                source="playwright",
                console_errors=json.dumps(issue.get("console_errors", [])) if issue.get("console_errors") else None,
                screenshot_b64=issue.get("screenshot_b64"),
            )
            # Set agent-specific fields
            ticket.tested_area = issue["area"]
            ticket.network_errors = issue.get("network_errors")
            ticket.performance_timings = {"load_ms": issue.get("performance_ms")} if issue.get("performance_ms") else None
            db.commit()
            created += 1
        except Exception:
            logger.exception("Failed to create ticket for issue: {}", issue["title"])

    return created
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_site_tester.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/site_tester.py tests/test_site_tester.py requirements.txt
git commit -m "feat: Playwright site tester — exhaustive click-every-button sweep"
```

---

### Task 10: Test Prompt Generator (Claude Agent Prompts)

**Files:**
- Create: `app/services/test_prompts.py`
- Test: `tests/test_test_prompts.py`

**Step 1: Write failing test**

File: `tests/test_test_prompts.py`

```python
"""Test prompt generation for Claude agents."""
from app.services.test_prompts import generate_all_prompts, generate_area_prompt


def test_generate_all_prompts_covers_every_area():
    prompts = generate_all_prompts()
    assert len(prompts) >= 15
    names = [p["area"] for p in prompts]
    assert "search" in names
    assert "crm_companies" in names
    assert "rfq" in names


def test_generate_area_prompt_has_required_fields():
    prompt = generate_area_prompt("search")
    assert "area" in prompt
    assert "url" in prompt
    assert "prompt" in prompt
    assert "what to test" in prompt["prompt"].lower() or "test" in prompt["prompt"].lower()


def test_generate_unknown_area_returns_none():
    result = generate_area_prompt("nonexistent_area")
    assert result is None
```

**Step 2: Write implementation**

File: `app/services/test_prompts.py`

```python
"""Claude agent test prompt generator — creates copy-paste prompts for intelligent testing.

Each prompt tells a Claude-in-Chrome agent what to test in a specific app area,
what correct behavior looks like, and how to submit findings.

Called by: routers/trouble_tickets.py (POST /api/trouble-tickets/find-trouble)
Depends on: nothing (pure data)
"""

AREA_PROMPTS = {
    "search": {
        "url_hash": "#view-sourcing",
        "prompt": """You are testing the SEARCH feature of AvailAI (electronic component sourcing).

WHAT TO TEST:
- Enter known part numbers (LM358, STM32F103, SN74HC595) and verify results appear
- Check that all connector sources show results (BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets, Element14)
- Verify pricing, quantities, and vendor names display correctly
- Test empty searches, partial part numbers, special characters
- Click on individual results — verify detail view opens
- Test "Add to Requisition" and "Add to RFQ" buttons from results
- Check pagination if many results
- Verify no console errors during any of these operations

WHAT CORRECT LOOKS LIKE:
- Results appear within 5 seconds for common parts
- Each result shows: vendor, MPN, quantity, price, source badge
- No "undefined" or "null" values in displayed data
- All buttons respond without errors

SUBMITTING FINDINGS:
For each issue found, submit a ticket via POST /api/trouble-tickets with:
- title: short description
- description: full details + steps to reproduce
- current_page: the URL hash
- source: "agent"
- tested_area: "search"
Check /api/trouble-tickets/similar?title=...&description=... BEFORE submitting to avoid duplicates.""",
    },
    "crm_companies": {
        "url_hash": "#view-companies",
        "prompt": """You are testing the COMPANIES feature of AvailAI CRM.

WHAT TO TEST:
- Companies list loads with pagination (default 100/page)
- Click "Load More" to verify pagination works
- Use owner filter dropdown — verify it filters correctly
- Click a company row to open the drawer
- In the drawer, test each tab: Overview, Contacts, Requisitions, Quotes, Activity
- Verify site count and open req count display correctly
- Test search/filter if available
- Check that company names, addresses, and metadata display without "undefined"

WHAT CORRECT LOOKS LIKE:
- List loads in under 1 second (cached) or 3 seconds (cold)
- Drawer tabs load their data when clicked
- Counts match the actual data in each tab
- No console errors

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "crm_companies"
Check /api/trouble-tickets/similar first.""",
    },
    "crm_contacts": {
        "url_hash": "#view-contacts",
        "prompt": """You are testing the CONTACTS feature of AvailAI CRM.

WHAT TO TEST:
- Contacts list loads (uses bulk endpoint)
- Create a new contact — fill all fields, submit
- Edit an existing contact
- Test enrichment buttons (Lusha, Hunter, Apollo) — verify they show results
- Verify contact details display: name, email, phone, company, title
- Test any search/filter functionality

WHAT CORRECT LOOKS LIKE:
- List loads without N+1 delays
- Create/edit forms validate required fields
- Enrichment shows data or a clear "no data found" message
- No console errors

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "crm_contacts"
Check /api/trouble-tickets/similar first.""",
    },
    "crm_quotes": {
        "url_hash": "#view-quotes",
        "prompt": """You are testing the QUOTES feature of AvailAI CRM.

WHAT TO TEST:
- Quotes list loads with status indicators
- Create a new quote — add line items, set prices
- Edit existing quote — change quantities, prices
- Test status transitions (draft → sent → accepted/rejected)
- Verify total calculations are correct
- Check PDF/export functionality if available

WHAT CORRECT LOOKS LIKE:
- Totals compute correctly (qty * price)
- Status changes persist after page refresh
- No "NaN" or "$undefined" in price displays

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "crm_quotes"
Check /api/trouble-tickets/similar first.""",
    },
    "requisitions": {
        "url_hash": "#view-requisitions",
        "prompt": """You are testing REQUISITIONS in AvailAI.

WHAT TO TEST:
- Requisitions list loads with pagination
- Create a new requisition
- Add parts to a requisition — verify part count updates
- Clone a requisition — verify the clone has the same parts
- Change requisition status
- Test archive/delete functionality
- Click into a requisition detail — verify parts list

WHAT CORRECT LOOKS LIKE:
- Part counts match actual parts in the requisition
- Clone creates independent copy
- Status changes reflect immediately
- No stale data after updates

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "requisitions"
Check /api/trouble-tickets/similar first.""",
    },
    "rfq": {
        "url_hash": "#view-rfq",
        "prompt": """You are testing the RFQ (Request for Quote) system in AvailAI.

WHAT TO TEST:
- RFQ list loads
- Create a new RFQ from a requisition
- View RFQ details — vendor selection, part list
- Test send functionality (may need to verify without actually sending)
- View RFQ responses and parsed offers
- Check status transitions

WHAT CORRECT LOOKS LIKE:
- RFQs linked correctly to requisitions
- Vendor list populated
- Response parser shows confidence scores
- No errors on status changes

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "rfq"
Check /api/trouble-tickets/similar first.""",
    },
    "prospecting": {
        "url_hash": "#view-suggested",
        "prompt": """You are testing the PROSPECTING discovery pool in AvailAI.

WHAT TO TEST:
- Discovery pool cards load
- Filter by revenue range, industry, region
- Sort by recency
- Click a company card — verify detail loads
- Test enrichment actions on cards
- Check stats endpoint data matches displayed counts
- Verify "discovery_source" labels on cards

WHAT CORRECT LOOKS LIKE:
- Cards show company name, industry, revenue, location
- Filters actually reduce the result set
- No empty cards with missing data
- Enrichment shows results or clear error

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "prospecting"
Check /api/trouble-tickets/similar first.""",
    },
    "vendors": {
        "url_hash": "#view-vendors",
        "prompt": """You are testing the VENDORS section of AvailAI.

WHAT TO TEST:
- Vendor list loads
- Click a vendor — verify detail drawer
- Check offers tab — verify offers display with pricing
- Check intelligence tab — vendor scoring, reliability
- Test any vendor search/filter

WHAT CORRECT LOOKS LIKE:
- Vendor names normalized (no duplicates)
- Offers show price, quantity, date
- Scores are numeric and reasonable (0-100 range)

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "vendors"
Check /api/trouble-tickets/similar first.""",
    },
    "tagging": {
        "url_hash": "#view-tagging",
        "prompt": """You are testing the MATERIAL TAGGING system in AvailAI.

WHAT TO TEST:
- Tag list/management page loads
- Material card tags display correctly
- Admin operations: purge unknown, analyze prefixes, backfill
- Tag statistics endpoint
- Confidence scores display and are reasonable

WHAT CORRECT LOOKS LIKE:
- Tags have names and confidence scores
- Admin buttons respond without errors
- Stats show coverage percentage

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "tagging"
Check /api/trouble-tickets/similar first.""",
    },
    "tickets": {
        "url_hash": "#view-tickets",
        "prompt": """You are testing the TROUBLE TICKETS system itself (meta-testing!).

WHAT TO TEST:
- Ticket list loads with filter pills
- Create a new ticket via "+ New Ticket"
- View ticket detail — all sections expand
- Admin actions: Diagnose, Execute Fix, Escalate, Resolve
- Stats bar loads with health indicator
- Notification bell + panel
- Template quick-select pre-fills title

WHAT CORRECT LOOKS LIKE:
- Tickets show number, title, status badge, risk badge
- Detail view shows all metadata
- Actions update status immediately
- Stats refresh after changes

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "tickets"
Check /api/trouble-tickets/similar first.""",
    },
    "admin_api_health": {
        "url_hash": "#view-api-health",
        "prompt": """You are testing the API HEALTH dashboard in AvailAI admin.

WHAT TO TEST:
- Dashboard loads with status grid
- Each API source shows a status (green/yellow/red)
- Usage overview section displays
- Click individual sources for detail
- Verify status reflects actual API availability

WHAT CORRECT LOOKS LIKE:
- All configured APIs show a status
- Working APIs are green, broken ones are red
- Usage numbers are numeric, not null/undefined

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "admin_api_health"
Check /api/trouble-tickets/similar first.""",
    },
    "admin_settings": {
        "url_hash": "#view-settings",
        "prompt": """You are testing ADMIN SETTINGS in AvailAI.

WHAT TO TEST:
- Settings panels load
- Each config section displays current values
- Save button works (test with a non-destructive change if possible)
- Verify settings actually persist after save

WHAT CORRECT LOOKS LIKE:
- All panels render without errors
- Current values displayed correctly
- Save shows success confirmation

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "admin_settings"
Check /api/trouble-tickets/similar first.""",
    },
    "notifications": {
        "url_hash": "#",
        "prompt": """You are testing the NOTIFICATION system in AvailAI.

WHAT TO TEST:
- Bell icon displays with unread count badge
- Click bell — notification panel opens
- Notifications show event type, title, body, time
- Click a notification — navigates to related ticket
- "Mark all read" button works
- Badge count updates after marking read

WHAT CORRECT LOOKS LIKE:
- Badge shows accurate unread count
- Panel displays notifications in reverse chronological order
- Click navigation works correctly
- Mark-read clears badge

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "notifications"
Check /api/trouble-tickets/similar first.""",
    },
    "auth": {
        "url_hash": "#",
        "prompt": """You are testing AUTH and SESSION handling in AvailAI.

WHAT TO TEST:
- Verify you are logged in and can see admin features
- Check that the session persists across page refreshes
- Verify role-based access (admin sees all tabs)
- Check that API calls include proper auth headers
- Test any 401 redirect behavior

WHAT CORRECT LOOKS LIKE:
- User info displayed correctly (name, role)
- All admin tabs visible and functional
- No random session expiry during testing
- 401 redirects to login page

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "auth"
Check /api/trouble-tickets/similar first.""",
    },
    "upload": {
        "url_hash": "#view-upload",
        "prompt": """You are testing FILE UPLOAD in AvailAI.

WHAT TO TEST:
- Upload page loads
- BOM upload accepts Excel/CSV files
- Stock list upload works
- Upload progress indicator
- Parsed data displays after upload
- Error handling for invalid files

WHAT CORRECT LOOKS LIKE:
- File picker opens and accepts files
- Progress shows during upload
- Parsed results display in table format
- Clear error message for unsupported formats

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "upload"
Check /api/trouble-tickets/similar first.""",
    },
    "pipeline": {
        "url_hash": "#view-pipeline",
        "prompt": """You are testing the SALES PIPELINE in AvailAI CRM.

WHAT TO TEST:
- Pipeline view loads with stages
- Opportunities display in correct stages
- Drag or move opportunities between stages
- Click an opportunity for detail
- Create a new opportunity

WHAT CORRECT LOOKS LIKE:
- Stages display left-to-right
- Opportunities show company, value, status
- Stage transitions persist
- No console errors on interactions

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "pipeline"
Check /api/trouble-tickets/similar first.""",
    },
    "activity": {
        "url_hash": "#view-activity",
        "prompt": """You are testing the ACTIVITY feed in AvailAI CRM.

WHAT TO TEST:
- Activity feed loads
- Events display with timestamps
- Filter by activity type if available
- Pagination/load more works
- Activity details show relevant context

WHAT CORRECT LOOKS LIKE:
- Events in reverse chronological order
- Each event shows type, description, timestamp
- No "undefined" user names or missing dates

SUBMITTING FINDINGS:
POST /api/trouble-tickets with source: "agent", tested_area: "activity"
Check /api/trouble-tickets/similar first.""",
    },
}


def generate_all_prompts() -> list[dict]:
    """Generate test prompts for all areas. Returns list of {area, url, prompt}."""
    return [
        {
            "area": name,
            "url_hash": data["url_hash"],
            "prompt": data["prompt"],
        }
        for name, data in AREA_PROMPTS.items()
    ]


def generate_area_prompt(area: str) -> dict | None:
    """Generate test prompt for a specific area. Returns {area, url, prompt} or None."""
    data = AREA_PROMPTS.get(area)
    if not data:
        return None
    return {
        "area": area,
        "url": data["url_hash"],
        "prompt": data["prompt"],
    }
```

**Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_test_prompts.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add app/services/test_prompts.py tests/test_test_prompts.py
git commit -m "feat: Claude agent test prompt generator — 17 area prompts"
```

---

### Task 11: "Find Trouble" Endpoint + Frontend Button

**Files:**
- Modify: `app/routers/trouble_tickets.py`
- Modify: `app/static/tickets.js`
- Test: `tests/test_find_trouble.py`

**Step 1: Write failing test**

File: `tests/test_find_trouble.py`

```python
"""Test Find Trouble endpoint."""
import pytest
from unittest.mock import AsyncMock, patch


def test_find_trouble_returns_job_id(client, admin_headers):
    with patch("app.routers.trouble_tickets.asyncio.create_task"):
        resp = client.post("/api/trouble-tickets/find-trouble", headers=admin_headers)
    assert resp.status_code == 200
    assert "job_id" in resp.json()


def test_find_trouble_prompts_endpoint(client, admin_headers):
    resp = client.get("/api/trouble-tickets/find-trouble/prompts", headers=admin_headers)
    assert resp.status_code == 200
    prompts = resp.json()["prompts"]
    assert len(prompts) >= 15
    assert all("area" in p and "prompt" in p for p in prompts)
```

**Step 2: Add endpoints**

In `app/routers/trouble_tickets.py`:

```python
# In-memory job tracking (simple — single instance)
_sweep_jobs: dict[str, dict] = {}


@router.post("/api/trouble-tickets/find-trouble")
async def start_find_trouble(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Launch exhaustive site audit. Returns job_id for progress polling."""
    import uuid
    from app.services.site_tester import SiteTester, create_tickets_from_issues

    job_id = str(uuid.uuid4())[:8]
    session_cookie = request.cookies.get("session", "")

    tester = SiteTester(
        base_url=str(request.base_url).rstrip("/"),
        session_cookie=session_cookie,
    )
    _sweep_jobs[job_id] = {"status": "running", "tester": tester, "issues_created": 0}

    async def _run():
        try:
            issues = await tester.run_full_sweep()
            from app.database import SessionLocal
            sweep_db = SessionLocal()
            try:
                count = await create_tickets_from_issues(issues, sweep_db)
                _sweep_jobs[job_id]["issues_created"] = count
            finally:
                sweep_db.close()
            _sweep_jobs[job_id]["status"] = "complete"
        except Exception as e:
            logger.exception("Find Trouble sweep failed")
            _sweep_jobs[job_id]["status"] = f"error: {e}"

    asyncio.create_task(_run())
    return {"job_id": job_id, "status": "running", "total_areas": len(SiteTester.TEST_AREAS if hasattr(SiteTester, 'TEST_AREAS') else [])}


@router.get("/api/trouble-tickets/find-trouble/{job_id}")
async def find_trouble_progress(
    job_id: str,
    user: User = Depends(require_admin),
):
    """Poll progress of a Find Trouble sweep."""
    job = _sweep_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    tester = job.get("tester")
    return {
        "status": job["status"],
        "progress": tester.progress if tester else [],
        "areas_tested": tester.areas_tested if tester else 0,
        "issues_found": len(tester.issues) if tester else 0,
        "issues_created": job.get("issues_created", 0),
    }


@router.get("/api/trouble-tickets/find-trouble/prompts")
async def find_trouble_prompts(
    user: User = Depends(require_admin),
):
    """Get Claude agent test prompts for all areas."""
    from app.services.test_prompts import generate_all_prompts
    return {"prompts": generate_all_prompts()}
```

**Important:** Place these routes BEFORE `/{ticket_id}` routes.

**Step 3: Add "Find Trouble" button to frontend**

In `tickets.js` `renderAdminDashboard()`, after the "+ New Ticket" button in the header, add:

```javascript
el('button', {
    className: 'btn btn-sm',
    textContent: 'Find Trouble',
    style: 'background:#dc2626;color:#fff;margin-left:8px;',
    onclick: function() { startFindTrouble(container); },
}),
```

Add the `startFindTrouble` function:

```javascript
async function startFindTrouble(container) {
    if (!confirm('Launch exhaustive site audit? This will:\n1. Playwright sweep (~5 min)\n2. Generate Claude agent prompts\n\nProceed?')) return;

    showToast('Starting site audit...', 'info');

    try {
        // Start Playwright sweep
        var job = await apiFetch('/api/trouble-tickets/find-trouble', { method: 'POST' });
        showToast('Playwright sweep started (job: ' + job.job_id + ')', 'success');

        // Poll progress
        pollSweepProgress(job.job_id, container);

        // Open Claude agent prompts
        var promptData = await apiFetch('/api/trouble-tickets/find-trouble/prompts');
        var prompts = promptData.prompts || [];

        // Open tabs for each area
        prompts.forEach(function(p) {
            window.open(window.location.origin + '/' + p.url_hash, '_blank');
        });

        // Show prompt panel
        showPromptPanel(prompts);
    } catch (e) {
        showToast('Failed to start audit: ' + e.message, 'error');
    }
}

function pollSweepProgress(jobId, container) {
    var interval = setInterval(async function() {
        try {
            var status = await apiFetch('/api/trouble-tickets/find-trouble/' + jobId);
            if (status.status === 'complete') {
                clearInterval(interval);
                showToast('Audit complete: ' + status.issues_found + ' issues found, ' + status.issues_created + ' tickets created', 'success');
                renderAdminDashboard(container);
            } else if (status.status.startsWith('error')) {
                clearInterval(interval);
                showToast('Audit failed: ' + status.status, 'error');
            }
        } catch (e) {
            clearInterval(interval);
        }
    }, 5000);
}

function showPromptPanel(prompts) {
    // Floating panel with copyable prompts
    var overlay = el('div', {
        id: 'promptPanel',
        style: 'position:fixed;right:16px;top:60px;width:420px;max-height:80vh;overflow-y:auto;background:#fff;border:1px solid var(--border);border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.15);z-index:9999;padding:16px;',
    });

    var closeBtn = el('button', {
        style: 'position:absolute;top:8px;right:12px;background:none;border:none;font-size:18px;cursor:pointer;color:var(--muted);',
        textContent: 'x',
        onclick: function() { overlay.remove(); },
    });
    overlay.appendChild(closeBtn);
    overlay.appendChild(el('h3', { style: 'margin:0 0 12px;font-size:14px;', textContent: 'Claude Agent Prompts' }));
    overlay.appendChild(el('p', { style: 'font-size:11px;color:var(--muted);margin-bottom:12px;', textContent: 'Copy each prompt into Claude in the corresponding browser tab.' }));

    prompts.forEach(function(p) {
        var card = el('div', {
            style: 'border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px;',
        });
        var cardHeader = el('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;' });
        cardHeader.appendChild(el('strong', { style: 'font-size:12px;', textContent: p.area }));
        var copyBtn = el('button', {
            className: 'btn btn-sm',
            textContent: 'Copy',
            style: 'font-size:10px;padding:2px 8px;',
        });
        copyBtn.onclick = function() {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(p.prompt).then(function() {
                    copyBtn.textContent = 'Copied!';
                    setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1500);
                });
            }
        };
        cardHeader.appendChild(copyBtn);
        card.appendChild(cardHeader);
        card.appendChild(el('p', {
            style: 'font-size:10px;color:var(--muted);margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;',
            textContent: p.prompt.split('\n')[0],
        }));
        overlay.appendChild(card);
    });

    document.body.appendChild(overlay);
}
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_find_trouble.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routers/trouble_tickets.py app/static/tickets.js tests/test_find_trouble.py
git commit -m "feat: Find Trouble button — Playwright sweep + Claude agent prompt panel"
```

---

### Task 12: Full Test Suite + Coverage

**Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS

**Step 2: Coverage check**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: No coverage regression from new files

**Step 3: Fix any failures**

Address any test failures or import errors from the new code.

**Step 4: Final commit**

```bash
git add -A
git commit -m "test: full suite passing after ticket refinement"
```

---

### Task 13: Deploy + Verify

**Step 1: Rebuild and deploy**

```bash
cd /root/availai && docker compose up -d --build
```

**Step 2: Check logs**

```bash
docker compose logs -f app 2>&1 | head -50
```

Verify: clean startup, Vinod user seeded, no migration errors.

**Step 3: Verify Playwright is installed in container**

If Playwright needs to be in the Docker image, add to `Dockerfile`:

```dockerfile
RUN pip install playwright && playwright install --with-deps chromium
```

**Step 4: End-to-end verification**

- Log in as admin
- Navigate to Tickets
- Verify "Find Trouble" button visible
- Submit a test ticket using a template
- Submit a similar ticket — verify it gets linked (parent_ticket_id set)
- Check dashboard shows child count badge

**Step 5: Commit any deploy fixes**

```bash
git add -A
git commit -m "deploy: ticket refinement — all 6 improvements live"
```
