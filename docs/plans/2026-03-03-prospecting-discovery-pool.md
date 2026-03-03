# Prospecting Discovery Pool — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the current 3-tab site-ownership prospecting page with the existing card-based prospect discovery pool (`view-suggested`), enhanced with additional filters and the "Prospecting" title.

**Architecture:** The card-based discovery UI already exists as `view-suggested` with full claim/dismiss/enrichment support via `/api/prospects/suggested` endpoints. The work is primarily UI wiring: (1) make "Prospecting" sidebar nav show the card pool instead of the ownership manager, (2) add missing filters (industry, revenue, region, source) to both frontend and backend, (3) rename "Suggested Accounts" to "Prospecting", (4) remove the old ownership-based prospecting view. The old prospecting endpoints remain available but the frontend no longer navigates to them.

**Tech Stack:** FastAPI, SQLAlchemy, Jinja2 templates, vanilla JS (crm.js)

**XSS Safety Note:** All user content rendered in cards already uses `esc()` and `escAttr()` helpers. New code must follow the same pattern — never insert raw user strings into HTML.

---

### Task 1: Add missing backend filters (revenue_range, discovery_source)

The `/api/prospects/suggested` endpoint already supports search, region, industry, employee_size, min_fit_score, min_readiness_score. We need to add `revenue_range` and `discovery_source` filter params.

**Files:**
- Modify: `app/routers/prospect_suggested.py:31-119`
- Test: `tests/test_routers_prospect_suggested.py`

**Step 1: Write the failing tests**

Add tests to `tests/test_routers_prospect_suggested.py`:

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
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -k "test_filter_by_revenue or test_filter_by_discovery" -v`
Expected: FAIL (422 — unknown query params)

**Step 3: Add the filter params to the endpoint**

In `app/routers/prospect_suggested.py`, add to `list_suggested()` function signature (after `employee_size` param):

```python
revenue_range: str = "",
discovery_source: str = "",
```

And add filter logic after the `employee_size` filter block (after line ~77):

```python
if revenue_range:
    safe = escape_like(revenue_range.strip())
    query = query.filter(ProspectAccount.revenue_range.ilike(f"%{safe}%"))

if discovery_source:
    query = query.filter(ProspectAccount.discovery_source == discovery_source.strip())
```

**Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -k "test_filter_by_revenue or test_filter_by_discovery" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routers/prospect_suggested.py tests/test_routers_prospect_suggested.py
git commit -m "feat(prospecting): add revenue_range and discovery_source filters to suggested endpoint"
```

---

### Task 2: Add sort options (fit_score default, size, revenue, recently_added)

The backend has readiness_desc (default), fit_desc, composite_desc, name_asc. We need to add: `recent_desc` sort.

**Files:**
- Modify: `app/routers/prospect_suggested.py:96-107`
- Test: `tests/test_routers_prospect_suggested.py`

**Step 1: Write the failing test**

```python
def test_sort_recent_desc(self, client, db_session):
    """Sort by recently added (created_at desc)."""
    resp = client.get("/api/prospects/suggested?sort=recent_desc")
    assert resp.status_code == 200
```

**Step 2: Run test to verify it passes (default sort fallback)**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -k "test_sort_recent" -v`

**Step 3: Add sort option**

In `app/routers/prospect_suggested.py`, extend the sort block (around lines 98-107), add before the `else`:

```python
elif sort == "recent_desc":
    query = query.order_by(ProspectAccount.created_at.desc())
```

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add app/routers/prospect_suggested.py tests/test_routers_prospect_suggested.py
git commit -m "feat(prospecting): add recency sort option"
```

---

### Task 3: Add industries/regions to the stats endpoint

The `/api/prospects/suggested/stats` endpoint needs to return distinct industries and regions for populating filter dropdowns.

**Files:**
- Modify: `app/routers/prospect_suggested.py:122-156`
- Test: `tests/test_routers_prospect_suggested.py`

**Step 1: Write the failing test**

```python
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

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -k "test_stats_includes_industries" -v`
Expected: FAIL (KeyError: 'industries')

**Step 3: Add industries/regions to the stats endpoint**

In `app/routers/prospect_suggested.py`, in the `stats_suggested()` endpoint, add queries:

```python
industries = sorted(
    r[0]
    for r in db.query(ProspectAccount.industry)
    .filter(ProspectAccount.status == "suggested", ProspectAccount.industry.isnot(None))
    .distinct()
    .all()
    if r[0]
)

regions = sorted(
    r[0]
    for r in db.query(ProspectAccount.region)
    .filter(ProspectAccount.status == "suggested", ProspectAccount.region.isnot(None))
    .distinct()
    .all()
    if r[0]
)
```

Add `"industries": industries, "regions": regions` to the return dict.

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_prospect_suggested.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add app/routers/prospect_suggested.py tests/test_routers_prospect_suggested.py
git commit -m "feat(prospecting): add industries and regions to stats endpoint for filter dropdowns"
```

---

### Task 4: Rewire sidebar navigation — Prospecting shows card pool

Make the "Prospecting" sidebar button show `view-suggested` (the card pool) instead of `view-prospecting` (the ownership manager).

**Files:**
- Modify: `app/static/app.js:7692-7693` (sidebarNav routes)
- Modify: `app/templates/index.html:375` (page title)
- Modify: `app/static/crm.js:7144` (top view label)

**Step 1: Change the sidebarNav route**

In `app/static/app.js`, change the prospecting route from:
```javascript
prospecting: () => window.showProspecting(),
```
to:
```javascript
prospecting: () => window.showSuggested(),
```

Keep the `suggested` route pointing to the same function for backwards compatibility.

**Step 2: Rename "Suggested Accounts" to "Prospecting" in the HTML**

In `app/templates/index.html`, line 375, change the h2 from "Suggested Accounts" to "Prospecting".

**Step 3: Update showSuggested() label**

In `app/static/crm.js`, in `showSuggested()`, change the `_setTopViewLabel('Suggested')` call to `_setTopViewLabel('Prospecting')`.

**Step 4: Verify manually**

Open the app, click "Prospecting" in sidebar. Should show the card grid pool, not the old table view.

**Step 5: Commit**

```bash
git add app/static/app.js app/templates/index.html app/static/crm.js
git commit -m "feat(prospecting): rewire sidebar to show discovery card pool"
```

---

### Task 5: Add missing filter dropdowns to the UI

Add industry, revenue range, region, and source filter dropdowns to the `view-suggested` toolbar. Wire them to the backend params.

**Files:**
- Modify: `app/templates/index.html:378-408` (toolbar section of view-suggested)
- Modify: `app/static/crm.js:7128-7163` (showSuggested and loadSuggested functions)

**Step 1: Add filter dropdowns to the HTML toolbar**

In `app/templates/index.html`, after the `suggestedSize` select (line 389), add industry, revenue, region, and source dropdowns. All use `onchange="debouncedLoadSuggested()"`.

Industry and region selects start with just an "All" option — they get populated dynamically from the stats endpoint.

Revenue options: `<$1M`, `$1M-$5M`, `$5M-$10M`, `$10M-$50M`, `$50M+`.

Source options: `apollo`, `lusha`, `manual`.

Also update the sort dropdown to use `fit_desc` as default (first option) and add `recent_desc`.

**Step 2: Wire the new filters in loadSuggested()**

In `app/static/crm.js`, update `loadSuggested()` to read new filter values (industry, revenue, region, source) and append them to the URLSearchParams.

**Step 3: Reset new filters in showSuggested()**

Add `.value = ''` resets for each new filter in `showSuggested()`.

**Step 4: Populate dynamic filter options (industry, region)**

Add a `_populateSuggestedFilters()` function that fetches `/api/prospects/suggested/stats` and populates the industry and region dropdown options using `esc()` for all user content. Call it from `showSuggested()`.

**Step 5: Commit**

```bash
git add app/templates/index.html app/static/crm.js
git commit -m "feat(prospecting): add industry, revenue, region, source filter dropdowns"
```

---

### Task 6: Enhance card with revenue range and source badge

Add revenue range display and source badge to prospect cards.

**Files:**
- Modify: `app/static/crm.js:7195-7301` (renderSuggestedGrid function)

**Step 1: Add revenue to meta line**

In `renderSuggestedGrid()`, after the meta push for `hq_location`, add:

```javascript
if (a.revenue_range) meta.push('<span>' + esc(a.revenue_range) + '</span>');
```

All values go through `esc()` for XSS safety.

**Step 2: Add source badge**

After the `sfBadge` line, create a source badge using `esc()`:

```javascript
const sourceBadge = a.discovery_source
    ? '<span class="suggested-badge" style="background:var(--border);color:var(--text);font-size:9px">' + esc(a.discovery_source) + '</span>'
    : '';
```

Include it in the card badges div.

**Step 3: Commit**

```bash
git add app/static/crm.js
git commit -m "feat(prospecting): show revenue range and source badge on prospect cards"
```

---

### Task 7: Update claim flow toast with CRM link

When a prospect is claimed, the toast should include a "Go to Account" link.

**Files:**
- Modify: `app/static/crm.js:7328-7343` (claimSuggestedAccount function)

**Step 1: Update claim success handler**

In `claimSuggestedAccount()`, update the success toast to include a link to open the company in CRM. The `result.company_id` is already returned by the backend. Use `esc()` for the company name.

**Step 2: Commit**

```bash
git add app/static/crm.js
git commit -m "feat(prospecting): add CRM link to claim success toast"
```

---

### Task 8: Remove old prospecting view from HTML and JS

Remove the `view-prospecting` div and its associated JS functions. Keep the backend endpoints.

**Files:**
- Modify: `app/templates/index.html:324-370` (remove view-prospecting div)
- Modify: `app/static/crm.js` (remove dead prospecting functions)

**Step 1: Remove the HTML**

In `app/templates/index.html`, remove the entire `<div id="view-prospecting">` block (lines 324-370).

**Step 2: Clean up the JS**

In `app/static/crm.js`, remove the old prospecting functions that are no longer called:
- State variables: `_prospectingTab`, `_prospectingData`, `_prospectCapacity`, `_expandedAccounts`, `_prospectAbort`, `_selectedProspectSiteId`, `_prospectMiniListIds`
- Functions: `setProspectingTab`, `showProspecting`, `_loadCapacityBar`, `_renderCapacityBar`, `loadProspecting`, `renderProspecting`, `_renderProspectingMobile`, `_healthBadge`, `toggleAccountExpand`, `_loadAccountSites`, `releaseSite`, `releaseStaleSites`, `claimSite`, `openProspectDrawer`, `closeProspectDrawer`, `switchProspectDrawerTab`, `_renderProspectDrawerOverview`, `_renderProspectDrawerContacts`, `_renderProspectDrawerActivity`, `_renderProspectMiniList`, `_renderProspectMiniListFromSearch`, `_prospectMiniListKeyNav`
- Remove from window exports at bottom of crm.js

**Important:** Do NOT remove backend endpoints in `app/routers/v13_features/prospecting.py` — ownership sweep and at-risk emails depend on them.

**Step 3: Commit**

```bash
git add app/templates/index.html app/static/crm.js
git commit -m "refactor(prospecting): remove old site-ownership prospecting view, keep backend endpoints"
```

---

### Task 9: Run full test suite and coverage check

**Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All PASS

**Step 2: Check coverage**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: Coverage should not decrease

**Step 3: Fix any regressions and commit fixes**

---

### Task 10: Deploy and verify

**Step 1: Push, rebuild, deploy**

```bash
git push origin main
docker compose up -d --build
docker compose logs -f app
```

**Step 2: Manual verification checklist**

1. Click "Prospecting" in sidebar - card grid loads with filters
2. Test each filter: search, industry, size, revenue, region, fit score, readiness, source
3. Test sort options: fit score, readiness, composite, recently added, name
4. Click a card - detail drawer opens with full enrichment data
5. Claim a prospect - Company created, toast with CRM link appears
6. Click CRM link in toast - navigates to company in CRM
7. Dismiss a prospect - card fades out
8. Mobile view - cards and filters render correctly
