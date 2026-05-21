# Phase 1: MVP Strip-Down Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all cut features (dashboard, self-heal, Teams bot/alerts, notification intelligence, AI drafting/quoting, resurfacing) from the codebase. Clean cuts — no stubs, no commented code.

**Architecture:** Surgical deletion of ~78 files (6 routers, 24 services, 2 jobs, 2 scripts, 46 tests), followed by import cleanup in 9 kept files and frontend cleanup. All models and migrations stay for DB continuity.

**Tech Stack:** Python/FastAPI, Jinja2, vanilla JS (frontend cleanup only — full rewrite is Phase 3)

**Spec:** `docs/superpowers/specs/2026-03-13-mvp-strip-down-design.md`

---

## Chunk 1: Delete Files

### Task 1: Delete router files

**Files:**
- Delete: `app/routers/dashboard/` (entire directory)
- Delete: `app/routers/explorium.py`
- Delete: `app/routers/teams_bot.py`
- Delete: `app/routers/teams_alerts.py`
- Delete: `app/routers/trouble_tickets.py`
- Delete: `app/routers/notifications.py`

- [ ] **Step 1: Delete router files**

```bash
rm -rf app/routers/dashboard/
rm -f app/routers/explorium.py
rm -f app/routers/teams_bot.py
rm -f app/routers/teams_alerts.py
rm -f app/routers/trouble_tickets.py
rm -f app/routers/notifications.py
```

- [ ] **Step 2: Verify deletions**

```bash
ls app/routers/dashboard/ 2>&1  # Should say "No such file or directory"
ls app/routers/explorium.py 2>&1  # Should say "No such file or directory"
```

- [ ] **Step 3: Commit**

```bash
git add -u app/routers/
git commit -m "strip: delete cut router files (dashboard, explorium, teams_bot, teams_alerts, trouble_tickets, notifications)"
```

---

### Task 2: Delete service files

**Files:**
- Delete: 24 service files listed below

- [ ] **Step 1: Delete service files**

```bash
rm -f app/services/activity_insights.py
rm -f app/services/ai_email_drafter.py
rm -f app/services/ai_quote_analyzer.py
rm -f app/services/ai_trouble_prompt.py
rm -f app/services/cost_controller.py
rm -f app/services/dashboard_briefing.py
rm -f app/services/deal_risk.py
rm -f app/services/diagnosis_service.py
rm -f app/services/execution_service.py
rm -f app/services/file_mapper.py
rm -f app/services/find_trouble_service.py
rm -f app/services/notify_intelligence.py
rm -f app/services/patch_generator.py
rm -f app/services/pattern_tracker.py
rm -f app/services/prompt_generator.py
rm -f app/services/resurfacing_service.py
rm -f app/services/rollback_service.py
rm -f app/services/site_tester.py
rm -f app/services/teams_bot_service.py
rm -f app/services/test_prompts.py
rm -f app/services/ticket_consolidation.py
rm -f app/services/trouble_ticket_service.py
rm -f app/services/notification_service.py
rm -f app/services/teams_alert_service.py
```

- [ ] **Step 2: Verify — count remaining service files**

```bash
ls app/services/*.py | wc -l
# Should be ~97 files (was ~121, minus 24)
```

- [ ] **Step 3: Commit**

```bash
git add -u app/services/
git commit -m "strip: delete 24 cut service files (self-heal, dashboard, notifications, teams bot/alerts, AI drafter/analyzer)"
```

---

### Task 3: Delete job files, scripts, and test files

**Files:**
- Delete: `app/jobs/notify_intelligence_jobs.py`, `app/jobs/teams_alert_jobs.py`
- Delete: `scripts/self_heal_watcher.sh`, `scripts/apply_patches.py`
- Delete: 46 test files

- [ ] **Step 1: Delete job files**

```bash
rm -f app/jobs/notify_intelligence_jobs.py
rm -f app/jobs/teams_alert_jobs.py
```

- [ ] **Step 2: Delete scripts**

```bash
rm -f scripts/self_heal_watcher.sh
rm -f scripts/apply_patches.py
```

- [ ] **Step 3: Delete test files — self-heal & diagnostics (19 files)**

```bash
rm -f tests/test_activity_insights.py
rm -f tests/test_cost_controller.py
rm -f tests/test_deal_risk.py
rm -f tests/test_diagnosis_service.py
rm -f tests/test_execution_service.py
rm -f tests/test_file_mapper.py
rm -f tests/test_find_trouble.py
rm -f tests/test_notify_intelligence.py
rm -f tests/test_patch_generator.py
rm -f tests/test_pattern_tracker.py
rm -f tests/test_prompt_generator.py
rm -f tests/test_rollback_service.py
rm -f tests/test_scheduler_selfheal.py
rm -f tests/test_selfheal_integration.py
rm -f tests/test_site_tester.py
rm -f tests/test_test_prompts.py
rm -f tests/test_ticket_consolidation.py
rm -f tests/test_trouble_prompt.py
rm -f tests/test_trouble_tickets.py
```

- [ ] **Step 4: Delete test files — dashboard, AI, notifications, teams alerts, browser (27 files)**

```bash
rm -f tests/test_dashboard_briefing.py
rm -f tests/test_dashboard_kpi_all_statuses.py
rm -f tests/test_dashboard_morning_brief.py
rm -f tests/test_dashboard_needs_attention.py
rm -f tests/test_dashboard_attention_feed.py
rm -f tests/test_team_leaderboard.py
rm -f tests/test_unified_leaderboard_endpoint.py
rm -f tests/test_buyer_dashboard.py
rm -f tests/test_email_drafter.py
rm -f tests/test_quote_analyzer.py
rm -f tests/test_notification_service.py
rm -f tests/test_notification_router.py
rm -f tests/test_notifications_overhaul.py
rm -f tests/test_resurfacing.py
rm -f tests/test_teams_bot.py
rm -f tests/test_teams_alert_service.py
rm -f tests/test_teams_alert_requisition.py
rm -f tests/test_teams_alert_vendor_quote.py
rm -f tests/test_teams_alert_director.py
rm -f tests/test_teams_alert_briefing.py
rm -f tests/test_apply_patches.py
rm -f tests/test_browser_e2e.py
```

- [ ] **Step 5: Commit**

```bash
git add -u app/jobs/ scripts/ tests/
git commit -m "strip: delete cut jobs, scripts, and 46 test files"
```

---

## Chunk 2: Clean Imports and Registrations

### Task 4: Clean app/main.py router registrations

**Files:**
- Modify: `app/main.py`

The file has router imports (lines ~595-636) and registrations (lines ~639-687). Remove deleted routers from both sections.

- [ ] **Step 1: Remove dashboard_router import**

Find and remove this line from the import block:
```python
from .routers.dashboard import router as dashboard_router
```

- [ ] **Step 2: Remove notifications_router import**

Find and remove:
```python
from .routers.notifications import router as notifications_router
```

- [ ] **Step 3: Remove teams_alerts_router import**

Find and remove:
```python
from .routers.teams_alerts import router as teams_alerts_router
```

- [ ] **Step 4: Remove notifications_router registration**

Find and remove from the core registrations block:
```python
    app.include_router(notifications_router)
```

- [ ] **Step 5: Remove dashboard_router and teams_alerts_router from MVP-gated block**

Find the MVP-gated block and remove the dashboard and teams_alerts lines:

Before:
```python
    if not settings.mvp_mode:
        app.include_router(apollo_sync_router)
        app.include_router(dashboard_router)
        app.include_router(enrichment_router)
        app.include_router(performance_router)
        app.include_router(teams_actions_router)
        app.include_router(teams_alerts_router)
        try:
            from .routers.explorium import router as explorium_router
            app.include_router(explorium_router)
        except ModuleNotFoundError:
            pass
```

After:
```python
    if not settings.mvp_mode:
        app.include_router(apollo_sync_router)
        app.include_router(enrichment_router)
        app.include_router(performance_router)
        app.include_router(teams_actions_router)
```

- [ ] **Step 6: Verify main.py compiles**

```bash
cd /root/availai && python -c "import py_compile; py_compile.compile('app/main.py', doraise=True)"
```
Expected: No errors

- [ ] **Step 7: Commit**

```bash
git add app/main.py
git commit -m "strip: remove deleted router imports and registrations from main.py"
```

---

### Task 5: Clean app/jobs/__init__.py

**Files:**
- Modify: `app/jobs/__init__.py`

- [ ] **Step 1: Remove teams_alert_jobs from MVP-gated block**

Find the MVP-gated block:

Before:
```python
    if not settings.mvp_mode:
        from .enrichment_jobs import register_enrichment_jobs
        from .teams_alert_jobs import register_teams_alert_jobs

        register_enrichment_jobs(scheduler, settings)
        register_teams_alert_jobs(scheduler, settings)
```

After:
```python
    if not settings.mvp_mode:
        from .enrichment_jobs import register_enrichment_jobs

        register_enrichment_jobs(scheduler, settings)
```

- [ ] **Step 2: Verify compilation**

```bash
cd /root/availai && python -c "import py_compile; py_compile.compile('app/jobs/__init__.py', doraise=True)"
```

- [ ] **Step 3: Commit**

```bash
git add app/jobs/__init__.py
git commit -m "strip: remove teams_alert_jobs registration from jobs init"
```

---

### Task 6: Clean knowledge_service.py (TOP-LEVEL import — crashes on startup)

**Files:**
- Modify: `app/services/knowledge_service.py`

This file has a **top-level** import of `notification_service.create_notification` at line 18. This will crash the app on startup since `notification_service.py` is deleted.

- [ ] **Step 1: Read the file to understand the import and all usages**

```bash
grep -n "notification_service\|create_notification" app/services/knowledge_service.py
```

- [ ] **Step 2: Remove the import line**

Remove line 18:
```python
from app.services.notification_service import create_notification
```

- [ ] **Step 3: Remove all `create_notification()` calls in the file**

Search for every call to `create_notification(` and remove the entire call block (including any surrounding try/except if the call is the only thing in it). Replace with a loguru log if context is useful:

```python
logger.info("Notification skipped (notifications removed): {}", "<context>")
```

- [ ] **Step 4: Verify compilation**

```bash
cd /root/availai && python -c "import py_compile; py_compile.compile('app/services/knowledge_service.py', doraise=True)"
```

- [ ] **Step 5: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "strip: remove notification_service imports from knowledge_service"
```

---

### Task 7: Clean health_monitor.py (lazy import)

**Files:**
- Modify: `app/services/health_monitor.py`

Line ~95 has a lazy import of `notification_service.create_notification` inside `_notify_admins()`.

- [ ] **Step 1: Read the relevant function**

```bash
grep -n -A 10 "_notify_admins\|notification_service" app/services/health_monitor.py
```

- [ ] **Step 2: Remove the notification import and call**

Replace the `create_notification()` call with a loguru warning:
```python
logger.warning("API source {} health alert: {}", source_name, message)
```

- [ ] **Step 3: Verify compilation**

```bash
cd /root/availai && python -c "import py_compile; py_compile.compile('app/services/health_monitor.py', doraise=True)"
```

- [ ] **Step 4: Commit**

```bash
git add app/services/health_monitor.py
git commit -m "strip: remove notification_service import from health_monitor"
```

---

### Task 8: Clean teams_alert_service imports from 3 files

**Files:**
- Modify: `app/email_service.py` (note: app root, not services/)
- Modify: `app/routers/crm/offers.py`
- Modify: `app/routers/requisitions/core.py`

All three have lazy imports of `teams_alert_service.send_alert` or `send_alert_to_role`.

- [ ] **Step 1: Read the affected code blocks**

```bash
grep -n -B 2 -A 10 "teams_alert_service" app/email_service.py
grep -n -B 2 -A 10 "teams_alert_service" app/routers/crm/offers.py
grep -n -B 2 -A 10 "teams_alert_service" app/routers/requisitions/core.py
```

- [ ] **Step 2: Clean app/email_service.py**

Find the lazy import block inside `send_batch_rfq()` (~line 587) and remove the entire try/except block that imports and calls `send_alert`. The RFQ still sends — it just doesn't notify Teams anymore.

- [ ] **Step 3: Clean app/routers/crm/offers.py**

Find the lazy import (~line 552) and remove the try/except block that calls `send_alert`.

- [ ] **Step 4: Clean app/routers/requisitions/core.py**

Find the lazy import (~line 507) and remove the try/except block that calls `send_alert_to_role`.

- [ ] **Step 5: Verify all three compile**

```bash
cd /root/availai
python -c "import py_compile; py_compile.compile('app/email_service.py', doraise=True)"
python -c "import py_compile; py_compile.compile('app/routers/crm/offers.py', doraise=True)"
python -c "import py_compile; py_compile.compile('app/routers/requisitions/core.py', doraise=True)"
```

- [ ] **Step 6: Commit**

```bash
git add app/email_service.py app/routers/crm/offers.py app/routers/requisitions/core.py
git commit -m "strip: remove teams_alert_service imports from email_service, offers, requisitions"
```

---

### Task 9: Clean remaining import references (knowledge.py, teams_qa_service.py, ai.py, knowledge_jobs.py)

**Files:**
- Modify: `app/routers/knowledge.py` — remove resurfacing_service import + endpoint
- Modify: `app/services/teams_qa_service.py` — inline `_resolve_user` or remove import
- Modify: `app/routers/ai.py` — remove ai_email_drafter and ai_quote_analyzer endpoints
- Modify: `app/jobs/knowledge_jobs.py` — remove briefing blocks + dead helper

- [ ] **Step 1: Clean app/routers/knowledge.py**

```bash
grep -n -B 2 -A 15 "resurfacing_service\|get_mpn_hints" app/routers/knowledge.py
```

Remove the `/resurfacing/hints` endpoint and its import of `resurfacing_service.get_mpn_hints`.

- [ ] **Step 2: Clean app/services/teams_qa_service.py**

```bash
grep -n -B 2 -A 10 "teams_bot_service\|_resolve_user" app/services/teams_qa_service.py
```

The `_resolve_user` function is a simple user lookup. Either:
- Inline it (copy the function body — likely just a `db.query(User).filter(...)` call)
- Or if the entire function using it is Teams-bot-only, remove the calling code

- [ ] **Step 3: Clean app/routers/ai.py**

```bash
grep -n -B 2 -A 15 "ai_email_drafter\|ai_quote_analyzer\|draft_rfq_email\|compare_quotes" app/routers/ai.py
```

Remove the two endpoints that import from deleted modules:
- The endpoint using `ai_email_drafter.draft_rfq_email` (~line 648)
- The endpoint using `ai_quote_analyzer.compare_quotes` (~line 678)

- [ ] **Step 4: Clean app/jobs/knowledge_jobs.py**

```bash
grep -n "dashboard_briefing\|generate_briefing\|_send_briefing_to_teams\|_job_precompute_briefings" app/jobs/knowledge_jobs.py
```

Remove:
- The `_job_precompute_briefings` function entirely
- The briefing import block inside `_job_deliver_question_batches()` (~line 231)
- The briefing import block inside `_job_send_knowledge_digests()` (~line 263)
- The `_send_briefing_to_teams()` helper function (~line 304)
- Any scheduler registration for briefing jobs in `register_knowledge_jobs()`

- [ ] **Step 5: Verify all four compile**

```bash
cd /root/availai
python -c "import py_compile; py_compile.compile('app/routers/knowledge.py', doraise=True)"
python -c "import py_compile; py_compile.compile('app/services/teams_qa_service.py', doraise=True)"
python -c "import py_compile; py_compile.compile('app/routers/ai.py', doraise=True)"
python -c "import py_compile; py_compile.compile('app/jobs/knowledge_jobs.py', doraise=True)"
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/knowledge.py app/services/teams_qa_service.py app/routers/ai.py app/jobs/knowledge_jobs.py
git commit -m "strip: clean remaining imports from knowledge, teams_qa, ai router, knowledge_jobs"
```

---

## Chunk 3: General Sweep and Verification

### Task 10: General import sweep

**Files:**
- Potentially modify: any file in `app/` that still references deleted modules

- [ ] **Step 1: Run the sweep**

```bash
cd /root/availai
grep -rn "from.*notification_service" app/ --include="*.py"
grep -rn "from.*teams_alert_service" app/ --include="*.py"
grep -rn "from.*teams_bot_service" app/ --include="*.py"
grep -rn "from.*dashboard_briefing" app/ --include="*.py"
grep -rn "from.*resurfacing_service" app/ --include="*.py"
grep -rn "from.*deal_risk" app/ --include="*.py"
grep -rn "from.*notify_intelligence" app/ --include="*.py"
grep -rn "from.*trouble_ticket" app/ --include="*.py"
grep -rn "from.*find_trouble" app/ --include="*.py"
grep -rn "from.*site_tester" app/ --include="*.py"
grep -rn "from.*ai_email_drafter" app/ --include="*.py"
grep -rn "from.*ai_quote_analyzer" app/ --include="*.py"
grep -rn "from.*activity_insights" app/ --include="*.py"
```

Expected: **Zero matches.** If any matches remain, fix them following the same pattern as Tasks 6-9.

- [ ] **Step 2: Fix any remaining references**

For each match: remove the import and call. Replace with loguru log if the call provided useful side effects.

- [ ] **Step 3: Commit if fixes were needed**

```bash
git add -u app/
git commit -m "strip: fix remaining import references found in general sweep"
```

---

### Task 11: Clean frontend references

**Files:**
- Modify: `app/static/app.js`
- Modify: `app/static/crm.js`
- Modify: `app/templates/index.html`

Note: This is a minimal cleanup to remove references to deleted features. The full frontend rewrite is Phase 3.

- [ ] **Step 1: Find dashboard references in app.js**

```bash
grep -n "dashboard\|briefing\|leaderboard\|find.trouble\|notification.*bell\|deal.risk" app/static/app.js | head -40
```

- [ ] **Step 2: Remove dashboard tab/view code from app.js**

Remove:
- Dashboard tab rendering (the nav item and click handler)
- Dashboard view/panel rendering functions
- Morning briefing card rendering
- "Find Trouble" button and SSE streaming UI
- Notification bell icon and dropdown
- Deal risk badge/indicator rendering
- Any API calls to deleted endpoints (`/api/dashboard/`, `/api/notifications/`, `/api/teams-bot/`, `/api/trouble-tickets/`)

Keep the file structure intact — just remove the deleted feature sections.

- [ ] **Step 3: Find and clean crm.js**

```bash
grep -n "dashboard\|briefing\|risk.*assess\|deal.risk" app/static/crm.js | head -20
```

Remove dashboard drill-down, briefing cards, risk indicators.

- [ ] **Step 4: Clean index.html**

```bash
grep -n "dashboard\|teams.*bot\|notification.*bell\|find.trouble" app/templates/index.html | head -20
```

Remove dashboard nav items, Teams bot controls, notification bell HTML.

- [ ] **Step 5: Verify the app loads**

```bash
docker compose up -d --build && sleep 10 && docker compose logs app --tail 5
```

Expected: App starts clean, no JS errors in console.

- [ ] **Step 6: Commit**

```bash
git add app/static/app.js app/static/crm.js app/templates/index.html
git commit -m "strip: remove dashboard, notification bell, find trouble, and deal risk from frontend"
```

---

### Task 12: Run full test suite and verify

**Files:**
- No modifications — verification only

- [ ] **Step 1: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x --tb=short -q 2>&1 | tail -20
```

Expected: All remaining tests pass. If any fail due to imports from deleted modules, fix those test files (they were missed in the deletion list).

- [ ] **Step 2: Check coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -30
```

Expected: Coverage should be >=95% on kept code.

- [ ] **Step 3: Fix any failing tests**

If tests fail because they import from deleted modules, delete those test files. If tests fail for other reasons, fix the underlying issue.

- [ ] **Step 4: Build and deploy**

```bash
docker compose up -d --build && sleep 10 && docker compose logs app --tail 20
```

Expected: Clean startup, scheduler running, no import errors.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "strip: Phase 1 complete — all cut features removed, tests passing"
```

---

## Summary

| Task | What | Files Affected |
|------|------|---------------|
| 1 | Delete router files | 6 files deleted |
| 2 | Delete service files | 24 files deleted |
| 3 | Delete jobs, scripts, tests | 48 files deleted |
| 4 | Clean main.py registrations | 1 file modified |
| 5 | Clean jobs/__init__.py | 1 file modified |
| 6 | Clean knowledge_service.py (top-level import) | 1 file modified |
| 7 | Clean health_monitor.py | 1 file modified |
| 8 | Clean teams_alert_service imports (3 files) | 3 files modified |
| 9 | Clean remaining imports (4 files) | 4 files modified |
| 10 | General import sweep | 0-N files modified |
| 11 | Clean frontend references | 3 files modified |
| 12 | Full test suite verification | 0-N files fixed |

**Total: ~78 files deleted, ~14 files modified**
