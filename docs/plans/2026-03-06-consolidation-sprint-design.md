# Design: Consolidation Sprint — Security, Prospecting, Cleanup

**Date:** 2026-03-06
**Goal:** Complete 3 pending work items in one sprint: security hardening (Tasks 3-4, 6), prospecting discovery pool frontend (Tasks 4-8) + tests, and unbounded `.all()` audit.

**Items confirmed already done (excluded):**
- Scheduler split into `app/jobs/` — already complete (63-line scheduler.py + 10 domain modules)
- Security Task 5 (CSP for Vite) — CSP middleware already in place
- Security Tasks 1-2 (OAuth state, encryption fail-closed) — committed in Phase 1
- Prospecting Tasks 1-3 (backend filters, sort, stats) — committed as `92107f5`

---

## Phase 1: Security Hardening (3 fixes, parallelizable)

### Task 1A: Frontend 401 redirect path
- **File:** `app/static/app.js:327`
- **Change:** `/login` → `/auth/login`
- **Also grep for** other `/login` references in JS files

### Task 1B: Uvicorn proxy headers
- **File:** `Dockerfile` CMD line
- **Change:** Add `--proxy-headers --forwarded-allow-ips *`
- **Safe because** port 8000 only reachable within Docker network (Caddy is sole ingress)

### Task 1C: Model config unification
- **File:** `app/services/gradient_service.py:39` — fix `opus-4.6` → `opus-4-6`
- **File:** `app/utils/claude_client.py:32-35` — read `MODELS["smart"]` from `settings.anthropic_model`
- **Tests:** Update or add tests for both changes

---

## Phase 2: Merge deep-cleaning branch

### Task 2A: Merge
- `git merge deep-cleaning` into main (no rebase)
- Resolve conflicts if any

### Task 2B: Fix broken test import
- `tests/test_sighting_cache.py` imports `_get_cached_sources` which doesn't exist
- Either fix the import or remove the test if the function was intentionally deleted

### Task 2C: Post-merge verification
- Run full test suite to confirm merge is clean
- Fix any breakage before proceeding

---

## Phase 3: Prospecting Frontend Completion (Tasks 4-8)

Verify what deep-cleaning already brought in before doing each task — avoid duplicating work.

### Task 3A: Rewire sidebar navigation
- **File:** `app/static/app.js` — change `prospecting: () => window.showProspecting()` to `prospecting: () => window.showSuggested()` (3 occurrences at lines 738, 1065, 7699)

### Task 3B: Add filter dropdowns to UI
- **File:** `app/templates/index.html` — add industry, revenue, region, source `<select>` elements after existing size filter in `view-suggested` toolbar
- **File:** `app/static/crm.js` — wire new filters into `loadSuggested()` URLSearchParams, add `_populateSuggestedFilters()` to fetch from stats endpoint
- Populate industry/region dynamically from `/api/prospects/suggested/stats`
- Revenue options: `<$1M`, `$1M-$5M`, `$5M-$10M`, `$10M-$50M`, `$50M+`
- Source options: `apollo`, `lusha`, `manual`
- Add `recent_desc` to sort dropdown

### Task 3C: Enhance prospect cards
- **File:** `app/static/crm.js` — in `renderSuggestedGrid()`:
  - Add `revenue_range` to meta line (after hq_location)
  - Add source badge (small styled span)
  - All values through `esc()` for XSS safety

### Task 3D: Claim toast with CRM link
- **File:** `app/static/crm.js` — in `claimSuggestedAccount()`, update success toast to include "Go to Account" link using `result.company_id`

### Task 3E: Remove old prospecting view
- **File:** `app/templates/index.html` — remove `<div id="view-prospecting">` block (line 324+)
- **File:** `app/static/crm.js` — remove dead functions: `showProspecting`, `loadProspecting`, `renderProspecting`, `setProspectingTab`, and all associated state variables + helpers
- **File:** `app/static/crm.js` — clean window exports
- **DO NOT** remove backend endpoints in `app/routers/v13_features/prospecting.py` (ownership sweep depends on them)

---

## Phase 4: Prospecting Test Suite

### Task 4A: Verify existing tests
- Check if `tests/test_routers_prospect_suggested.py` exists and has 117 tests post-merge
- Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -v`

### Task 4B: Add missing test coverage
- Test revenue_range filter
- Test discovery_source filter
- Test recent_desc sort
- Test stats endpoint returns industries/regions
- Test edge cases: empty filters, invalid sort values

---

## Phase 5: Unbounded `.all()` Audit

### Task 5A: Find unbounded queries
- Grep for `.all()` across `app/routers/` and `app/services/`
- Identify endpoints that return full result sets without `limit`/`offset`
- Categorize by risk: high (user-facing, large tables), medium (admin-only), low (small tables)

### Task 5B: Add pagination guards
- For high-risk endpoints: add proper `limit`/`offset` params with defaults
- For medium-risk: add `.limit(1000)` safety cap
- For low-risk: document as acceptable (e.g., config tables, enum lookups)

### Task 5C: Test pagination
- Add tests for any newly paginated endpoints
- Verify existing pagination (companies endpoint already paginated)

---

## Phase 6: Full Test Suite + Deploy

### Task 6A: Final test suite
- `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
- `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
- Coverage must not decrease from current baseline

### Task 6B: Deploy
- `git push origin main`
- `docker compose up -d --build`
- `docker compose logs -f app` — verify clean startup

### Task 6C: Manual verification
1. Click "Prospecting" in sidebar — card grid loads with filters
2. Test each filter: search, industry, size, revenue, region, source
3. Test sort options: fit score, readiness, composite, recently added, name
4. Click a card — detail drawer opens
5. Claim a prospect — toast with CRM link
6. Dismiss a prospect — card fades out
7. Trigger a 401 — redirects to `/auth/login`

---

## Risk Mitigation

- **Deep-cleaning merge conflicts:** Phase 2 is isolated — fix before touching frontend
- **Frontend overlap:** Check deep-cleaning changes before each Phase 3 task to avoid duplication
- **Test breakage:** Run suite after each phase, not just at the end
- **Unbounded query audit:** Conservative approach — only add limits where tables can grow large
