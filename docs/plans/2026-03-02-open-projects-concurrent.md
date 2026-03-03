# Open Projects — Concurrent Execution Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:dispatching-parallel-agents to run Streams A–E concurrently, then superpowers:executing-plans for Phase 2 integration.

**Goal:** Close all open projects in parallel — scheduler refactor, test coverage, infrastructure hardening, Vibe/Explorium integration, and frontend fixes.

**Architecture:** 5 independent streams with no shared file conflicts, followed by a sequential integration phase. Each stream is a self-contained task list that can be executed by a subagent in its own worktree.

**Tech Stack:** Python/FastAPI/SQLAlchemy, Jinja2/vanilla JS, Docker/Caddy, pytest

**Status of open items:**
- ~~Apollo Phase 2~~ — COMPLETE (merged on main, commit `1e8a2cf`)
- ~~RFQ UX Improvements~~ — BLOCKED (needs user requirements, skip for now)

---

## Phase 1: Five Parallel Streams

### Stream A: Scheduler Refactor

**Goal:** Split `app/scheduler.py` (2,374 lines, 54 job functions) into domain modules under `app/jobs/`.

**Files:**
- Modify: `app/scheduler.py` (slim to ~80 lines: imports, scheduler instance, configure_scheduler re-export)
- Create: `app/jobs/__init__.py`
- Create: `app/jobs/core_jobs.py` — auto_archive, token_refresh, inbox_scan, batch_results, webhook_subscriptions
- Create: `app/jobs/email_jobs.py` — contacts_sync, ownership_sweep, site_ownership_sweep, deep_email_mining, contact_scoring, contact_status_compute, email_health_update, calendar_scan, email_reverification, mine_vendor_contacts, scan_outbound_rfqs, compute_vendor_scores, sync_user_contacts
- Create: `app/jobs/enrichment_jobs.py` — deep_enrichment, monthly_enrichment_refresh, customer_enrichment_sweep, engagement_scoring
- Create: `app/jobs/inventory_jobs.py` — po_verification, stock_autocomplete, scan_stock_list_attachments, download_and_import_stock_list, parse_stock_file
- Create: `app/jobs/prospecting_jobs.py` — pool_health_report, discover_prospects, enrich_pool, find_contacts, refresh_scores, expire_and_resurface
- Create: `app/jobs/tagging_jobs.py` — connector_enrichment, internal_boost, prefix_backfill, sighting_mining, nexar_validate, material_enrichment, tagging_backfill
- Create: `app/jobs/offers_jobs.py` — proactive_matching, proactive_offer_expiry, flag_stale_offers, performance_tracking
- Create: `app/jobs/maintenance_jobs.py` — cache_cleanup, auto_dedup, reset_connector_errors, auto_attribute_activities, integrity_check
- Create: `app/jobs/health_jobs.py` — health_ping, health_deep, cleanup_usage_log, reset_monthly_usage
- Create: `app/jobs/selfheal_jobs.py` — self_heal_weekly_report, self_heal_auto_close
- Modify: `app/main.py` (only if scheduler import path changes)
- Modify: any tests importing from `app.scheduler`

**Step 1: Create `app/jobs/__init__.py`**

```python
"""Background job modules — domain-organized scheduler tasks.

Each module groups related job functions by domain. All jobs are registered
via configure_scheduler() which delegates to per-domain register functions.

Called by: app/scheduler.py
Depends on: app/services/*, app/models/*, app/database
"""

from .core_jobs import register_core_jobs
from .email_jobs import register_email_jobs
from .enrichment_jobs import register_enrichment_jobs
from .inventory_jobs import register_inventory_jobs
from .prospecting_jobs import register_prospecting_jobs
from .tagging_jobs import register_tagging_jobs
from .offers_jobs import register_offers_jobs
from .maintenance_jobs import register_maintenance_jobs
from .health_jobs import register_health_jobs
from .selfheal_jobs import register_selfheal_jobs


def register_all_jobs(scheduler, settings):
    """Register all background jobs with the APScheduler instance."""
    register_core_jobs(scheduler, settings)
    register_email_jobs(scheduler, settings)
    register_enrichment_jobs(scheduler, settings)
    register_inventory_jobs(scheduler, settings)
    register_prospecting_jobs(scheduler, settings)
    register_tagging_jobs(scheduler, settings)
    register_offers_jobs(scheduler, settings)
    register_maintenance_jobs(scheduler, settings)
    register_health_jobs(scheduler, settings)
    register_selfheal_jobs(scheduler, settings)
```

**Step 2: For each domain module**, extract the relevant job functions + their `add_job()` registrations from `scheduler.py`. Each module follows this pattern:

```python
"""[Domain] background jobs.

Called by: app/jobs/__init__.py via register_[domain]_jobs()
Depends on: app/services/[relevant_service].py, app/database
"""
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from ..scheduler import _traced_job  # keep decorator in scheduler.py


def register_[domain]_jobs(scheduler, settings):
    scheduler.add_job(_job_foo, IntervalTrigger(...), id="foo", name="...")
    # ...

@_traced_job
async def _job_foo():
    # exact code from scheduler.py
    ...
```

**Step 3: Slim `app/scheduler.py`** to:
- `_traced_job` decorator (keep here, exported to job modules)
- `scheduler` global instance
- `configure_scheduler()` that calls `register_all_jobs(scheduler, settings)`
- Token management re-exports (lines 67-68)

**Step 4: Update any test imports** — grep for `from app.scheduler import` and `from app import scheduler` in tests/

**Step 5: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

**Step 6: Run coverage check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

**Step 7: Commit**

```bash
git add app/jobs/ app/scheduler.py
git commit -m "refactor: split scheduler.py into app/jobs/ domain modules"
```

---

### Stream B: Test Coverage & Stability

**Goal:** Fix 4 flaky tests, add buyplan_v3_notifications tests (19% → 100%), close remaining coverage gaps.

**Files:**
- Modify: existing test files with flaky tests
- Create: `tests/test_buyplan_v3_notifications.py`
- Create: additional test files as needed for coverage gaps

**Task B1: Fix 4 flaky order-dependent tests**

**Step 1: Identify the flaky tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v 2>&1 | grep -i "FAIL\|ERROR" | head -20
```

Also check: `test_delete_requirement` and `TestAiGate` tests (3 tests).

**Step 2: Fix `test_delete_requirement`** — add fixture isolation for DB state. Ensure each test creates its own requirement rather than depending on insertion order.

**Step 3: Fix `TestAiGate` (3 tests)** — add autouse fixture that resets `_last_api_failure` and `_classification_cache` between tests:

```python
@pytest.fixture(autouse=True)
def _reset_ai_gate(self):
    from app.services.tagging_ai import _classification_cache
    _classification_cache.clear()
    # Reset any module-level state
    import app.services.tagging_ai as mod
    mod._last_api_failure = None
    yield
```

**Step 4: Verify all 4 pass in randomized full-suite run**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v -p random
```

**Step 5: Commit**

```bash
git add tests/
git commit -m "fix: resolve 4 flaky order-dependent tests with proper isolation"
```

**Task B2: buyplan_v3_notifications tests (19% → 100%)**

**Step 1: Read the module** — `app/services/buyplan_v3_notifications.py`

**Step 2: Write comprehensive tests** in `tests/test_buyplan_v3_notifications.py`:
- Mock `get_valid_token()`, `GraphClient.post_json()`, Teams webhook/DM
- Test each `notify_v3_*` function (submitted, approved, rejected, so_verified, so_rejected, issue_flagged, po_confirmed, completed)
- Test `run_v3_notify_bg` fire-and-forget wrapper
- Test `_plan_context`, `_lines_html`, `_wrap_email` helpers
- Test error paths (token failure, Graph API failure, missing config)

**Step 3: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_v3_notifications.py -v
```

**Step 4: Verify coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_v3_notifications.py --cov=app/services/buyplan_v3_notifications --cov-report=term-missing -q
```

**Step 5: Commit**

```bash
git add tests/test_buyplan_v3_notifications.py
git commit -m "test: add comprehensive buyplan_v3_notifications tests (19% → 100%)"
```

**Task B3: Close remaining coverage gaps**

**Step 1: Run full coverage report to identify gaps**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | grep -E "^\s*app/" | awk -F' ' '$NF < 95 {print}' | sort -t' ' -k4 -n
```

**Step 2: Write tests for each file below 95%** — prioritize by impact:
- `proactive_matching.py` (83%)
- `startup.py` (88%)
- `prospect_contacts.py` (93%)
- `buy_plan_v3_service.py` (93%)
- `nc_worker/worker.py` (94%)
- `prospect_scoring.py` (94%)

**Step 3: For each module** — read uncovered lines, write targeted tests that exercise those branches.

**Step 4: Run full suite + coverage after each module**

**Step 5: Commit per module or batch**

```bash
git commit -m "test: close coverage gaps — [module] to 100%"
```

---

### Stream C: Infrastructure Hardening

**Goal:** Tighten CSP (remove unsafe-inline), add pagination guards to unbounded .all() queries, improve Docker config.

**Files:**
- Modify: `Caddyfile`
- Modify: `app/templates/index.html` (nonce support)
- Modify: `app/main.py` or `app/dependencies.py` (nonce middleware)
- Modify: `app/routers/crm/enrichment.py:244`, `app/routers/crm/buy_plans_v3.py:212`
- Modify: `docker-compose.yml`

**Task C1: CSP nonce-based inline scripts**

**Step 1: Read current template and identify inline scripts/styles**

```bash
grep -n '<script' app/templates/index.html | head -20
grep -n 'style=' app/templates/index.html | head -10
```

**Step 2: Add CSP nonce middleware** — generate a random nonce per request, pass to template context:

```python
# In app/dependencies.py or a new middleware
import secrets

def generate_csp_nonce():
    return secrets.token_urlsafe(16)
```

**Step 3: Update Jinja2 templates** — add `nonce="{{ csp_nonce }}"` to all `<script>` and `<style>` tags.

**Step 4: Update Caddyfile** — replace `'unsafe-inline'` with `'nonce-{header.X-CSP-Nonce}'` or use a Caddy snippet to inject the nonce.

**Important consideration:** Caddy doesn't natively support per-request nonces in CSP headers. The recommended approach is:
- Option A: Generate nonce in FastAPI middleware, set it as a response header, reference in Caddy — BUT Caddy can't read response headers for request CSP.
- Option B: Move CSP header to FastAPI middleware instead of Caddyfile (RECOMMENDED — gives full control).

**Step 5: Move CSP to FastAPI middleware**

```python
# In app/main.py, add middleware
@app.middleware("http")
async def csp_middleware(request, call_next):
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    csp = (
        f"default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://cdnjs.cloudflare.com; "
        f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com; "
        f"font-src 'self' https://fonts.gstatic.com; "
        f"img-src 'self' data:; "
        f"connect-src 'self'"
    )
    response.headers["Content-Security-Policy"] = csp
    return response
```

**Step 6: Remove CSP line from Caddyfile**

**Step 7: Test by deploying locally and checking browser console for CSP violations**

**Step 8: Commit**

```bash
git add app/main.py app/templates/index.html Caddyfile
git commit -m "security: replace unsafe-inline CSP with nonce-based policy"
```

**Task C2: Pagination guards on unbounded .all() queries**

**Step 1: Fix high-concern queries** identified in audit:

| File | Line | Fix |
|------|------|-----|
| `app/routers/crm/enrichment.py` | 244 | `db.query(User).all()` — User table is small (~20), acceptable. Add comment: `# Admin-only; User table bounded by org size` |
| `app/routers/crm/buy_plans_v3.py` | 212 | Add `.limit(1000)` safety cap or filter by plan_id |

**Step 2: Read each file, understand context, apply minimal fix**

**Step 3: Write test for any new pagination behavior**

**Step 4: Commit**

```bash
git commit -m "fix: add pagination guards to unbounded .all() queries"
```

**Task C3: Docker Compose hardening**

**Step 1: Add `start_period` to db and caddy health checks** (they're missing it):

```yaml
db:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U availai"]
    interval: 5s
    timeout: 3s
    retries: 10
    start_period: 30s  # ADD

caddy:
  healthcheck:
    test: ["CMD-SHELL", "curl -sf http://localhost:2019/config/"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 15s  # ADD
```

**Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "ops: add start_period to db and caddy health checks"
```

---

### Stream D: Vibe/Explorium Router & Tests

**Goal:** Add router endpoints and tests for the existing `app/services/prospect_discovery_explorium.py` (402 lines).

**Files:**
- Read: `app/services/prospect_discovery_explorium.py`
- Create: `app/routers/explorium.py`
- Create: `app/schemas/explorium.py` (if needed)
- Create: `tests/test_explorium.py`
- Modify: `app/main.py` (register router)

**Step 1: Read the existing service** to understand its public API:

```bash
grep "^async def\|^def\|^class" app/services/prospect_discovery_explorium.py
```

**Step 2: Create Pydantic schemas** for request/response models in `app/schemas/explorium.py`

**Step 3: Create router** in `app/routers/explorium.py` with endpoints:
- `GET /api/explorium/segments` — list available ICP segments
- `POST /api/explorium/discover` — discover companies by segment
- `GET /api/explorium/status` — check API connectivity
- Auth: `require_user` dependency

**Step 4: Register router** in `app/main.py`

**Step 5: Write tests** in `tests/test_explorium.py`:
- Mock the Explorium API calls (httpx)
- Test each endpoint (happy path + error cases)
- Test schema validation
- Test auth requirements

**Step 6: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_explorium.py -v
```

**Step 7: Coverage check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_explorium.py --cov=app/routers/explorium --cov=app/services/prospect_discovery_explorium --cov-report=term-missing -q
```

**Step 8: Commit**

```bash
git add app/routers/explorium.py app/schemas/explorium.py tests/test_explorium.py app/main.py
git commit -m "feat: add Explorium/Vibe discovery router with tests"
```

---

### Stream E: Frontend HIGH-Priority Fixes

**Goal:** Fix 5 HIGH-severity frontend issues (CSS architecture, z-index, inline styles, responsive sync).

**Files:**
- Modify: `app/static/styles.css`
- Modify: `app/templates/index.html`
- Modify: `app/static/app.js` and/or `app/static/crm.js`

**Task E1: Eliminate `!important` overrides (H5)**

**Step 1: Find all !important declarations**

```bash
grep -n '!important' app/static/styles.css
```

**Step 2: Restructure base rules** to eliminate need for overrides:
- `.sc` padding chain (lines ~179/445)
- Sidebar state overrides (lines ~412/539)
- `.ofr-detail` and `.drow` table styling

**Step 3: Test in browser** — verify no visual regressions

**Step 4: Commit**

```bash
git add app/static/styles.css
git commit -m "fix: eliminate !important overrides by restructuring CSS specificity"
```

**Task E2: Consolidate media queries (H6)**

**Step 1: Find all fragmented media query blocks**

```bash
grep -n '@media' app/static/styles.css
```

**Step 2: Consolidate 4 fragmented `@media(max-width:768px)` blocks** into 1-2 organized blocks at the end of the file.

**Step 3: Commit**

```bash
git add app/static/styles.css
git commit -m "fix: consolidate fragmented media query blocks"
```

**Task E3: Fix z-index stacking (H7)**

**Step 1: Audit z-index values**

```bash
grep -n 'z-index' app/static/styles.css
```

**Step 2: Fix** `.site-typeahead-list` (10 → 501+) and resolve `.toparea`/`.sidebar` z-index tie (both 200 → make sidebar 201 or toparea 199).

**Step 3: Commit**

```bash
git add app/static/styles.css
git commit -m "fix: resolve z-index stacking conflicts"
```

**Task E4: Migrate inline `style="display:none"` to `.u-hidden` (H8)**

**Step 1: Count inline hidden styles**

```bash
grep -c 'style="display:none"' app/templates/index.html
```

**Step 2: Replace with class** — find-replace `style="display:none"` with `class="u-hidden"` (preserving existing classes).

**Step 3: Verify JS show/hide logic** uses `.u-hidden` class toggle, not inline style manipulation. Update any `el.style.display = 'none'` → `el.classList.add('u-hidden')` and `el.style.display = ''` → `el.classList.remove('u-hidden')`.

**Important:** This is a large change (~143 occurrences). Be careful with elements that have BOTH a class and inline style. Pattern: `style="display:none"` → add `u-hidden` to class list.

**Step 4: Commit**

```bash
git add app/templates/index.html app/static/app.js app/static/crm.js app/static/styles.css
git commit -m "fix: replace inline display:none with .u-hidden class"
```

**Task E5: Sync dual search inputs on resize (H9)**

**Step 1: Read app.js** to find the two search inputs and understand the responsive breakpoint.

**Step 2: Add resize listener** that syncs the value between mobile and desktop search inputs.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix: sync dual search input values on responsive resize"
```

---

## Phase 2: Integration & Deploy (Sequential — after all streams complete)

**Step 1: Pull all stream branches** (if using worktrees) or verify all commits on main

**Step 2: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

**Step 3: Run coverage check — must be ≥ current baseline**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

**Step 4: Lint check**

```bash
cd /root/availai && ruff check app/ tests/
```

**Step 5: Deploy**

```bash
docker compose up -d --build
docker compose logs -f app 2>&1 | head -50
```

**Step 6: Verify health**

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

---

## Phase 3: Lower Priority (Optional, after Phase 2)

These are lower priority and can be done if time permits:

- **Frontend tech debt (L1-L9):** CSS variables, duplicate selectors, JS robustness, accessibility
- **Sourcing connector debugging:** OEMSecrets, TME (blocked — API secret expired), Element14
- **RFQ UX improvements:** BLOCKED on user requirements

---

## Stream Dependency Map

```
Stream A (Scheduler) ──┐
Stream B (Tests)     ──┤
Stream C (Infra)     ──┼──→ Phase 2: Integration & Deploy
Stream D (Explorium) ──┤
Stream E (Frontend)  ──┘
```

No cross-stream file conflicts. All streams are fully independent.

## File Conflict Matrix

| File | A | B | C | D | E |
|------|---|---|---|---|---|
| app/scheduler.py | ✏️ | | | | |
| app/jobs/* | ✏️ | | | | |
| tests/* | | ✏️ | | ✏️ | |
| Caddyfile | | | ✏️ | | |
| docker-compose.yml | | | ✏️ | | |
| app/main.py | maybe | | ✏️ | ✏️ | |
| app/templates/index.html | | | ✏️ | | ✏️ |
| app/static/styles.css | | | | | ✏️ |
| app/static/app.js | | | | | ✏️ |
| app/routers/explorium.py | | | | ✏️ | |

**Potential conflicts:**
- `app/main.py`: Streams A, C, D all may touch it — coordinate by having each add only their specific lines (router import, middleware)
- `app/templates/index.html`: Streams C and E — C adds nonce attributes, E changes classes. Low conflict risk.
