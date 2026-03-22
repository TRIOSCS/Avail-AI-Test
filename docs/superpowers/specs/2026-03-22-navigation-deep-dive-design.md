# Navigation Deep Dive Test & Repair

**Date**: 2026-03-22
**Scope**: Systematic test of all navigation paths, fix known bugs, clean up dead code

## Approach

Hybrid: Playwright smoke test → code audit + fix → Playwright regression test.

## 1. Playwright Smoke Test

### Per-nav-item tests (13 items)

Bottom nav items: `requisitions`, `search`, `quotes`, `companies`, `vendors`, `prospecting`, `materials`, `buy-plans`, `follow-ups`, `proactive`, `excess`, `settings`, `trouble-tickets`

For each item:
1. Click nav link → verify HTTP 200 response on the partial endpoint
2. Verify browser URL matches expected `hx-push-url` value
3. Verify `currentView` Alpine state matches the nav item id
4. Verify the clicked item has active styling class (`text-brand-600`)
5. Verify previously active item loses active styling

### Cross-cutting tests

- **Back button**: Navigate A → B → browser back. Verify URL reverts to A and `currentView` matches A.
- **Direct URL access**: Hit each `/v2/{page}` as a full page load. Verify page renders with correct `currentView` and correct partial loads.
- **Error recovery**: Intercept a request to force a failure. Verify `currentView` reverts to the previous value (currently broken — will fail before fix).

## 2. Known Bugs to Fix

### Bug 1: `currentView` not reverted on error/timeout

**Files**: `htmx_app.js:204-228`, `htmx/base.html:132,154`

**Problem**: `@click="currentView = '{{ id }}'"` fires immediately on click. If the HTMX request fails, times out, or has a network error, `currentView` stays on the new value — wrong nav item highlighted.

**Fix**: Instead of setting `currentView` on `@click`, set it on successful swap via `htmx:afterSwap`. The `_syncSidebarToUrl()` function already does this for `htmx:pushedIntoHistory`, but that only fires after URL push. The cleanest fix:
- Remove `@click="currentView = '{{ id }}'"` from nav links
- Rely on `htmx:pushedIntoHistory` → `_syncSidebarToUrl()` which already runs after every successful navigation
- This naturally handles errors because `pushedIntoHistory` only fires on success

### Bug 2: `_viewFromPath()` missing routes

**File**: `htmx_app.js:165-178`

**Problem**: The function is missing regex patterns for: `excess`, `follow-ups`, `materials`, `trouble-tickets`. These nav items will fall through to the default `'requisitions'` on back button or history restore.

**Fix**: Add the 4 missing patterns:
```javascript
if (/\/excess(\/|$)/.test(path)) return 'excess';
if (/\/follow-ups(\/|$)/.test(path)) return 'follow-ups';
if (/\/materials(\/|$)/.test(path)) return 'materials';
if (/\/trouble-tickets(\/|$)/.test(path)) return 'trouble-tickets';
```

### Bug 3: `_viewFromPath()` has stale routes

**File**: `htmx_app.js:170,172,176`

**Problem**: Contains patterns for `strategic`, `my-vendors`, `tasks` — none of these exist in the bottom nav or as `/v2/*` routes.

**Fix**: Remove all three. If `tasks` was meant to be `follow-ups`, the new pattern covers it.

### Bug 4: `current_view` naming mismatch for trouble-tickets

**File**: `htmx_views.py:315`

**Problem**: Server sets `current_view = "tickets"` but the bottom nav item id is `"trouble-tickets"` (from the Jinja loop in base.html line 139-150). On full page load of `/v2/trouble-tickets`, Alpine initializes with `currentView = 'tickets'` which doesn't match any nav item id — no item gets highlighted.

**Fix**: Change `htmx_views.py:315` from `current_view = "tickets"` to `current_view = "trouble-tickets"`. Also update the partial URL mapping at line 331-332 to use `"trouble-tickets"` as the key.

## 3. Dead Code Cleanup

| File | Status | Action |
|------|--------|--------|
| `app/templates/base.html` | Only used by old non-HTMX path; `htmx/base.html` is the active base | Delete |
| `app/templates/htmx/partials/shared/mobile_nav.html` | Only included by old `base.html` | Delete |

**Verification before deletion**: grep all templates and Python files for references to confirm no live code paths use them.

## 4. Structural Improvements

### DRY up detail route parsing (htmx_views.py:336-367)

Replace 8 repetitive `elif` blocks with a loop:

```python
_DETAIL_ROUTES = {
    "requisitions": "requisitions",
    "vendors": "vendors",
    "companies": "companies",
    "buy-plans": "buy-plans",
    "excess": "excess",
    "quotes": "quotes",
    "prospecting": "prospecting",
    "trouble-tickets": "trouble-tickets",
    "materials": "materials",
}

for url_segment, partial_segment in _DETAIL_ROUTES.items():
    if f"/{url_segment}/" in path:
        parts = path.split(f"/{url_segment}/")
        if len(parts) > 1 and parts[1].split("/")[0].isdigit():
            partial_url = f"/v2/partials/{partial_segment}/{parts[1].split('/')[0]}"
        break
```

### Cross-reference comment

Add a comment in `htmx_app.js` at `_viewFromPath()` referencing `base.html` bottom nav items, and vice versa, so future changes keep them in sync.

## 5. Regression Test

Re-run the full Playwright suite from Section 1. All tests should pass.

## Files Modified

| File | Changes |
|------|---------|
| `app/static/htmx_app.js` | Fix `_viewFromPath()`, remove stale routes, remove `@click` nav sync |
| `app/templates/htmx/base.html` | Remove `@click="currentView = ..."` from nav links |
| `app/routers/htmx_views.py` | Fix `tickets` → `trouble-tickets`, DRY detail routes |
| `app/templates/base.html` | Delete (dead code) |
| `app/templates/htmx/partials/shared/mobile_nav.html` | Delete (dead code) |
| `tests/test_navigation.py` | New — Playwright smoke/regression tests |
