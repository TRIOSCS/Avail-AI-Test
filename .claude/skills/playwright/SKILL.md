---
name: playwright
description: |
  Implements end-to-end tests with Playwright for the AvailAI FastAPI + HTMX stack.
  Use when: writing new E2E specs, adding HTMX partial coverage, testing authenticated
  workflows, running accessibility audits, debugging dead-end partials, or extending
  the dead-ends/workflows/accessibility/visual test projects.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__plugin_context7_context7__resolve-library-id, mcp__plugin_context7_context7__query-docs, mcp__playwright__browser_close, mcp__playwright__browser_resize, mcp__playwright__browser_console_messages, mcp__playwright__browser_handle_dialog, mcp__playwright__browser_evaluate, mcp__playwright__browser_file_upload, mcp__playwright__browser_fill_form, mcp__playwright__browser_install, mcp__playwright__browser_press_key, mcp__playwright__browser_type, mcp__playwright__browser_navigate, mcp__playwright__browser_navigate_back, mcp__playwright__browser_network_requests, mcp__playwright__browser_run_code, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_snapshot, mcp__playwright__browser_click, mcp__playwright__browser_drag, mcp__playwright__browser_hover, mcp__playwright__browser_select_option, mcp__playwright__browser_tabs, mcp__playwright__browser_wait_for
---

# Playwright

AvailAI's E2E suite uses **TypeScript specs** in `e2e/` (Playwright test runner) and **Python specs** in `tests/e2e/` (pytest-playwright). The TypeScript side tests the API and HTMX partials via `request` fixtures — no browser needed. The Python side uses `authed_page` (Starlette session cookie injection) to test authenticated browser workflows. The app server is spun up automatically by `playwright.config.ts` using `TESTING=1 sqlite://`.

## Quick Start

### Run a project

```bash
npx playwright test --project=dead-ends     # All HTMX partials return non-500
npx playwright test --project=workflows     # Multi-step user journeys
npx playwright test --project=accessibility # WCAG 2.1 AA via axe-core
npx playwright test --project=smoke         # Server health + static assets
npx playwright test --project=visual        # Screenshot regression
```

### Authenticated Python test

```python
# tests/e2e/conftest.py provides authed_page — cookie pre-injected
def test_vendors_loads(authed_page, base_url):
    authed_page.goto(f"{base_url}/v2/vendors", wait_until="networkidle")
    assert authed_page.url.endswith("/v2/vendors")
```

### HTMX partial check (TypeScript)

```typescript
// Always send HX-Request: true for partial endpoints
const res = await request.get('/v2/partials/vendors', {
  headers: { 'HX-Request': 'true' },
});
expect(res.status()).toBeLessThan(500);
```

## Key Concepts

| Concept | Usage | Notes |
|---------|-------|-------|
| Projects | 8 named projects in `playwright.config.ts` | dead-ends, workflows, smoke, api, auth, data-validation, accessibility, visual |
| `authed_page` | Python fixture: signed Starlette cookie for user_id=1 | Skip Azure OAuth in E2E tests |
| `HX-Request: true` | Header required for HTMX partial routes | Without it, server returns 422 or redirects |
| `TESTING=1` | Disables scheduler + real API calls | Set in `webServer` command |
| `page.route()` | Intercept + mock HTMX partial responses | Use to test error recovery flows |

## Common Patterns

### Intercept a partial to simulate 500

```typescript
page.route('**/v2/partials/materials/workspace', route =>
  route.fulfill({ status: 500, body: 'error' })
);
```

### Read Alpine.js state from the DOM

```python
def _get_current_view(page) -> str:
    return page.evaluate("() => document.body._x_dataStack?.[0]?.currentView || ''")
```

### Wait for HTMX swap to complete

```python
page.wait_for_load_state("networkidle")
page.wait_for_timeout(300)  # Alpine reactivity lag
```

## See Also

- [patterns](references/patterns.md)
- [workflows](references/workflows.md)

## Related Skills

- See the **pytest** skill for Python fixture conventions and `conftest.py` patterns
- See the **htmx** skill for partial routing and `HX-Request` header requirements
- See the **fastapi** skill for route structure that determines which partials to test
- See the **vite** skill for `npm run build` smoke tests and bundle validation

## Documentation Resources

> Fetch latest Playwright documentation with Context7.

1. `mcp__plugin_context7_context7__resolve-library-id` → search "playwright"
2. Prefer `/websites/` IDs over source repos
3. `mcp__plugin_context7_context7__query-docs` with resolved ID

**Recommended queries:** "page fixtures", "request fixture API testing", "route interception", "expect assertions"
