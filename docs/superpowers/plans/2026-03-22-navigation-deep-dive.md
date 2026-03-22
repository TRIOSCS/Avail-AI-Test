# Navigation Deep Dive Test & Repair — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Systematically test every navigation path, fix all bugs, clean up dead code, and leave a regression test suite.

**Architecture:** Fix server-side route mapping (htmx_views.py) and client-side URL→view mapping (htmx_app.js) to be consistent. Remove optimistic `@click` nav state updates in favor of event-driven sync that already exists. Delete dead template files. Validate with Playwright E2E tests.

**Tech Stack:** Python/FastAPI (htmx_views.py), JavaScript (htmx_app.js), Jinja2 (base.html), Playwright (E2E tests)

**Spec:** `docs/superpowers/specs/2026-03-22-navigation-deep-dive-design.md`

---

### Task 1: Write Playwright smoke tests (pre-fix baseline)

**Files:**
- Create: `tests/e2e/test_navigation_smoke.py`
- Reference: `tests/e2e/conftest.py` (authed_page fixture, base_url)

- [ ] **Step 1: Create the navigation smoke test file**

```python
"""
test_navigation_smoke.py — Playwright smoke tests for bottom nav navigation.

Verifies: every nav link loads, URL updates correctly, currentView syncs,
active styling applies, back button works, direct URL access works.

Called by: pytest tests/e2e/test_navigation_smoke.py
Depends on: tests/e2e/conftest.py (authed_page, base_url fixtures)
"""

import pytest
from playwright.sync_api import Page, expect

# 12 bottom nav items (requisitions hardcoded + 11 in loop): (id, push_url, partial_url)
# Note: trouble-tickets is NOT in the bottom nav — it's accessed via /v2/trouble-tickets directly.
NAV_ITEMS = [
    ("requisitions", "/v2/requisitions", "/v2/partials/parts/workspace"),
    ("search", "/v2/search", "/v2/partials/search"),
    ("quotes", "/v2/quotes", "/v2/partials/quotes"),
    ("companies", "/v2/companies", "/v2/partials/companies"),
    ("vendors", "/v2/vendors", "/v2/partials/vendors"),
    ("prospecting", "/v2/prospecting", "/v2/partials/prospecting"),
    ("materials", "/v2/materials", "/v2/partials/materials/workspace"),
    ("buy-plans", "/v2/buy-plans", "/v2/partials/buy-plans"),
    ("follow-ups", "/v2/follow-ups", "/v2/partials/follow-ups"),
    ("proactive", "/v2/proactive", "/v2/partials/proactive"),
    ("excess", "/v2/excess", "/v2/partials/excess"),
    ("settings", "/v2/settings", "/v2/partials/settings"),
]

# Pages accessible via direct URL but not in bottom nav
DIRECT_ACCESS_PAGES = [
    ("trouble-tickets", "/v2/trouble-tickets", "/v2/partials/trouble-tickets/workspace"),
]


def _get_current_view(page: Page) -> str:
    """Read Alpine currentView from body x-data."""
    return page.evaluate("() => document.body._x_dataStack?.[0]?.currentView || ''")


def _wait_for_nav(page: Page):
    """Wait for HTMX swap to complete."""
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)


class TestNavClickLoadsCorrectly:
    """Click each nav item and verify URL, currentView, and HTTP status."""

    @pytest.mark.parametrize("nav_id, push_url, partial_url", NAV_ITEMS)
    def test_nav_click(self, authed_page: Page, base_url: str, nav_id: str, push_url: str, partial_url: str):
        # Start from requisitions
        authed_page.goto(f"{base_url}/v2/requisitions", wait_until="networkidle")
        authed_page.wait_for_timeout(500)

        if nav_id == "requisitions":
            return  # Already here

        # Intercept the partial request to check status
        with authed_page.expect_response(lambda r: partial_url in r.url) as response_info:
            # Click the nav link
            nav_link = authed_page.locator(f"nav a[href='{push_url}']")
            nav_link.click()
            _wait_for_nav(authed_page)

        # Verify HTTP 200
        assert response_info.value.status == 200, f"{nav_id}: got {response_info.value.status}"

        # Verify URL
        assert authed_page.url.endswith(push_url), f"{nav_id}: URL is {authed_page.url}"

        # Verify currentView
        cv = _get_current_view(authed_page)
        assert cv == nav_id, f"{nav_id}: currentView is '{cv}'"


class TestActiveStyleApplied:
    """Verify the active nav item gets brand-600 styling."""

    @pytest.mark.parametrize("nav_id, push_url, partial_url", NAV_ITEMS)
    def test_active_class(self, authed_page: Page, base_url: str, nav_id: str, push_url: str, partial_url: str):
        authed_page.goto(f"{base_url}{push_url}", wait_until="networkidle")
        authed_page.wait_for_timeout(500)

        nav_link = authed_page.locator(f"nav a[href='{push_url}']")
        classes = nav_link.get_attribute("class") or ""
        # Alpine reactive :class should have applied text-brand-500
        # We check via evaluate since :class is dynamic
        has_active = authed_page.evaluate(
            f"""() => {{
                const link = document.querySelector("nav a[href='{push_url}']");
                return link ? link.classList.contains('text-brand-500') : false;
            }}"""
        )
        assert has_active, f"{nav_id}: missing active class at {push_url}"


class TestBackButton:
    """Navigate A→B, press back, verify A is restored."""

    def test_back_restores_view(self, authed_page: Page, base_url: str):
        # Go to vendors
        authed_page.goto(f"{base_url}/v2/vendors", wait_until="networkidle")
        authed_page.wait_for_timeout(500)

        # Click materials
        authed_page.locator("nav a[href='/v2/materials']").click()
        _wait_for_nav(authed_page)
        assert authed_page.url.endswith("/v2/materials")

        # Press back
        authed_page.go_back()
        authed_page.wait_for_timeout(500)

        # Should be back at vendors
        assert "/v2/vendors" in authed_page.url
        assert _get_current_view(authed_page) == "vendors"


class TestDirectUrlAccess:
    """Hit each /v2/{page} directly — full page load should work."""

    @pytest.mark.parametrize("nav_id, push_url, partial_url", NAV_ITEMS + DIRECT_ACCESS_PAGES)
    def test_direct_access(self, authed_page: Page, base_url: str, nav_id: str, push_url: str, partial_url: str):
        response = authed_page.goto(f"{base_url}{push_url}", wait_until="networkidle")
        assert response.status == 200, f"{nav_id}: direct access got {response.status}"
        authed_page.wait_for_timeout(500)

        cv = _get_current_view(authed_page)
        assert cv == nav_id, f"{nav_id}: direct access currentView is '{cv}'"


class TestErrorRecovery:
    """After a failed nav request, currentView should stay on the previous value."""

    def test_failed_request_keeps_previous_view(self, authed_page: Page, base_url: str):
        # Start at vendors
        authed_page.goto(f"{base_url}/v2/vendors", wait_until="networkidle")
        authed_page.wait_for_timeout(500)
        assert _get_current_view(authed_page) == "vendors"

        # Intercept the materials partial to return 500
        authed_page.route("**/v2/partials/materials/workspace", lambda route: route.fulfill(status=500, body="error"))

        # Click materials nav
        authed_page.locator("nav a[href='/v2/materials']").click()
        authed_page.wait_for_timeout(1000)

        # currentView should still be "vendors" since the request failed
        cv = _get_current_view(authed_page)
        assert cv == "vendors", f"After failed request, currentView is '{cv}' (expected 'vendors')"

        # Clean up route intercept
        authed_page.unroute("**/v2/partials/materials/workspace")
```

- [ ] **Step 2: Run the smoke tests to establish baseline (expect failures)**

Run: `cd /root/availai && python -m pytest tests/e2e/test_navigation_smoke.py -v --timeout=120 2>&1 | tail -40`

Expected failures:
- `trouble-tickets` direct access: `currentView` will be `'tickets'` not `'trouble-tickets'` (Bug 4)
- Back button test may show wrong `currentView` for some pages due to missing `_viewFromPath()` patterns (Bug 2)
- Error recovery test: `currentView` will incorrectly change due to optimistic `@click` (Bug 1)

Record which tests fail.

- [ ] **Step 3: Commit baseline tests**

```bash
cd /root/availai
git add tests/e2e/test_navigation_smoke.py
git commit -m "test: add Playwright navigation smoke tests (baseline, some expected failures)"
```

---

### Task 2: Fix `_viewFromPath()` — add missing routes, remove stale ones (Bugs 2 & 3)

**Files:**
- Modify: `app/static/htmx_app.js:164-179`
- Modify: `tests/test_browser_back_navigation.py:55-73`

- [ ] **Step 1: Update the existing unit test to expect the correct nav sections**

In `tests/test_browser_back_navigation.py`, replace the parametrize list in `TestViewFromPathCoverage`:

```python
    @pytest.mark.parametrize(
        "section",
        [
            "buy-plans",
            "quotes",
            "prospecting",
            "proactive",
            "settings",
            "vendors",
            "companies",
            "search",
            "requisitions",
            "trouble-tickets",
            "excess",
            "follow-ups",
            "materials",
        ],
    )
```

This removes `strategic`, `my-vendors`, `tasks` and adds `trouble-tickets`, `excess`, `follow-ups`, `materials`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_browser_back_navigation.py::TestViewFromPathCoverage -v`

Expected: FAIL for `trouble-tickets`, `excess`, `follow-ups`, `materials` (not yet in JS).

- [ ] **Step 3: Fix `_viewFromPath()` in htmx_app.js**

Replace lines 164-179 with:

```javascript
// ── Derive currentView from URL path ────────────────────────
// SYNC: These must match the nav item IDs in htmx/base.html bottom_items list.
function _viewFromPath(path) {
    if (/\/buy-plans(\/|$)/.test(path)) return 'buy-plans';
    if (/\/trouble-tickets(\/|$)/.test(path)) return 'trouble-tickets';
    if (/\/follow-ups(\/|$)/.test(path)) return 'follow-ups';
    if (/\/quotes(\/|$)/.test(path)) return 'quotes';
    if (/\/prospecting(\/|$)/.test(path)) return 'prospecting';
    if (/\/proactive(\/|$)/.test(path)) return 'proactive';
    if (/\/settings(\/|$)/.test(path)) return 'settings';
    if (/\/vendors(\/|$)/.test(path)) return 'vendors';
    if (/\/companies(\/|$)/.test(path)) return 'companies';
    if (/\/search(\/|$)/.test(path)) return 'search';
    if (/\/excess(\/|$)/.test(path)) return 'excess';
    if (/\/materials(\/|$)/.test(path)) return 'materials';
    if (/\/requisitions(\/|$)/.test(path)) return 'requisitions';
    return 'requisitions';
}
```

Changes: removed `strategic`, `my-vendors`, `tasks`. Added `trouble-tickets`, `follow-ups`, `excess`, `materials`. Added SYNC comment.

- [ ] **Step 4: Verify no live references to removed routes**

Run: `grep -rn "my-vendors\|/strategic\|/v2/tasks" app/templates/ app/routers/`

Expected: No matches (confirming safe to remove).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_browser_back_navigation.py -v`

Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
cd /root/availai
git add app/static/htmx_app.js tests/test_browser_back_navigation.py
git commit -m "fix(nav): sync _viewFromPath() with actual nav items — add 4 missing, remove 3 stale"
```

---

### Task 3: Fix trouble-tickets naming mismatch in htmx_views.py (Bug 4)

**Files:**
- Modify: `app/routers/htmx_views.py:314-315, 331-332, 364`

- [ ] **Step 1: Fix `current_view` assignment**

In `htmx_views.py`, change line 315:
```python
# Before:
        current_view = "tickets"
# After:
        current_view = "trouble-tickets"
```

- [ ] **Step 2: Fix partial URL mapping**

Change line 331:
```python
# Before:
    elif current_view == "tickets":
# After:
    elif current_view == "trouble-tickets":
```

- [ ] **Step 3: Fix detail route**

Change line 364:
```python
# Before:
    elif current_view == "tickets" and "/trouble-tickets/" in path:
# After:
    elif current_view == "trouble-tickets" and "/trouble-tickets/" in path:
```

- [ ] **Step 4: Verify no other references to `"tickets"` as a view name**

Run: `grep -n '"tickets"' app/routers/htmx_views.py`

Expected: No matches.

- [ ] **Step 5: Commit**

```bash
cd /root/availai
git add app/routers/htmx_views.py
git commit -m "fix(nav): rename current_view 'tickets' to 'trouble-tickets' to match nav item id"
```

---

### Task 4: Remove optimistic `@click` nav updates (Bug 1)

**Files:**
- Modify: `app/templates/htmx/base.html:42, 132, 154`

- [ ] **Step 1: Remove `@click` from the logo link**

In `base.html` line 41-42, remove `@click="currentView = 'requisitions'"`:

```html
<!-- Before: -->
      <a href="/v2" hx-get="/v2/partials/parts/workspace" hx-target="#main-content" hx-push-url="/v2/requisitions"
         @click="currentView = 'requisitions'"
         class="flex items-center">

<!-- After: -->
      <a href="/v2" hx-get="/v2/partials/parts/workspace" hx-target="#main-content" hx-push-url="/v2/requisitions"
         class="flex items-center">
```

- [ ] **Step 2: Remove `@click` from the requisitions nav link**

In `base.html` line 132, remove `@click="currentView = 'requisitions'"`:

```html
<!-- Before: -->
         @click="currentView = 'requisitions'"
         :class="currentView === 'requisitions' ? ...

<!-- After: -->
         :class="currentView === 'requisitions' ? ...
```

- [ ] **Step 3: Remove `@click` from the nav loop**

In `base.html` line 154, remove `@click="currentView = '{{ id }}'"`:

```html
<!-- Before: -->
      <a href="{{ href }}" hx-get="{{ partial }}" hx-target="#main-content" hx-push-url="{{ href }}"
         @click="currentView = '{{ id }}'"
         :class="currentView === '{{ id }}' ? ...

<!-- After: -->
      <a href="{{ href }}" hx-get="{{ partial }}" hx-target="#main-content" hx-push-url="{{ href }}"
         :class="currentView === '{{ id }}' ? ...
```

- [ ] **Step 4: Verify `_syncSidebarToUrl()` handles nav updates**

The existing `htmx:pushedIntoHistory` listener in `htmx_app.js:194-196` already calls `_syncSidebarToUrl()` after every successful HTMX navigation. This replaces the removed `@click` handlers.

Run: `grep -n "pushedIntoHistory" app/static/htmx_app.js` — confirm it exists.

- [ ] **Step 5: Commit**

```bash
cd /root/availai
git add app/templates/htmx/base.html
git commit -m "fix(nav): remove optimistic @click currentView updates — rely on htmx:pushedIntoHistory"
```

---

### Task 5: DRY up detail route parsing in htmx_views.py

**Files:**
- Modify: `app/routers/htmx_views.py:335-367`

- [ ] **Step 1: Replace repetitive elif blocks with a loop**

Replace lines 335-367 (the entire detail route parsing block) with:

```python
    # Detail view override — extract numeric ID from URL and point to detail partial.
    # Runs after workspace defaults are set, so only overrides when /{segment}/{id} is present.
    # Note: the original elif chain also checked current_view == seg, but that guard is redundant
    # since current_view is derived from the same path variable. This simplification is intentional.
    _DETAIL_SEGMENTS = [
        "requisitions", "vendors", "companies", "buy-plans",
        "excess", "quotes", "prospecting", "trouble-tickets", "materials",
    ]
    for seg in _DETAIL_SEGMENTS:
        if f"/{seg}/" in path:
            tail = path.split(f"/{seg}/", 1)[1].split("/")[0]
            if tail.isdigit():
                partial_url = f"/v2/partials/{seg}/{tail}"
            break
```

- [ ] **Step 2: Run the existing htmx_views tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "htmx_view" -v 2>&1 | tail -20`

Expected: All pass (the detail routing logic is functionally equivalent).

- [ ] **Step 3: Commit**

```bash
cd /root/availai
git add app/routers/htmx_views.py
git commit -m "refactor(nav): DRY up detail route parsing with loop over segments"
```

---

### Task 6: Delete dead template files

**Files:**
- Delete: `app/templates/base.html`
- Delete: `app/templates/htmx/partials/shared/mobile_nav.html`

- [ ] **Step 1: Verify no live references to base.html (non-htmx)**

Run: `grep -rn "base.html" app/ --include="*.py" | grep -v "htmx/base" | grep -v __pycache__`

Confirm only `htmx/base.html` and `htmx/base_page.html` are referenced in Python code.

Run: `grep -rn 'base.html' app/templates/ | grep -v "htmx/"` — should only show self-references.

- [ ] **Step 2: Verify no live references to mobile_nav.html**

Run: `grep -rn "mobile_nav" app/ --include="*.py"` — should return nothing.
Run: `grep -rn "mobile_nav" app/templates/ | grep -v mobile_nav.html` — should return nothing (only the old base.html includes it, which we're also deleting).

- [ ] **Step 3: Delete the files**

```bash
cd /root/availai
rm app/templates/base.html
rm app/templates/htmx/partials/shared/mobile_nav.html
```

- [ ] **Step 4: Commit**

```bash
cd /root/availai
git add -u app/templates/base.html app/templates/htmx/partials/shared/mobile_nav.html
git commit -m "chore(nav): delete dead base.html and mobile_nav.html templates"
```

---

### Task 7: Add cross-reference comment in base.html

**Files:**
- Modify: `app/templates/htmx/base.html:123`

- [ ] **Step 1: Add sync comment above the bottom nav**

Before the bottom nav `<nav>` tag (line 123), add:

```html
  {# ── Bottom navigation bar (all screen sizes) ───────────────
       SYNC: Nav item IDs must match _viewFromPath() in htmx_app.js
       and current_view values in htmx_views.py:v2_page(). ──────── #}
```

- [ ] **Step 2: Commit**

```bash
cd /root/availai
git add app/templates/htmx/base.html
git commit -m "docs(nav): add cross-reference comments linking nav items, JS, and Python"
```

---

### Task 8: Rebuild and run Playwright regression tests

**Files:**
- Reference: `tests/e2e/test_navigation_smoke.py`

- [ ] **Step 1: Rebuild the Docker app with all fixes**

```bash
cd /root/availai && docker compose up -d --build
```

Wait for app to be healthy. Check logs:
```bash
docker compose logs -f app 2>&1 | head -20
```

- [ ] **Step 2: Run the full navigation smoke test suite**

```bash
cd /root/availai && python -m pytest tests/e2e/test_navigation_smoke.py -v --timeout=120
```

Expected: All tests PASS now that bugs are fixed.

- [ ] **Step 3: Run the unit test suite for navigation**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_browser_back_navigation.py -v
```

Expected: All PASS.

- [ ] **Step 4: Run the full test suite**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=120 2>&1 | tail -20
```

Expected: No regressions.
