# Find Trouble Test Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the Find Trouble button to a persistent in-app loop that runs Playwright sweep + Claude agent deep testing, creates deduplicated tickets, auto-heals low/medium risk, and repeats until clean.

**Architecture:** A `FindTroubleService` singleton manages the loop as a background asyncio task. Phase 1 uses the existing `SiteTester` for fast button-click sweep. Phase 2 shells out to `scripts/test-site.sh` for deep Claude agent testing. SSE endpoint streams live progress to the frontend. Ticket creation deduplicates against open tickets in the same area.

**Tech Stack:** FastAPI (SSE via StreamingResponse), asyncio, Playwright (patchright), existing SiteTester + test-site.sh

---

### Task 1: Fix 3 Broken Test Imports

**Files:**
- Modify: `tests/test_rollback_service.py:19`
- Modify: `tests/test_selfheal_integration.py:24`
- Modify: `tests/test_sighting_cache.py:18-22`

**Step 1: Fix test_rollback_service.py**

The test imports `check_post_fix_health` which was replaced by `verify_and_retest`. Update the import and all references:

```python
# line 19: change
from app.services.rollback_service import check_post_fix_health
# to
from app.services.rollback_service import verify_and_retest
```

Then update every test function that calls `check_post_fix_health` to call `verify_and_retest` instead. The function signature is:
```python
async def verify_and_retest(ticket_id: int, db: Session, *, base_url: str = "http://localhost:8000", session_cookie: str | None = None) -> dict
```

Returns `{"ok": True, "status": "resolved"}` on pass, `{"error": "..."}` on failure, or escalation dict on retest failure.

**Step 2: Fix test_selfheal_integration.py**

Same issue -- imports `check_post_fix_health` from rollback_service. Apply same fix as step 1.

**Step 3: Fix test_sighting_cache.py**

The test imports `_get_cached_sources`, `_load_cached_sightings`, `_sighting_to_connector_dict` from `app.search_service` but these private functions were removed. Check if the functions were renamed or moved:

Run: `grep -rn "_get_cached_sources\|_load_cached_sightings\|_sighting_to_connector_dict" app/`

If no matches: delete `tests/test_sighting_cache.py` entirely (tests dead code).

**Step 4: Verify tests collect**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --collect-only 2>&1 | tail -5`
Expected: `collected XXXX items` with 0 errors.

**Step 5: Commit**

```bash
git add tests/test_rollback_service.py tests/test_selfheal_integration.py tests/test_sighting_cache.py
git commit -m "fix: repair broken test imports (rollback_service, sighting_cache)"
```

---

### Task 2: Add Ticket Dedup to `create_tickets_from_issues`

**Files:**
- Modify: `app/services/site_tester.py:230-258`
- Test: `tests/test_find_trouble.py` (create)

**Step 1: Write failing test**

Create `tests/test_find_trouble.py`:

```python
"""Tests for Find Trouble service -- dedup, loop manager, endpoints.

Called by: pytest
Depends on: app.services.site_tester, app.services.find_trouble_service
"""

import asyncio
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket


@pytest.fixture()
def ft_user(db_session: Session) -> User:
    user = User(email="ft@test.com", name="FT Tester", role="admin")
    db_session.add(user)
    db_session.commit()
    return user


def test_dedup_skips_existing_open_ticket(db_session: Session, ft_user: User):
    """create_tickets_from_issues should skip issues matching an open ticket in same area."""
    from app.services.site_tester import create_tickets_from_issues

    existing = TroubleTicket(
        title="Console errors on load: search",
        description="2 console error(s) on initial load",
        status="submitted",
        submitted_by=ft_user.id,
        source="playwright",
        current_view="search",
    )
    db_session.add(existing)
    db_session.commit()

    issues = [{
        "area": "search",
        "title": "Console errors on load: search",
        "description": "3 console error(s) on initial load",
        "url": "http://localhost:8000/#view-sourcing",
        "console_errors": ["[error] something"],
        "network_errors": [],
    }]

    loop = asyncio.get_event_loop()
    count = loop.run_until_complete(create_tickets_from_issues(issues, db_session))
    assert count == 0


def test_dedup_creates_ticket_for_new_area(db_session: Session, ft_user: User):
    """create_tickets_from_issues should create tickets for areas with no open tickets."""
    from app.services.site_tester import create_tickets_from_issues

    issues = [{
        "area": "rfq",
        "title": "Network error on load: rfq",
        "description": "1 failed network request(s)",
        "url": "http://localhost:8000/#view-rfq",
        "console_errors": [],
        "network_errors": [{"url": "/api/rfq", "failure": "net::ERR"}],
    }]

    loop = asyncio.get_event_loop()
    count = loop.run_until_complete(create_tickets_from_issues(issues, db_session))
    assert count == 1
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_find_trouble.py::test_dedup_skips_existing_open_ticket -v`
Expected: FAIL (count is 1, not 0)

**Step 3: Implement dedup in create_tickets_from_issues**

Edit `app/services/site_tester.py`, replace `create_tickets_from_issues` (lines 230-258):

```python
async def create_tickets_from_issues(issues: list[dict[str, Any]], db: Any) -> int:
    """Create a TroubleTicket for each issue found by the site tester.

    Deduplicates: skips issues where an open ticket already exists
    in the same area with the same title prefix (first 80 chars).

    Returns the number of tickets created.
    """
    from app.models.trouble_ticket import TroubleTicket
    from app.services.trouble_ticket_service import create_ticket

    open_statuses = ("submitted", "diagnosed", "escalated", "in_progress", "open", "fix_queued")
    existing = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.status.in_(open_statuses))
        .filter(TroubleTicket.source == "playwright")
        .all()
    )
    seen = {(t.current_view or "", (t.title or "")[:80]) for t in existing}

    count = 0
    for issue in issues:
        area = issue.get("area", "")
        title = issue.get("title", "")[:200]
        dedup_key = (area, title[:80])

        if dedup_key in seen:
            logger.debug("site_tester: skipping duplicate '{}' in '{}'", title[:60], area)
            continue

        try:
            create_ticket(
                db=db,
                user_id=1,
                title=title,
                description=issue["description"],
                current_page=issue.get("url"),
                source="playwright",
                console_errors="\n".join(issue.get("console_errors", [])) or None,
                current_view=area,
            )
            seen.add(dedup_key)
            count += 1
        except Exception as exc:
            logger.warning("site_tester: failed to create ticket for '{}': {}", title, exc)

    if count:
        db.commit()
        logger.info("site_tester: created {} trouble tickets from sweep issues", count)

    return count
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_find_trouble.py -v`
Expected: both tests PASS

**Step 5: Commit**

```bash
git add app/services/site_tester.py tests/test_find_trouble.py
git commit -m "feat: add ticket dedup to create_tickets_from_issues"
```

---

### Task 3: Create Find Trouble Service

**Files:**
- Create: `app/services/find_trouble_service.py`
- Test: `tests/test_find_trouble.py` (append)

**Step 1: Write failing test**

Append to `tests/test_find_trouble.py`:

```python
def test_find_trouble_service_singleton():
    """Only one Find Trouble job can run at a time."""
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    assert svc.active_job is None
    assert svc.is_running is False


def test_find_trouble_service_status_when_not_running():
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    status = svc.get_status()
    assert status["running"] is False
    assert status["round"] == 0


def test_find_trouble_service_cannot_start_twice():
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    svc.active_job = {"running": True, "cancel": False}
    result = svc.try_start("http://localhost", "cookie")
    assert result is None
    svc.active_job = None
```

**Step 2: Run to verify failure**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_find_trouble.py::test_find_trouble_service_singleton -v`
Expected: FAIL (module not found)

**Step 3: Create find_trouble_service.py**

Create `app/services/find_trouble_service.py` with the full service class. Key implementation details:

- Singleton pattern via module-level `_service` instance
- `try_start()` creates asyncio task running `_run_loop()`
- `_run_loop()` iterates rounds: Phase 1 (Playwright), Phase 2 (deep test subprocess), ticket creation, auto-heal, wait for fixes
- `_run_playwright_sweep()` uses existing `SiteTester`
- `_run_deep_test()` runs `scripts/test-site.sh` via `asyncio.create_subprocess_exec("bash", script_path, ...)`
- `_emit()` appends to event list for SSE consumption
- `stop()` sets cancel flag, checked between phases
- `consume_events(after=N)` returns events after index N for SSE streaming

See design doc for full class implementation.

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_find_trouble.py -v`
Expected: all 5 tests PASS

**Step 5: Commit**

```bash
git add app/services/find_trouble_service.py tests/test_find_trouble.py
git commit -m "feat: FindTroubleService -- loop manager with sweep, deep test, auto-heal"
```

---

### Task 4: Add Find Trouble Endpoints

**Files:**
- Modify: `app/routers/trouble_tickets.py` (add 4 endpoints BEFORE `POST /api/trouble-tickets`)
- Test: `tests/test_find_trouble.py` (append)

**Step 1: Write failing tests**

Append to `tests/test_find_trouble.py` (adjust fixtures to match conftest.py patterns):

```python
def test_find_trouble_start_endpoint(client, admin_headers):
    with patch("app.routers.trouble_tickets.get_find_trouble_service") as mock_svc:
        mock_instance = MagicMock()
        mock_instance.try_start.return_value = {"status": "started"}
        mock_svc.return_value = mock_instance
        resp = client.post("/api/trouble-tickets/find-trouble", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"


def test_find_trouble_stop_endpoint(client, admin_headers):
    with patch("app.routers.trouble_tickets.get_find_trouble_service") as mock_svc:
        mock_instance = MagicMock()
        mock_instance.stop.return_value = True
        mock_svc.return_value = mock_instance
        resp = client.post("/api/trouble-tickets/find-trouble/stop", headers=admin_headers)
        assert resp.status_code == 200


def test_find_trouble_prompts_endpoint(client, admin_headers):
    resp = client.get("/api/trouble-tickets/find-trouble/prompts", headers=admin_headers)
    assert resp.status_code == 200
    assert len(resp.json()["prompts"]) >= 17
```

Note: Check conftest.py for existing `client` and `admin_headers` fixtures. If they use different names, adjust accordingly.

**Step 2: Add endpoints**

Insert 4 endpoints in `trouble_tickets.py` BEFORE the `@router.post("/api/trouble-tickets")` line:

1. `POST /api/trouble-tickets/find-trouble` -- start loop
2. `POST /api/trouble-tickets/find-trouble/stop` -- cancel
3. `GET /api/trouble-tickets/find-trouble/stream` -- SSE event stream
4. `GET /api/trouble-tickets/find-trouble/prompts` -- agent prompts

The SSE endpoint uses `StreamingResponse` with `text/event-stream` media type. It polls `svc.consume_events(after=cursor)` every 1s and yields `data: {json}\n\n` lines.

**Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_find_trouble.py -v`
Expected: all tests PASS

**Step 4: Commit**

```bash
git add app/routers/trouble_tickets.py tests/test_find_trouble.py
git commit -m "feat: Find Trouble endpoints -- start, stop, SSE stream, prompts"
```

---

### Task 5: Frontend -- Find Trouble Button + Progress UI

**Files:**
- Modify: `app/static/tickets.js:213-222` (admin dashboard header)
- Modify: `app/static/tickets.js` (append before keyboard shortcuts at line 874)

**Step 1: Add Find Trouble button to admin dashboard header**

In `renderAdminDashboard` (lines 215-222), add a red "Find Trouble" button next to "+ New Ticket" in the header div.

**Step 2: Add Find Trouble functions before keyboard shortcuts section**

Key functions to add:
- `startFindTrouble(container)` -- confirmation dialog, POST to start endpoint, show progress
- `stopFindTrouble()` -- POST to stop endpoint
- `updateFindTroubleBtn()` -- toggle button text/style between "Find Trouble" and "Running... (Stop)"
- `showFindTroubleProgress(container)` -- render progress panel with area grid, stats, event log
- `connectFindTroubleSSE(container)` -- EventSource connection to SSE stream, updates area grid colors (green=pass, red=fail, amber=timeout), appends to event log, updates stats counters
- CSS animation `@keyframes ftPulse` for pulsing button

Area grid: 17 cells in a CSS grid, each showing area name, colored by status.
Event log: scrollable monospace div showing timestamped events.
Auto-refresh: when loop completes, re-render admin dashboard after 2s delay.

**Step 3: Manual test**

Start app, log in as admin, navigate to Tickets, verify button + UI.

**Step 4: Commit**

```bash
git add app/static/tickets.js
git commit -m "feat: Find Trouble button + live progress UI with SSE streaming"
```

---

### Task 6: Restore Scripts + Commit

**Files:**
- Already restored: `scripts/agent-prompts/`, `scripts/test-site.sh`, `scripts/test-site-loop.sh`, `scripts/post-deploy.sh`

**Step 1: Commit**

```bash
git add scripts/agent-prompts/ scripts/test-site.sh scripts/test-site-loop.sh scripts/post-deploy.sh
git commit -m "restore: agent-prompts, test-site.sh, loop runner, post-deploy from deep-cleaning"
```

---

### Task 7: Full Test Suite + Coverage Check

**Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -20`
Expected: 0 collection errors, all tests pass

**Step 2: Coverage check**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -20`

**Step 3: Fix any failures, commit**

---

### Task 8: Deploy + Smoke Test

**Step 1:** `cd /root/availai && docker compose up -d --build`

**Step 2:** `docker compose logs -f app 2>&1 | head -30` -- verify clean startup

**Step 3:** Manual smoke test:
- Log in as admin, go to Tickets
- Verify red "Find Trouble" button visible
- Click, confirm dialog, watch progress panel
- Test Stop button
- After completion, verify new tickets appear in list

**Step 4:** Check DB for new tickets:
```sql
SELECT status, COUNT(*) FROM trouble_tickets
WHERE source='playwright' AND created_at > NOW() - INTERVAL '1 hour'
GROUP BY status;
```
