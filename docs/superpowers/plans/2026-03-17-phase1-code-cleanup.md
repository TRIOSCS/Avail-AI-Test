# Phase 1: Code Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete legacy requisitions2 code, consolidate to single template tree, rename Companies → Customers.

**Architecture:** The frontend currently has one monolithic router (`htmx_views.py`, 3700+ lines) that serves all `/v2` routes. The `app/routers/htmx/*.py` sub-modules (6,444 lines total) exist but are NEVER imported — they are unused dead code from a planned refactoring. Templates live in two trees: `htmx/partials/` (newer) and `partials/` (older, still actively referenced by htmx_views.py). We consolidate to one tree and remove all dead code.

**Tech Stack:** FastAPI, Jinja2, HTMX, Python

**Key files:**
- `app/main.py` — router imports (lines 599-668)
- `app/routers/htmx_views.py` — monolithic 3700+ line router (THE router)
- `app/routers/htmx/*.py` — 13 unused sub-modules (dead code)
- `app/routers/requisitions2.py` — legacy router
- `app/templates/partials/` — 77 templates to move into `htmx/partials/`
- `app/templates/requisitions2/` — 8 templates to delete
- `app/templates/htmx/base.html` — sidebar nav (companies → customers rename)

---

### Task 1: Delete Requisitions2 (Dead Code)

**Files:**
- Delete: `app/routers/requisitions2.py`
- Delete: `app/templates/requisitions2/` (8 files)
- Delete: `app/static/js/requisitions2.js`
- Delete: `tests/test_requisitions2_routes.py`
- Delete: `tests/test_requisitions2_schemas.py`
- Delete: `tests/test_requisitions2_templates.py`
- Delete: `tests/frontend/requisitions2.e2e.test.mjs`
- Modify: `app/main.py:620,655` — remove requisitions2 import and include_router
- Modify: `Dockerfile` — remove `RUN cp -r app/static/js/ app/static/dist/js/` line

- [ ] **Step 1: Remove requisitions2 router from main.py**

In `app/main.py`, delete line 620 (the import) and line 655 (the include_router call):
```python
# DELETE: from .routers.requisitions2 import router as rq2_router
# DELETE: app.include_router(rq2_router)
```

- [ ] **Step 2: Delete requisitions2 router file**

```bash
rm app/routers/requisitions2.py
```

- [ ] **Step 3: Delete requisitions2 templates**

```bash
rm -rf app/templates/requisitions2/
```

- [ ] **Step 4: Delete requisitions2 JS**

```bash
rm app/static/js/requisitions2.js
```

- [ ] **Step 5: Delete requisitions2 test files**

```bash
rm tests/test_requisitions2_routes.py
rm tests/test_requisitions2_schemas.py
rm tests/test_requisitions2_templates.py
rm tests/frontend/requisitions2.e2e.test.mjs
```

- [ ] **Step 6: Remove Dockerfile JS copy line**

In `Dockerfile`, delete the line:
```dockerfile
# DELETE: RUN cp -r app/static/js/ app/static/dist/js/ 2>/dev/null || true
```

- [ ] **Step 7: Update any remaining redirects to /requisitions2**

Search for redirects in `app/routers/auth.py` and `app/routers/htmx_views.py`. Change any `RedirectResponse(url="/requisitions2"...)` to `RedirectResponse(url="/v2/requisitions"...)`.

Check: `grep -rn "requisitions2" app/routers/`

- [ ] **Step 8: Run tests to verify nothing breaks**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: All remaining tests pass (deleted tests won't run).

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "cleanup: delete legacy requisitions2 router, templates, JS, and tests"
```

---

### Task 2: Delete Unused htmx/ Sub-modules

The `app/routers/htmx/*.py` modules (6,444 lines) are never imported or mounted. They are dead code.

**Files:**
- Delete: `app/routers/htmx/` (entire directory — 15 files)

- [ ] **Step 1: Verify htmx/ modules are truly unused**

```bash
grep -rn "from.*routers.htmx" app/ --include="*.py" | grep -v "htmx_views"
grep -rn "htmx_router\|htmx\.router\|from.*htmx import" app/main.py
```

Expected: No results (confirming these modules are never imported).

- [ ] **Step 2: Delete the entire htmx/ directory**

```bash
rm -rf app/routers/htmx/
```

- [ ] **Step 3: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "cleanup: delete unused htmx/ sub-modules (6,444 lines of dead code)"
```

---

### Task 3: Consolidate Templates (partials/ → htmx/partials/)

Move all templates from `app/templates/partials/` into `app/templates/htmx/partials/`, then update every `TemplateResponse` path in `htmx_views.py`.

**Files:**
- Move: `app/templates/partials/*` → `app/templates/htmx/partials/`
- Modify: `app/routers/htmx_views.py` — update all template paths
- Delete: `app/templates/partials/` (after move)

- [ ] **Step 1: Identify all template paths referenced in htmx_views.py**

```bash
grep -n 'TemplateResponse.*"partials/' app/routers/htmx_views.py | head -60
```

This gives every line that references `partials/` (without `htmx/` prefix). These are the paths to update.

- [ ] **Step 2: Move templates, merging with existing htmx/partials/ directories**

For each subdirectory in `partials/`, copy files into the corresponding `htmx/partials/` subdirectory. Where subdirectories already exist in `htmx/partials/`, merge (no filename conflicts — verified in audit, except 3 search/ duplicates where we keep the htmx version).

```bash
# Domains that DON'T exist in htmx/partials/ yet — straight move
for dir in admin emails follow_ups knowledge offers sourcing; do
  cp -r app/templates/partials/$dir/ app/templates/htmx/partials/$dir/
done

# Domains that DO exist in htmx/partials/ — merge files in
for dir in materials proactive prospecting quotes requisitions search shared vendors; do
  cp -rn app/templates/partials/$dir/* app/templates/htmx/partials/$dir/ 2>/dev/null || true
done

# Companies → will be renamed to customers in Task 4, move for now
cp -r app/templates/partials/companies/ app/templates/htmx/partials/companies/
```

Note: `cp -rn` = no-clobber, preserves existing htmx/partials versions when filenames overlap.

- [ ] **Step 3: Update all template paths in htmx_views.py**

In `app/routers/htmx_views.py`, replace every `"partials/` with `"htmx/partials/` using find-and-replace. Only change lines that DON'T already have the `htmx/` prefix.

Search pattern: `TemplateResponse("partials/` → `TemplateResponse("htmx/partials/`

Verify with:
```bash
grep -c '"partials/' app/routers/htmx_views.py  # Should be 0 after
grep -c '"htmx/partials/' app/routers/htmx_views.py  # Should match total
```

- [ ] **Step 4: Delete old partials/ directory**

```bash
rm -rf app/templates/partials/
```

- [ ] **Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "cleanup: consolidate all templates into htmx/partials/ tree"
```

---

### Task 4: Rename Companies → Customers

**Files:**
- Rename: `app/templates/htmx/partials/companies/` → `app/templates/htmx/partials/customers/`
- Modify: `app/routers/htmx_views.py` — all company route paths, function names, template refs, context vars
- Modify: `app/templates/htmx/base.html` — sidebar nav items
- Modify: Test files — URL paths

- [ ] **Step 1: Rename template directory**

```bash
mv app/templates/htmx/partials/companies/ app/templates/htmx/partials/customers/
```

- [ ] **Step 2: Update sidebar nav in base.html**

In `app/templates/htmx/base.html`, find all nav tuples referencing `companies` and update:
- `'/v2/companies'` → `'/v2/customers'`
- `'/v2/partials/companies'` → `'/v2/partials/customers'`
- Keep the display label as `'Customers'` (already says Customers in some places, verify all)

- [ ] **Step 3: Update route paths in htmx_views.py**

In `app/routers/htmx_views.py`, replace all route decorators:
- `"/v2/companies"` → `"/v2/customers"`
- `"/v2/companies/{company_id:int}"` → `"/v2/customers/{company_id:int}"`
- `"/v2/partials/companies"` → `"/v2/partials/customers"`
- `"/v2/partials/companies/create-form"` → `"/v2/partials/customers/create-form"`
- `"/v2/partials/companies/create"` → `"/v2/partials/customers/create"`
- `"/v2/partials/companies/typeahead"` → `"/v2/partials/customers/typeahead"`
- `"/v2/partials/companies/check-duplicate"` → `"/v2/partials/customers/check-duplicate"`
- `"/v2/partials/companies/{company_id}"` → `"/v2/partials/customers/{company_id}"`
- `"/v2/partials/companies/{company_id}/tab/{tab}"` → `"/v2/partials/customers/{company_id}/tab/{tab}"`
- All similar patterns

- [ ] **Step 4: Update template paths in htmx_views.py**

Replace all template references:
- `"htmx/partials/companies/` → `"htmx/partials/customers/`

- [ ] **Step 5: Update context variables and view detection**

In `htmx_views.py`, update:
- `current_view = "companies"` → `current_view = "customers"`
- `"/companies" in path` → `"/customers" in path`
- `.split("/companies/")` → `.split("/customers/")`
- `f"/v2/partials/companies/` → `f"/v2/partials/customers/`

- [ ] **Step 6: Add redirect from old URL**

Add a redirect route at the top of the companies section in htmx_views.py:

```python
@router.get("/v2/companies", response_class=HTMLResponse)
@router.get("/v2/companies/{path:path}", response_class=HTMLResponse)
async def companies_redirect(request: Request, path: str = ""):
    """Redirect old /v2/companies URLs to /v2/customers."""
    from fastapi.responses import RedirectResponse
    new_url = f"/v2/customers/{path}" if path else "/v2/customers"
    return RedirectResponse(url=new_url, status_code=301)
```

- [ ] **Step 7: Update test files**

In test files, replace `/v2/companies` with `/v2/customers` and `/v2/partials/companies` with `/v2/partials/customers`:

```bash
grep -rln "v2/companies\|v2/partials/companies" tests/
```

Update each file found.

- [ ] **Step 8: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: All tests pass.

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "rename: Companies → Customers across routes, templates, and tests"
```

---

### Task 5: Delete Legacy base.html

**Files:**
- Delete: `app/templates/base.html` (old SPA shell, unused)

- [ ] **Step 1: Verify base.html is unused**

```bash
grep -rn '"base.html"' app/routers/ --include="*.py"
grep -rn "base.html" app/templates/ --include="*.html" | grep -v htmx
```

Expected: No active references (only htmx/base.html and htmx/base_page.html are used).

- [ ] **Step 2: Delete it**

```bash
rm app/templates/base.html
```

- [ ] **Step 3: Run tests and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
git add -A && git commit -m "cleanup: delete unused legacy base.html template"
```

---

### Task 6: Final Verification & Deploy

- [ ] **Step 1: Run full test suite with coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Expected: No coverage regression. All tests pass.

- [ ] **Step 2: Verify template tree is clean**

```bash
# Should only have htmx/ and documents/ under templates
ls app/templates/
# Expected: htmx/  documents/

# No more partials/ or requisitions2/
ls app/templates/partials/ 2>&1    # Should error: no such directory
ls app/templates/requisitions2/ 2>&1  # Should error: no such directory
```

- [ ] **Step 3: Verify no broken template references**

```bash
# Every TemplateResponse should reference htmx/ or documents/
grep -n 'TemplateResponse' app/routers/htmx_views.py | grep -v 'htmx/' | grep -v 'documents/'
```

Expected: No results (all templates point to htmx/ tree).

- [ ] **Step 4: Push and deploy**

```bash
git push origin main
docker compose up -d --build
docker compose logs app --tail 10
```

Expected: Clean startup, no template errors in logs.

- [ ] **Step 5: Smoke test in browser**

Verify these pages load without errors:
- `/v2/requisitions`
- `/v2/customers` (renamed from companies)
- `/v2/vendors`
- `/v2/search`
- `/v2/quotes`
- `/v2/settings`
- `/v2/companies` → redirects to `/v2/customers`
