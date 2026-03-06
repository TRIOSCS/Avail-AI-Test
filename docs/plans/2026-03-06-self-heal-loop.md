# Self-Heal Loop Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the broken subprocess execution with Claude API patch generation + host watcher, and add post-heal Playwright verification that creates regression tickets on failure.

**Architecture:** The app (in Docker) handles testing, diagnosis, and patch generation via Claude API. Fix payloads are written as JSON to a shared volume. A host-side watcher script applies patches, rebuilds the container, and triggers a verify-retest endpoint that runs SiteTester on just the affected area.

**Tech Stack:** Anthropic SDK (via existing `claude_client.py`), Playwright (via existing `site_tester.py`), bash (host watcher), shared Docker volume.

---

### Task 1: Add fix_queue shared volume to Docker Compose

**Files:**
- Modify: `docker-compose.yml:80-82` (app volumes section)

**Step 1: Add the volume mount**

In `docker-compose.yml`, add `./fix_queue:/app/fix_queue` to the app service volumes:

```yaml
    volumes:
      - uploads:/app/uploads
      - static_files:/srv/static
      - applogs:/var/log/avail
      - ./fix_queue:/app/fix_queue
```

**Step 2: Create the directory structure on host**

Run:
```bash
mkdir -p /root/availai/fix_queue/applied /root/availai/fix_queue/failed
```

**Step 3: Add fix_queue to .gitignore**

Append to `.gitignore`:
```
fix_queue/
```

**Step 4: Commit**

```bash
git add docker-compose.yml .gitignore
git commit -m "feat: add fix_queue shared volume for self-heal pipeline"
```

---

### Task 2: Create patch_generator service (Claude API → structured patches)

**Files:**
- Create: `app/services/patch_generator.py`
- Test: `tests/test_patch_generator.py`

**Step 1: Write the failing test**

Create `tests/test_patch_generator.py`:

```python
"""Tests for patch_generator service.

Covers: Claude API patch generation, file reading, JSON output format.

Called by: pytest
Depends on: app.services.patch_generator
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.services.patch_generator import generate_patches, PATCH_SCHEMA


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestGeneratePatches:
    @patch("app.services.patch_generator.claude_structured", new_callable=AsyncMock)
    def test_generates_patch_list(self, mock_claude):
        mock_claude.return_value = {
            "patches": [
                {
                    "file": "app/static/tickets.js",
                    "search": "var x = 1;",
                    "replace": "var x = 2;",
                    "explanation": "Fix variable value",
                }
            ],
            "summary": "Fixed the value",
        }
        result = _run(generate_patches(
            title="Bug in tickets",
            diagnosis={"root_cause": "Wrong value", "fix_approach": "Change x to 2"},
            category="ui",
            affected_files=["app/static/tickets.js"],
        ))
        assert result is not None
        assert len(result["patches"]) == 1
        assert result["patches"][0]["file"] == "app/static/tickets.js"
        assert result["summary"] == "Fixed the value"

    @patch("app.services.patch_generator.claude_structured", new_callable=AsyncMock)
    def test_returns_none_on_api_failure(self, mock_claude):
        mock_claude.return_value = None
        result = _run(generate_patches(
            title="Bug",
            diagnosis={"root_cause": "Unknown"},
            category="other",
            affected_files=[],
        ))
        assert result is None

    @patch("app.services.patch_generator.claude_structured", new_callable=AsyncMock)
    def test_reads_file_contents_for_context(self, mock_claude):
        mock_claude.return_value = {
            "patches": [],
            "summary": "No changes needed",
        }
        _run(generate_patches(
            title="Bug",
            diagnosis={"root_cause": "Test"},
            category="ui",
            affected_files=["app/config.py"],
        ))
        # Verify the prompt sent to Claude includes file contents
        call_args = mock_claude.call_args
        prompt = call_args.kwargs.get("prompt") or call_args[0][0]
        assert "app/config.py" in prompt

    def test_patch_schema_is_valid(self):
        assert "patches" in PATCH_SCHEMA["properties"]
        assert "summary" in PATCH_SCHEMA["properties"]
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_patch_generator.py -v`
Expected: FAIL — module not found

**Step 3: Write the implementation**

Create `app/services/patch_generator.py`:

```python
"""Patch generator — uses Claude API to produce search/replace patches for tickets.

Reads affected source files, sends them with the diagnosis to Claude,
and returns structured patches ready to apply.

Called by: services/execution_service.py
Depends on: utils/claude_client.py, services/prompt_generator.py (BASE_CONSTRAINTS)
"""

import os

from loguru import logger

from app.services.prompt_generator import BASE_CONSTRAINTS, CATEGORY_RULES
from app.utils.claude_client import claude_structured

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Relative path from project root"},
                    "search": {"type": "string", "description": "Exact code block to find"},
                    "replace": {"type": "string", "description": "Replacement code block"},
                    "explanation": {"type": "string", "description": "Why this change fixes the issue"},
                },
                "required": ["file", "search", "replace", "explanation"],
            },
        },
        "summary": {"type": "string", "description": "One-sentence summary of all changes"},
    },
    "required": ["patches", "summary"],
}

PATCH_SYSTEM = """You are a senior developer fixing bugs in AVAIL, a FastAPI + PostgreSQL electronic component sourcing platform.

You MUST return search/replace patches. Each patch has:
- file: the relative file path (e.g. app/static/tickets.js)
- search: the EXACT current code (copy-paste, including whitespace)
- replace: the fixed code
- explanation: why this change fixes the bug

Rules:
- Keep patches minimal — fix only what's broken
- The search string MUST match the file contents exactly
- Never modify authentication, encryption, or migration files
- Prefer fixing the root cause over adding workarounds
"""


def _read_source_file(rel_path: str) -> str | None:
    """Read a source file from the container filesystem."""
    # Inside Docker the app lives at /app, in tests at project root
    for base in ["/app", os.getcwd()]:
        full = os.path.join(base, rel_path)
        if os.path.isfile(full):
            try:
                with open(full) as f:
                    content = f.read()
                # Truncate very large files to avoid token bloat
                if len(content) > 15000:
                    content = content[:15000] + "\n... (truncated)"
                return content
            except Exception:
                pass
    return None


async def generate_patches(
    title: str,
    diagnosis: dict,
    category: str,
    affected_files: list[str],
) -> dict | None:
    """Generate search/replace patches using Claude API.

    Returns: {patches: [{file, search, replace, explanation}], summary} or None.
    """
    # Build file context
    file_sections = []
    for fpath in affected_files[:5]:  # Limit to 5 files for token budget
        content = _read_source_file(fpath)
        if content:
            file_sections.append(f"### {fpath}\n```\n{content}\n```")
        else:
            file_sections.append(f"### {fpath}\n(file not readable)")

    file_context = "\n\n".join(file_sections) if file_sections else "No file contents available."

    category_rules = CATEGORY_RULES.get(category, CATEGORY_RULES.get("other", ""))

    prompt = f"""Fix this bug:

**Title:** {title}
**Root Cause:** {diagnosis.get("root_cause", "Unknown")}
**Fix Approach:** {diagnosis.get("fix_approach", "Not specified")}

{category_rules}

## Current File Contents

{file_context}

Return search/replace patches to fix this issue. The search strings must match the files exactly."""

    result = await claude_structured(
        prompt=prompt,
        schema=PATCH_SCHEMA,
        system=PATCH_SYSTEM + "\n" + BASE_CONSTRAINTS,
        model_tier="smart",
        max_tokens=4096,
    )

    if not result:
        logger.warning("Patch generation failed for: {}", title)
        return None

    logger.info("Generated {} patches for: {}", len(result.get("patches", [])), title)
    return result
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_patch_generator.py -v`
Expected: All 4 PASS

**Step 5: Commit**

```bash
git add app/services/patch_generator.py tests/test_patch_generator.py
git commit -m "feat: add patch_generator service using Claude API for self-heal fixes"
```

---

### Task 3: Rewrite execution_service.py (Claude API + fix queue)

**Files:**
- Modify: `app/services/execution_service.py` (full rewrite of `_run_fix` and `_subprocess_fix`)
- Modify: `tests/test_execution_service.py` (update mocks)

**Step 1: Update the test for the new flow**

Add to `tests/test_execution_service.py` — replace the existing `_run_fix` mock pattern. The mock target changes from `_run_fix` to `generate_patches`:

```python
# Add these imports at top:
# from unittest.mock import AsyncMock, patch, mock_open
# import json

class TestExecuteFixSuccess:
    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_successful_fix_writes_queue_file(self, mock_gen, db_session, diagnosed_ticket, tmp_path):
        mock_gen.return_value = {
            "patches": [{"file": "app/routers/vendors.py", "search": "old", "replace": "new", "explanation": "fix"}],
            "summary": "Fixed query",
        }
        with patch("app.services.execution_service.FIX_QUEUE_DIR", str(tmp_path)):
            result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert result["ok"] is True
        assert result["status"] == "fix_queued"
        # Verify JSON file was written
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["ticket_id"] == diagnosed_ticket.id
        assert len(data["patches"]) == 1

    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_successful_fix_emits_notification(self, mock_gen, db_session, diagnosed_ticket, tmp_path):
        mock_gen.return_value = {
            "patches": [{"file": "app/routers/vendors.py", "search": "old", "replace": "new", "explanation": "fix"}],
            "summary": "Done",
        }
        with patch("app.services.execution_service.FIX_QUEUE_DIR", str(tmp_path)):
            _run(execute_fix(diagnosed_ticket.id, db_session))
        notifs = (
            db_session.query(Notification)
            .filter_by(ticket_id=diagnosed_ticket.id, event_type="fixed")
            .all()
        )
        assert len(notifs) == 1


class TestExecuteFixFailure:
    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_patch_generation_fails(self, mock_gen, db_session, diagnosed_ticket):
        mock_gen.return_value = None
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "Patch generation failed" in result["error"]
        db_session.refresh(diagnosed_ticket)
        assert diagnosed_ticket.status == "diagnosed"

    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_empty_patches_fails(self, mock_gen, db_session, diagnosed_ticket):
        mock_gen.return_value = {"patches": [], "summary": "Nothing to fix"}
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "No patches generated" in result["error"]

    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_failed_fix_final_attempt_escalates(self, mock_gen, db_session, diagnosed_ticket):
        mock_gen.return_value = None
        with patch("app.services.execution_service.settings") as mock_settings:
            mock_settings.self_heal_max_iterations_low = 2
            mock_settings.self_heal_max_iterations_medium = 10
            mock_settings.self_heal_ticket_budget = 100.0
            mock_settings.self_heal_weekly_budget = 500.0
            diagnosed_ticket.iterations_used = 1
            db_session.commit()
            result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "escalated" in result["error"]
        db_session.refresh(diagnosed_ticket)
        assert diagnosed_ticket.status == "escalated"
```

**Step 2: Run tests to verify failures**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_execution_service.py -v`
Expected: New tests FAIL (generate_patches not imported, FIX_QUEUE_DIR not defined)

**Step 3: Rewrite execution_service.py**

Replace the entire file content of `app/services/execution_service.py`:

```python
"""Execution service — generates AI patches and queues them for host-side application.

Flow: validate → budget check → file lock → generate patches (Claude API) →
      write fix JSON to shared volume → host watcher applies and rebuilds.

Called by: routers/trouble_tickets.py
Depends on: services/cost_controller.py, services/patch_generator.py,
            services/trouble_ticket_service.py, services/notification_service.py
"""

import json
import os
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models.trouble_ticket import TroubleTicket
from app.services.cost_controller import check_budget, record_cost
from app.services.notification_service import create_notification
from app.services.patch_generator import generate_patches
from app.services.trouble_ticket_service import check_file_lock, update_ticket

FIX_QUEUE_DIR = "/app/fix_queue"


async def execute_fix(ticket_id: int, db: Session) -> dict:
    """Full execution pipeline for a diagnosed ticket.

    Generates patches via Claude API and writes a fix JSON to the shared queue.
    Returns: {ok, status, message} or {error, reason}.
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    if not ticket.diagnosis:
        return {"error": "Ticket not yet diagnosed"}

    if ticket.status not in ("diagnosed", "prompt_ready", "fix_queued"):
        return {"error": f"Ticket status '{ticket.status}' cannot be executed"}

    risk_tier = ticket.risk_tier or "medium"
    if risk_tier == "high":
        return {"error": "High-risk tickets require human intervention"}

    # Budget check
    budget = check_budget(db, ticket_id)
    if not budget["allowed"]:
        _escalate(db, ticket, budget["reason"])
        return {"error": budget["reason"]}

    # File lock check
    file_mapping = ticket.file_mapping or []
    if file_mapping:
        blocking = check_file_lock(db, file_mapping)
        if blocking and blocking.id != ticket_id:
            return {"error": f"File lock conflict with ticket #{blocking.id}"}

    # Max iterations check
    max_iter = settings.self_heal_max_iterations_low if risk_tier == "low" else settings.self_heal_max_iterations_medium
    current_iter = ticket.iterations_used or 0
    if current_iter >= max_iter:
        _escalate(db, ticket, f"Max iterations ({max_iter}) reached")
        return {"error": f"Max iterations ({max_iter}) reached — escalated"}

    # Mark as in-progress
    update_ticket(db, ticket_id, status="in_progress", iterations_used=current_iter + 1)

    # Generate patches via Claude API
    diagnosis = ticket.diagnosis.get("detailed") or ticket.diagnosis if isinstance(ticket.diagnosis, dict) else {}
    result = await generate_patches(
        title=ticket.title,
        diagnosis=diagnosis,
        category=ticket.category or "other",
        affected_files=ticket.file_mapping or [],
    )

    if not result:
        error_msg = "Patch generation failed"
        if current_iter + 1 >= max_iter:
            _escalate(db, ticket, f"{error_msg} after {current_iter + 1} iterations")
            return {"error": f"{error_msg} and escalated"}
        update_ticket(db, ticket_id, status="diagnosed")
        record_cost(db, ticket_id, 0.03)  # API call cost even on failure
        _notify(db, ticket, "failed", "Patch generation failed", error_msg)
        return {"error": error_msg}

    patches = result.get("patches", [])
    if not patches:
        update_ticket(db, ticket_id, status="diagnosed")
        _notify(db, ticket, "failed", "No patches generated", "Claude could not generate patches")
        return {"error": "No patches generated"}

    # Write fix JSON to queue
    fix_payload = {
        "ticket_id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "risk_tier": risk_tier,
        "category": ticket.category,
        "test_area": ticket.tested_area or ticket.category,
        "patches": patches,
        "summary": result.get("summary", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    _write_fix_queue(ticket.id, fix_payload)

    # Update ticket
    update_ticket(db, ticket_id, status="fix_queued", fix_branch=f"fix/ticket-{ticket.id}")
    record_cost(db, ticket_id, 0.05)  # Estimated Sonnet cost
    _notify(db, ticket, "fixed", "Fix generated", result.get("summary", "Patches queued for application"))
    logger.info("Ticket {} patches queued ({} files)", ticket_id, len(patches))

    return {"ok": True, "status": "fix_queued", "message": f"Generated {len(patches)} patches"}


def _write_fix_queue(ticket_id: int, payload: dict) -> None:
    """Write fix JSON to the shared queue directory."""
    os.makedirs(FIX_QUEUE_DIR, exist_ok=True)
    path = os.path.join(FIX_QUEUE_DIR, f"{ticket_id}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info("Fix payload written to {}", path)


def _escalate(db: Session, ticket: TroubleTicket, reason: str) -> None:
    """Escalate a ticket to human review."""
    update_ticket(db, ticket.id, status="escalated", resolution_notes=reason)
    _notify(db, ticket, "escalated", "Ticket escalated", reason)
    logger.warning("Ticket {} escalated: {}", ticket.id, reason)


def _notify(db: Session, ticket: TroubleTicket, event: str, title: str, body: str) -> None:
    """Send notification to ticket submitter."""
    if ticket.submitted_by:
        create_notification(
            db,
            user_id=ticket.submitted_by,
            event_type=event,
            title=f"Ticket #{ticket.id}: {title}",
            body=body,
            ticket_id=ticket.id,
        )
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_execution_service.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add app/services/execution_service.py tests/test_execution_service.py
git commit -m "feat: rewrite execution service — Claude API patches + fix queue"
```

---

### Task 4: Add verify-retest endpoint and service

**Files:**
- Modify: `app/services/rollback_service.py` (replace stub with real SiteTester retest)
- Modify: `app/routers/trouble_tickets.py` (add verify-retest endpoint)
- Modify: `tests/test_rollback_service.py` (test real verification)

**Step 1: Write failing tests**

Replace `tests/test_rollback_service.py` content:

```python
"""Tests for verify-retest service (post-fix Playwright verification).

Covers: area-scoped retest, ticket resolution on pass, regression ticket on fail.

Called by: pytest
Depends on: app.services.rollback_service
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.notification import Notification
from app.models.trouble_ticket import TroubleTicket
from app.services.rollback_service import verify_and_retest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def rb_user(db_session: Session) -> User:
    user = User(
        email="rollback@trioscs.com",
        name="Rollback User",
        role="admin",
        azure_id="test-rb-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def queued_ticket(db_session: Session, rb_user: User) -> TroubleTicket:
    ticket = TroubleTicket(
        ticket_number="TT-RB-001",
        submitted_by=rb_user.id,
        title="Fixed bug",
        description="Was broken, now fixed",
        status="fix_queued",
        risk_tier="low",
        category="ui",
        tested_area="tickets",
        diagnosis={"root_cause": "Query error"},
        file_mapping=["app/static/tickets.js"],
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


class TestVerifyAndRetest:
    @patch("app.services.rollback_service.SiteTester")
    def test_pass_resolves_ticket(self, MockTester, db_session, queued_ticket):
        instance = MockTester.return_value
        instance.run_full_sweep = AsyncMock(return_value=[])  # No issues
        instance.issues = []

        result = _run(verify_and_retest(
            queued_ticket.id, db_session, base_url="http://localhost:8000", session_cookie="test",
        ))
        assert result["passed"] is True
        db_session.refresh(queued_ticket)
        assert queued_ticket.status == "resolved"

    @patch("app.services.rollback_service.SiteTester")
    def test_fail_creates_regression_ticket(self, MockTester, db_session, queued_ticket):
        instance = MockTester.return_value
        instance.run_full_sweep = AsyncMock(return_value=[
            {"area": "tickets", "title": "Still broken", "description": "Error persists"},
        ])
        instance.issues = [{"area": "tickets", "title": "Still broken", "description": "Error persists"}]

        result = _run(verify_and_retest(
            queued_ticket.id, db_session, base_url="http://localhost:8000", session_cookie="test",
        ))
        assert result["passed"] is False
        assert result["regression_ticket_id"] is not None
        # Original ticket should be escalated
        db_session.refresh(queued_ticket)
        assert queued_ticket.status == "escalated"
        # Regression ticket should exist and link to parent
        child = db_session.get(TroubleTicket, result["regression_ticket_id"])
        assert child is not None
        assert child.parent_ticket_id == queued_ticket.id
        assert "regression" in child.title.lower() or "retest" in child.title.lower()

    def test_missing_ticket(self, db_session):
        result = _run(verify_and_retest(
            99999, db_session, base_url="http://localhost:8000", session_cookie="test",
        ))
        assert result["passed"] is False
        assert "not found" in result.get("error", "").lower()

    @patch("app.services.rollback_service.SiteTester")
    def test_pass_emits_notification(self, MockTester, db_session, queued_ticket, rb_user):
        instance = MockTester.return_value
        instance.run_full_sweep = AsyncMock(return_value=[])
        instance.issues = []

        _run(verify_and_retest(
            queued_ticket.id, db_session, base_url="http://localhost:8000", session_cookie="test",
        ))
        notifs = db_session.query(Notification).filter_by(
            ticket_id=queued_ticket.id, event_type="resolved",
        ).all()
        assert len(notifs) == 1
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_rollback_service.py -v`
Expected: FAIL — `verify_and_retest` not found

**Step 3: Rewrite rollback_service.py**

Replace `app/services/rollback_service.py`:

```python
"""Verify-retest service — post-fix Playwright verification for the self-heal pipeline.

After a fix is applied and the container rebuilt, this service runs SiteTester
on just the affected area. If issues persist, it creates a regression ticket
and escalates.

Called by: routers/trouble_tickets.py (verify-retest endpoint), host watcher script
Depends on: services/site_tester.py, services/trouble_ticket_service.py,
            services/notification_service.py
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.trouble_ticket import TroubleTicket
from app.services.notification_service import create_notification
from app.services.trouble_ticket_service import create_ticket, update_ticket


async def verify_and_retest(
    ticket_id: int,
    db: Session,
    *,
    base_url: str,
    session_cookie: str,
) -> dict:
    """Run SiteTester on the ticket's affected area and resolve or escalate.

    Returns: {passed: bool, issues: list, regression_ticket_id?: int, error?: str}
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"passed": False, "issues": [], "error": "Ticket not found"}

    test_area = ticket.tested_area or ticket.category or "search"

    # Find the matching area config from SiteTester
    from app.services.site_tester import SiteTester, TEST_AREAS

    area_config = next((a for a in TEST_AREAS if a["name"] == test_area), None)
    if not area_config:
        # Fall back to testing the area by name with a generic hash
        area_config = {"name": test_area, "hash": f"#view-{test_area}", "description": test_area}

    logger.info("Verify-retest: testing area '{}' for ticket {}", test_area, ticket_id)

    tester = SiteTester(base_url=base_url, session_cookie=session_cookie)

    try:
        await tester.run_full_sweep()
    except Exception as e:
        logger.error("Verify-retest sweep failed for ticket {}: {}", ticket_id, e)
        return {"passed": False, "issues": [str(e)], "error": str(e)}

    # Filter issues to just this area
    area_issues = [i for i in tester.issues if i.get("area") == test_area]

    if not area_issues:
        # Pass — resolve the ticket
        update_ticket(db, ticket_id, status="resolved", resolution_notes="Verified by automated retest")
        if ticket.submitted_by:
            create_notification(
                db,
                user_id=ticket.submitted_by,
                event_type="resolved",
                title=f"Ticket #{ticket_id}: Fix verified",
                body=f"Automated retest of '{test_area}' passed — ticket resolved.",
                ticket_id=ticket_id,
            )
        logger.info("Ticket {} verified — resolved", ticket_id)
        return {"passed": True, "issues": []}

    # Fail — create regression ticket and escalate original
    issue_desc = "\n".join(f"- {i['title']}: {i['description']}" for i in area_issues)
    regression = create_ticket(
        db=db,
        user_id=ticket.submitted_by or 1,
        title=f"Retest failed: {ticket.title[:150]}",
        description=f"Automated retest after fix for ticket #{ticket_id} found {len(area_issues)} issue(s):\n\n{issue_desc}",
        source="retest",
        current_view=test_area,
    )
    regression.parent_ticket_id = ticket_id
    regression.tested_area = test_area
    db.commit()

    update_ticket(
        db, ticket_id,
        status="escalated",
        resolution_notes=f"Retest failed: {len(area_issues)} issue(s) in '{test_area}'. Regression ticket #{regression.id}",
    )

    if ticket.submitted_by:
        create_notification(
            db,
            user_id=ticket.submitted_by,
            event_type="failed",
            title=f"Ticket #{ticket_id}: Retest failed",
            body=f"Fix did not resolve the issue. {len(area_issues)} problem(s) remain in '{test_area}'.",
            ticket_id=ticket_id,
        )

    logger.warning("Ticket {} retest failed — {} issues, regression ticket #{}", ticket_id, len(area_issues), regression.id)
    return {"passed": False, "issues": area_issues, "regression_ticket_id": regression.id}
```

**Step 4: Add the endpoint to trouble_tickets.py**

Add after the existing `/api/trouble-tickets/{id}/execute` endpoint in `app/routers/trouble_tickets.py`:

```python
@router.post("/api/trouble-tickets/{ticket_id}/verify-retest")
async def verify_retest(
    ticket_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Run SiteTester on the ticket's area and resolve or create regression ticket."""
    from app.services.rollback_service import verify_and_retest

    session_cookie = request.cookies.get("session", "")
    base_url = str(request.base_url).rstrip("/")
    return await verify_and_retest(
        ticket_id, db, base_url=base_url, session_cookie=session_cookie,
    )
```

**Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_rollback_service.py -v`
Expected: All 4 PASS

**Step 6: Commit**

```bash
git add app/services/rollback_service.py app/routers/trouble_tickets.py tests/test_rollback_service.py
git commit -m "feat: add verify-retest endpoint — Playwright area retest with regression tickets"
```

---

### Task 5: Create host watcher script

**Files:**
- Create: `scripts/self_heal_watcher.sh`
- Create: `scripts/apply_patches.py`

**Step 1: Create the patch applier helper**

Create `scripts/apply_patches.py`:

```python
#!/usr/bin/env python3
"""Apply search/replace patches from a self-heal fix JSON file.

Usage: python3 scripts/apply_patches.py fix_queue/123.json

Called by: scripts/self_heal_watcher.sh
Depends on: fix queue JSON format from execution_service.py
"""

import json
import sys


def apply_patches(fix_file: str) -> bool:
    """Apply all patches from a fix JSON file. Returns True if all succeeded."""
    with open(fix_file) as f:
        data = json.load(f)

    patches = data.get("patches", [])
    if not patches:
        print("No patches to apply")
        return False

    success = True
    for i, patch in enumerate(patches):
        fpath = patch["file"]
        search = patch["search"]
        replace = patch["replace"]

        try:
            with open(fpath) as f:
                content = f.read()

            if search not in content:
                print(f"WARN: patch {i+1} search string not found in {fpath}")
                success = False
                continue

            new_content = content.replace(search, replace, 1)
            with open(fpath, "w") as f:
                f.write(new_content)

            print(f"OK: patch {i+1} applied to {fpath}")
        except Exception as e:
            print(f"ERR: patch {i+1} failed on {fpath}: {e}")
            success = False

    return success


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/apply_patches.py <fix_file.json>")
        sys.exit(1)
    ok = apply_patches(sys.argv[1])
    sys.exit(0 if ok else 1)
```

**Step 2: Create the watcher script**

Create `scripts/self_heal_watcher.sh`:

```bash
#!/usr/bin/env bash
# Self-heal watcher — applies fix patches, rebuilds, and triggers retest.
#
# Watches fix_queue/ for new JSON files, applies patches to the source tree,
# rebuilds the Docker container, and calls the verify-retest endpoint.
#
# Usage: Run via cron every 2 minutes, or: bash scripts/self_heal_watcher.sh
# Depends on: scripts/apply_patches.py, docker compose, curl

set -euo pipefail

PROJ_DIR="/root/availai"
QUEUE_DIR="${PROJ_DIR}/fix_queue"
APPLIED_DIR="${QUEUE_DIR}/applied"
FAILED_DIR="${QUEUE_DIR}/failed"
APP_URL="http://localhost:8000"
LOG_FILE="/var/log/avail/self_heal_watcher.log"

mkdir -p "$APPLIED_DIR" "$FAILED_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG_FILE"; }

# Get admin session cookie for API calls
get_session() {
    # Read from .env or use the first active session
    curl -sf "${APP_URL}/health" > /dev/null 2>&1 || { log "App not healthy, skipping"; exit 0; }
}

process_fix() {
    local fix_file="$1"
    local ticket_id
    ticket_id=$(python3 -c "import json; print(json.load(open('$fix_file'))['ticket_id'])")
    local branch="fix/ticket-${ticket_id}"

    log "Processing fix for ticket ${ticket_id}"

    cd "$PROJ_DIR"

    # Create fix branch from current HEAD
    git checkout -b "$branch" 2>/dev/null || git checkout "$branch"

    # Apply patches
    if ! python3 scripts/apply_patches.py "$fix_file"; then
        log "FAIL: Patches did not apply cleanly for ticket ${ticket_id}"
        git checkout main 2>/dev/null || git checkout master
        git branch -D "$branch" 2>/dev/null || true
        mv "$fix_file" "$FAILED_DIR/"
        return 1
    fi

    # Commit the changes
    git add -A
    git commit -m "fix(self-heal): ticket #${ticket_id} — automated patch

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>" || {
        log "Nothing to commit for ticket ${ticket_id}"
        git checkout main 2>/dev/null || git checkout master
        git branch -D "$branch" 2>/dev/null || true
        mv "$fix_file" "$FAILED_DIR/"
        return 1
    }

    # Merge to main
    git checkout main 2>/dev/null || git checkout master
    git merge --no-ff "$branch" -m "merge: self-heal fix for ticket #${ticket_id}"

    # Rebuild and deploy
    log "Rebuilding container..."
    docker compose up -d --build

    # Wait for health check
    for i in $(seq 1 30); do
        if curl -sf "${APP_URL}/health" > /dev/null 2>&1; then
            log "Container healthy after rebuild"
            break
        fi
        sleep 2
    done

    # Trigger verify-retest (uses internal endpoint — no auth needed from localhost)
    # The endpoint requires admin auth, so we call it via the app's internal task
    log "Triggering verify-retest for ticket ${ticket_id}"
    local retest_result
    retest_result=$(curl -sf -X POST "${APP_URL}/api/internal/verify-retest/${ticket_id}" 2>&1) || true

    if echo "$retest_result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('passed') else 1)" 2>/dev/null; then
        log "PASS: Ticket ${ticket_id} verified successfully"
        mv "$fix_file" "$APPLIED_DIR/"
    else
        log "FAIL: Retest failed for ticket ${ticket_id} — reverting"
        git revert HEAD --no-edit
        docker compose up -d --build
        # Wait for healthy after revert
        for i in $(seq 1 30); do
            curl -sf "${APP_URL}/health" > /dev/null 2>&1 && break
            sleep 2
        done
        mv "$fix_file" "$FAILED_DIR/"
    fi

    # Clean up branch
    git branch -d "$branch" 2>/dev/null || true
}

# Main loop: process all pending fix files
get_session

for fix_file in "$QUEUE_DIR"/*.json; do
    [ -f "$fix_file" ] || continue
    process_fix "$fix_file" || log "Error processing $fix_file"
done

log "Watcher run complete"
```

**Step 3: Make executable**

Run:
```bash
chmod +x scripts/self_heal_watcher.sh scripts/apply_patches.py
```

**Step 4: Commit**

```bash
git add scripts/self_heal_watcher.sh scripts/apply_patches.py
git commit -m "feat: add host watcher script for self-heal fix application and retest"
```

---

### Task 6: Add internal verify-retest endpoint (no auth for watcher)

**Files:**
- Modify: `app/routers/trouble_tickets.py`

**Step 1: Add the internal endpoint**

Add to `app/routers/trouble_tickets.py`, near the other verify-retest endpoint:

```python
@router.post("/api/internal/verify-retest/{ticket_id}")
async def internal_verify_retest(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Internal endpoint for host watcher — localhost only, no auth."""
    # Only allow from localhost
    client = request.client
    if not client or client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Internal endpoint — localhost only")

    from app.services.rollback_service import verify_and_retest

    session_cookie = request.cookies.get("session", "")
    base_url = str(request.base_url).rstrip("/")
    return await verify_and_retest(
        ticket_id, db, base_url=base_url, session_cookie=session_cookie,
    )
```

**Step 2: Commit**

```bash
git add app/routers/trouble_tickets.py
git commit -m "feat: add internal verify-retest endpoint for localhost watcher"
```

---

### Task 7: Update auto_process_ticket to use new execution flow

**Files:**
- Modify: `app/services/trouble_ticket_service.py` (update auto_process_ticket)

**Step 1: Read current auto_process_ticket**

The function at line 338 calls `execute_fix` which now writes to the queue instead of running a subprocess. The flow is:

1. `auto_process_ticket` → `diagnose_full` → `execute_fix` → writes JSON to queue
2. Host watcher picks up JSON → applies → rebuilds → calls verify-retest

The existing `auto_process_ticket` already calls `execute_fix`, so it should work with the new implementation. Verify the `fix_queued` status is accepted:

**Step 2: Update the status check in auto_process_ticket**

In `app/services/trouble_ticket_service.py`, find the `auto_process_ticket` function (line 338) and update the success log message:

Change line 376 from:
```python
logger.info("Ticket {} auto-processed: diagnosed and fix executed (risk={})", ticket_id, risk_tier)
```
To:
```python
logger.info("Ticket {} auto-processed: diagnosed and fix queued (risk={})", ticket_id, risk_tier)
```

**Step 3: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All tests pass

**Step 4: Commit**

```bash
git add app/services/trouble_ticket_service.py
git commit -m "fix: update auto_process_ticket log message for new queue-based flow"
```

---

### Task 8: Add cron job for watcher

**Files:**
- Create: `scripts/install_watcher_cron.sh`

**Step 1: Create installer script**

Create `scripts/install_watcher_cron.sh`:

```bash
#!/usr/bin/env bash
# Install cron job for the self-heal watcher (runs every 2 minutes).
set -euo pipefail

CRON_LINE="*/2 * * * * /bin/bash /root/availai/scripts/self_heal_watcher.sh >> /var/log/avail/self_heal_watcher.log 2>&1"

# Check if already installed
if crontab -l 2>/dev/null | grep -q "self_heal_watcher"; then
    echo "Watcher cron already installed"
    exit 0
fi

# Add to crontab
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo "Watcher cron installed: runs every 2 minutes"
```

**Step 2: Make executable and commit**

```bash
chmod +x scripts/install_watcher_cron.sh
git add scripts/install_watcher_cron.sh
git commit -m "feat: add cron installer for self-heal watcher"
```

---

### Task 9: Run full test suite + coverage check

**Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All tests pass

**Step 2: Check coverage**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: Coverage should not drop below current level. New files (`patch_generator.py`) should show coverage.

**Step 3: Fix any failures**

If tests fail, diagnose and fix before proceeding.

---

### Task 10: Deploy and verify

**Step 1: Rebuild and deploy**

```bash
cd /root/availai
docker compose up -d --build
```

**Step 2: Check logs for clean startup**

```bash
docker compose logs app --tail 50
```

**Step 3: Install the watcher cron**

```bash
bash scripts/install_watcher_cron.sh
```

**Step 4: Verify fix queue volume is mounted**

```bash
docker compose exec app ls -la /app/fix_queue/
```

**Step 5: Test the full loop manually**

Create a test ticket via the UI, watch the logs:
```bash
docker compose logs -f app | grep -i "ticket\|patch\|queue\|retest"
```

**Step 6: Commit any final adjustments**

```bash
git add -A
git commit -m "deploy: self-heal loop v2 — Claude API patches + host watcher + verify-retest"
```
