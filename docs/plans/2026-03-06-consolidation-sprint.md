# Consolidation Sprint — Security, Prospecting, Cleanup

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete 3 pending work items: security hardening (2 remaining fixes), prospecting discovery pool frontend (Tasks 4-8) + tests, and unbounded `.all()` query audit.

**Architecture:** 6 phases executed sequentially. Phase 1 (security) has 2 independent tasks that can run in parallel. Phase 2 merges deep-cleaning branch. Phase 3 completes prospecting frontend. Phase 4 adds test coverage. Phase 5 audits and fixes unbounded queries. Phase 6 is full test + deploy.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Jinja2, vanilla JS

---

### Task 1: Fix 401 redirect path

**Files:**
- Modify: `app/static/app.js:327`

**Step 1: Fix the redirect**

In `app/static/app.js`, line 327, change:
```javascript
setTimeout(() => { window.location.href = '/login'; }, 1500);
```
to:
```javascript
setTimeout(() => { window.location.href = '/auth/login'; }, 1500);
```

**Step 2: Check for other occurrences**

Run: `grep -rn "'/login'" app/static/ app/templates/`
Expected: No other bare `/login` references (only `/auth/login`)

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix: correct 401 redirect from /login to /auth/login"
```

---

### Task 2: Add Uvicorn proxy headers

**Files:**
- Modify: `Dockerfile:52`

**Step 1: Update CMD line**

In `Dockerfile`, line 52, change:
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
to:
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

Note: `--forwarded-allow-ips '*'` is safe because port 8000 is only reachable within the Docker network (Caddy is the sole ingress).

**Step 2: Commit**

```bash
git add Dockerfile
git commit -m "ops: enable Uvicorn proxy headers for correct client IP behind Caddy"
```

---

### Task 3: Merge deep-cleaning branch

**Step 1: Merge**

```bash
git merge deep-cleaning
```

Resolve any conflicts. The deep-cleaning branch has 42 commits primarily about agent testing, self-heal loop, and service splits.

**Step 2: Fix broken test import**

Check if `tests/test_sighting_cache.py` exists and has import errors:

```bash
TESTING=1 PYTHONPATH=/root/availai python -c "import tests.test_sighting_cache" 2>&1 | head -5
```

If it imports `_get_cached_sources` from `search_service` and that function doesn't exist, either:
- Remove the test file if the feature was intentionally deleted
- Or fix the import to point to the current function name

**Step 3: Post-merge test verification**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short`
Expected: All pass. Fix any breakage before proceeding.

**Step 4: Commit merge fixes if any**

```bash
git add -A
git commit -m "fix: resolve post-merge issues from deep-cleaning branch"
```

---

### Task 4: Rewire sidebar — Prospecting shows card pool

**Files:**
- Modify: `app/static/app.js:738,1065,7699`
- Modify: `app/templates/index.html:375`

**Step 1: Change all 3 route mappings**

In `app/static/app.js`, change these 3 lines:

Line 738 — change:
```javascript
'view-prospecting': () => window.showProspecting(),
```
to:
```javascript
'view-prospecting': () => window.showSuggested(),
```

Line 1065 — same change:
```javascript
'view-prospecting': () => window.showProspecting(),
```
to:
```javascript
'view-prospecting': () => window.showSuggested(),
```

Line 7699 — change:
```javascript
prospecting: () => window.showProspecting(),
```
to:
```javascript
prospecting: () => window.showSuggested(),
```

**Step 2: Update the view-suggested header**

In `app/templates/index.html`, line 375, change:
```html
<h2>Suggested Accounts</h2>
```
to:
```html
<h2>Prospecting</h2>
```

**Step 3: Commit**

```bash
git add app/static/app.js app/templates/index.html
git commit -m "feat(prospecting): rewire sidebar to show discovery card pool"
```

---

### Task 5: Add filter dropdowns to discovery pool UI

**Files:**
- Modify: `app/templates/index.html:389-407` (after suggestedSize select)
- Modify: `app/static/crm.js` (loadSuggested, showSuggested)

**Step 1: Add filter selects to HTML**

In `app/templates/index.html`, after the `suggestedSize` select (line 389), before the `suggestedFitScore` select (line 390), add:

```html
            <select id="suggestedIndustry" onchange="debouncedLoadSuggested()" style="font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--white)">
                <option value="">All Industries</option>
            </select>
            <select id="suggestedRevenue" onchange="debouncedLoadSuggested()" style="font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--white)">
                <option value="">All Revenue</option>
                <option value="<$1M">&lt;$1M</option>
                <option value="$1M-$5M">$1M-$5M</option>
                <option value="$5M-$10M">$5M-$10M</option>
                <option value="$10M-$50M">$10M-$50M</option>
                <option value="$50M+">$50M+</option>
            </select>
            <select id="suggestedRegion" onchange="debouncedLoadSuggested()" style="font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--white)">
                <option value="">All Regions</option>
            </select>
            <select id="suggestedSource" onchange="debouncedLoadSuggested()" style="font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--white)">
                <option value="">All Sources</option>
                <option value="apollo">Apollo</option>
                <option value="lusha">Lusha</option>
                <option value="manual">Manual</option>
            </select>
```

Also add `recent_desc` to the sort dropdown (line 406, after the "Sort: Name" option):
```html
                <option value="recent_desc">Sort: Recently Added</option>
```

**Step 2: Wire filters in loadSuggested()**

In `app/static/crm.js`, find `loadSuggested()` (line 7148). Inside it, find where URLSearchParams are built and add after the existing params:

```javascript
const industry = document.getElementById('suggestedIndustry')?.value || '';
const revenue = document.getElementById('suggestedRevenue')?.value || '';
const region = document.getElementById('suggestedRegion')?.value || '';
const source = document.getElementById('suggestedSource')?.value || '';
if (industry) params.set('industry', industry);
if (revenue) params.set('revenue_range', revenue);
if (region) params.set('region', region);
if (source) params.set('discovery_source', source);
```

**Step 3: Populate dynamic filters from stats endpoint**

In `app/static/crm.js`, add a new function after `showSuggested()` (line ~7146):

```javascript
async function _populateSuggestedFilters() {
    try {
        const resp = await api('/api/prospects/suggested/stats');
        const data = await resp.json();
        const indSel = document.getElementById('suggestedIndustry');
        const regSel = document.getElementById('suggestedRegion');
        if (indSel && data.industries) {
            const opts = ['<option value="">All Industries</option>'];
            data.industries.forEach(i => { opts.push('<option value="' + escAttr(i) + '">' + esc(i) + '</option>'); });
            indSel.textContent = '';
            opts.forEach(html => { indSel.insertAdjacentHTML('beforeend', html); });
        }
        if (regSel && data.regions) {
            const opts = ['<option value="">All Regions</option>'];
            data.regions.forEach(r => { opts.push('<option value="' + escAttr(r) + '">' + esc(r) + '</option>'); });
            regSel.textContent = '';
            opts.forEach(html => { regSel.insertAdjacentHTML('beforeend', html); });
        }
    } catch (e) { /* stats are optional - filters still work with static options */ }
}
```

Note: Uses `textContent = ''` to clear existing options safely, then `insertAdjacentHTML` with `esc()`/`escAttr()` sanitized values for XSS safety. All user-sourced strings (industry names, region names) pass through `esc()` and `escAttr()` before insertion.

Call `_populateSuggestedFilters()` from `showSuggested()` (after the existing `loadSuggested()` call):

```javascript
_populateSuggestedFilters();
```

**Step 4: Reset filters in showSuggested()**

In `showSuggested()`, add resets for the new filter elements:

```javascript
const filterIds = ['suggestedIndustry', 'suggestedRevenue', 'suggestedRegion', 'suggestedSource'];
filterIds.forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
```

**Step 5: Commit**

```bash
git add app/templates/index.html app/static/crm.js
git commit -m "feat(prospecting): add industry, revenue, region, source filter dropdowns"
```

---

### Task 6: Enhance prospect cards with revenue and source badge

**Files:**
- Modify: `app/static/crm.js` (renderSuggestedGrid function, line ~7195)

**Step 1: Add revenue to card meta line**

In `renderSuggestedGrid()` (line 7195), find where meta items are pushed (look for `hq_location` or similar). After that push, add:

```javascript
if (a.revenue_range) meta.push('<span>' + esc(a.revenue_range) + '</span>');
```

**Step 2: Add source badge**

In the same function, find where badges are rendered (look for `sfBadge` or badge div). Add a source badge:

```javascript
const sourceBadge = a.discovery_source
    ? '<span class="suggested-badge" style="background:var(--border);color:var(--text);font-size:9px">' + esc(a.discovery_source) + '</span>'
    : '';
```

Include `sourceBadge` in the badges HTML output.

**Step 3: Commit**

```bash
git add app/static/crm.js
git commit -m "feat(prospecting): show revenue range and source badge on prospect cards"
```

---

### Task 7: Claim toast with CRM link

**Files:**
- Modify: `app/static/crm.js` (claimSuggestedAccount function, line ~7328)

**Step 1: Update success toast**

In `claimSuggestedAccount()` (line 7328), find the success toast call (likely `showToast`). Update it to include a link to the company detail view. The `result.company_id` is a server-returned integer (safe). The company name must go through `esc()`:

```javascript
const safeLink = '<a href="#" onclick="showCompanyDetail(' + result.company_id + ');return false" style="color:var(--primary);text-decoration:underline">Go to Account</a>';
showToast(esc(name) + ' claimed! ' + safeLink, 'success', true);
```

Verify that `showToast` supports an HTML mode parameter (third arg). If it uses `textContent` internally, add a flag to allow safe HTML when `true` is passed. The only dynamic value in the HTML is `result.company_id` (integer from server) and `name` (escaped via `esc()`).

**Step 2: Commit**

```bash
git add app/static/crm.js
git commit -m "feat(prospecting): add CRM link to claim success toast"
```

---

### Task 8: Remove old prospecting view

**Files:**
- Modify: `app/templates/index.html:324-370` (remove view-prospecting div)
- Modify: `app/static/crm.js` (remove dead functions)

**Step 1: Remove the HTML**

In `app/templates/index.html`, remove the entire `<div id="view-prospecting">` block (lines 324-370, from `<!-- VIEW: Prospecting -->` through the closing `</div>`).

**Step 2: Remove dead JS functions from crm.js**

In `app/static/crm.js`, remove these functions and their associated state variables. Search for each and delete the complete function body:

State variables to remove (near top of file):
- `_prospectingTab`, `_prospectingData`, `_prospectCapacity`, `_expandedAccounts`, `_prospectAbort`, `_selectedProspectSiteId`, `_prospectMiniListIds`

Functions to remove:
- `setProspectingTab`
- `showProspecting`
- `debouncedLoadProspecting`
- `loadProspecting`
- `renderProspecting`
- `_renderProspectingMobile`
- `_healthBadge`
- `_loadCapacityBar`
- `_renderCapacityBar`
- `toggleAccountExpand`
- `_loadAccountSites`
- `releaseSite`
- `releaseStaleSites`
- `claimSite`
- `openProspectDrawer`
- `closeProspectDrawer`
- `switchProspectDrawerTab`
- `_renderProspectDrawerOverview`
- `_renderProspectDrawerContacts`
- `_renderProspectDrawerActivity`
- `_renderProspectMiniList`
- `_renderProspectMiniListFromSearch`
- `_prospectMiniListKeyNav`

Also remove these from the `window` exports at the bottom of crm.js.

**Important:** Do NOT remove backend endpoints in `app/routers/v13_features/prospecting.py` -- ownership sweep and at-risk emails depend on them.

**Step 3: Verify no remaining references**

Run: `grep -n "showProspecting\|loadProspecting\|renderProspecting\|setProspectingTab\|prospectDrawer\|claimSite\|releaseSite" app/static/crm.js app/static/app.js app/templates/index.html`
Expected: No matches.

**Step 4: Commit**

```bash
git add app/templates/index.html app/static/crm.js
git commit -m "refactor(prospecting): remove old site-ownership prospecting view, keep backend endpoints"
```

---

### Task 9: Prospecting test coverage

**Files:**
- Verify: `tests/test_routers_prospect_suggested.py`

**Step 1: Verify existing test file**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -v --tb=short 2>&1 | tail -10`

Check how many tests exist and if they all pass.

**Step 2: Add missing filter tests if needed**

If `test_filter_by_revenue_range` and `test_filter_by_discovery_source` don't already exist, add them:

```python
def test_filter_by_revenue_range(self, client, db_session):
    """Filter prospects by revenue_range."""
    from app.models.prospect_account import ProspectAccount
    p = ProspectAccount(name="RevCo", domain="revco.com", discovery_source="apollo",
                        revenue_range="$5M-$10M", status="suggested")
    db_session.add(p)
    db_session.commit()
    resp = client.get("/api/prospects/suggested?revenue_range=$5M-$10M")
    assert resp.status_code == 200
    assert any(i["name"] == "RevCo" for i in resp.json()["items"])

def test_filter_by_discovery_source(self, client, db_session):
    """Filter prospects by discovery_source."""
    from app.models.prospect_account import ProspectAccount
    p = ProspectAccount(name="ApolCo", domain="apolco.com", discovery_source="apollo",
                        status="suggested")
    db_session.add(p)
    db_session.commit()
    resp = client.get("/api/prospects/suggested?discovery_source=apollo")
    assert resp.status_code == 200
    assert any(i["name"] == "ApolCo" for i in resp.json()["items"])

def test_sort_recent_desc(self, client, db_session):
    """Sort by recently added."""
    resp = client.get("/api/prospects/suggested?sort=recent_desc")
    assert resp.status_code == 200

def test_stats_includes_industries_and_regions(self, client, db_session):
    """Stats endpoint returns distinct industries and regions."""
    from app.models.prospect_account import ProspectAccount
    p = ProspectAccount(name="IndCo", domain="indco.com", discovery_source="apollo",
                        industry="Electronics", region="Southwest", status="suggested")
    db_session.add(p)
    db_session.commit()
    resp = client.get("/api/prospects/suggested/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "industries" in data
    assert "Electronics" in data["industries"]
    assert "regions" in data
    assert "Southwest" in data["regions"]
```

**Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -v`
Expected: All PASS

**Step 4: Commit if new tests added**

```bash
git add tests/test_routers_prospect_suggested.py
git commit -m "test(prospecting): add coverage for revenue, source, recency filters and stats"
```

---

### Task 10: Unbounded `.all()` audit

**Files:**
- Modify: Various router files (see analysis below)

**Step 1: Identify high-risk unbounded queries**

Run: `grep -rn '\.all()' app/routers/ | grep -v '\.limit(' | grep -v '\.offset(' | grep -v '_ids)' | grep -v 'group_by'`

Focus on queries that:
1. Hit large tables (Sighting, Offer, VendorCard, MaterialCard, Contact, MaterialVendorHistory)
2. Are user-facing (not admin-only)
3. Have no `.limit()` or `.filter(X.id.in_(...))` constraint

Already-safe patterns to skip:
- Queries with `.limit()` before `.all()`
- Queries filtered by specific IDs (`.filter(X.id.in_(ids))` -- bounded by input)
- Small tables: User, ApiSource, Tag, SystemConfig
- Admin-only endpoints behind `require_admin`

**Step 2: Categorize and fix**

For each high-risk query found:

**Pattern A -- User-facing list endpoint without pagination:**
Add `limit` and `offset` query params with sensible defaults:
```python
@router.get("/api/something")
def list_something(limit: int = 100, offset: int = 0, ...):
    items = query.offset(offset).limit(limit).all()
    total = query.count()
    return {"items": [...], "total": total, "limit": limit, "offset": offset}
```

**Pattern B -- Internal query that could grow large:**
Add a safety cap:
```python
items = query.limit(5000).all()
```

**Pattern C -- Already safe (filtered by ID set, small table, admin-only):**
Document as acceptable -- no change needed.

**Step 3: Key files to check (highest risk)**

These files have multiple unbounded `.all()` on potentially large tables:

- `app/routers/rfq.py` -- lines 72, 128, 152, 155, 166, 611
- `app/routers/crm/offers.py` -- lines 59, 70, 96, 185, 586
- `app/routers/crm/buy_plans.py` -- lines 362
- `app/routers/crm/buy_plans_v3.py` -- lines 362, 645
- `app/routers/command_center.py` -- lines 44, 57, 71
- `app/routers/dashboard/briefs.py` -- lines 53, 68, 207, 378, 537, 608, 679, 715, 814, 841
- `app/routers/dashboard/overview.py` -- lines 53, 90, 237, 278, 345, 378

**Step 4: Add tests for newly paginated endpoints**

For each endpoint that gets pagination added, add a test:
```python
def test_endpoint_respects_limit(client, db_session):
    resp = client.get("/api/endpoint?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert len(data["items"]) <= 10
```

**Step 5: Commit**

```bash
git add app/routers/ tests/
git commit -m "safety: add pagination guards to unbounded .all() queries"
```

---

### Task 11: Full test suite + deploy

**Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All pass

**Step 2: Check coverage**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: Coverage >= 97% (no regression)

**Step 3: Fix any regressions**

If tests fail, fix and commit before deploying.

**Step 4: Deploy**

```bash
git push origin main
docker compose up -d --build
docker compose logs -f app
```

Verify clean startup -- no tracebacks, all scheduler jobs registered.

**Step 5: Manual verification checklist**

1. Click "Prospecting" in sidebar -- card grid loads with filter dropdowns
2. Test each filter: search, industry, size, revenue, region, fit score, readiness, source
3. Test sort options: fit score, readiness, composite, recently added, name
4. Click a card -- detail drawer opens with enrichment data
5. Claim a prospect -- toast with "Go to Account" CRM link
6. Dismiss a prospect -- card fades out
7. Trigger a 401 (expire session) -- redirects to `/auth/login`
8. Check `docker compose logs app | grep "X-Forwarded-For"` -- client IPs resolve correctly
