# Playwright Patterns Reference

## Contents
- Project structure
- TypeScript vs Python test placement
- HTMX-specific patterns
- Authentication patterns
- Anti-patterns

---

## Project Structure

Eight projects are defined in `playwright.config.ts`. Each maps to a `*.spec.ts` glob:

| Project | File | Purpose |
|---------|------|---------|
| `dead-ends` | `e2e/dead-ends.spec.ts` | All HTMX list partials return non-500 |
| `workflows` | `e2e/workflows.spec.ts` | Multi-step user journeys via `request` |
| `smoke` | `e2e/smoke.spec.ts` | Server health, static assets, API versioning |
| `api` | `e2e/api.spec.ts` | JSON API contract tests |
| `auth` | `e2e/auth.spec.ts` | Auth redirect flows |
| `accessibility` | `e2e/accessibility.spec.ts` | axe-core WCAG 2.1 AA |
| `visual` | `e2e/visual.spec.ts` | Screenshot regression |

Python E2E tests live in `tests/e2e/` and use pytest-playwright with the `authed_page` fixture.

---

## TypeScript vs Python

**TypeScript (`e2e/*.spec.ts`):** Use when testing via HTTP — no browser, no cookie needed. The `request` fixture is enough for HTMX partials, API endpoints, and status code checks.

**Python (`tests/e2e/`):** Use when you need a real browser session with authentication — Alpine.js state inspection, click interactions, back-button tests, route interception.

```typescript
// TypeScript: pure HTTP, no browser
test('partial returns HTML', async ({ request }) => {
  const res = await request.get('/v2/partials/quotes', {
    headers: { 'HX-Request': 'true' },
  });
  expect(res.status()).toBeLessThan(500);
  const html = await res.text();
  expect(html.length).toBeGreaterThan(50);
});
```

```python
# Python: browser + auth cookie
def test_quotes_tab_active(authed_page, base_url):
    authed_page.goto(f"{base_url}/v2/quotes", wait_until="networkidle")
    has_active = authed_page.evaluate(
        "() => document.querySelector(\"nav a[href='/v2/quotes']\")?.classList.contains('text-brand-500')"
    )
    assert has_active
```

---

## HTMX-Specific Patterns

HTMX partial routes require `HX-Request: true`. Without it, FastAPI returns 422 (missing header dependency) or redirects to the full page shell.

```typescript
// ALWAYS include HX-Request for partial endpoints
const res = await request.get('/v2/partials/requisitions', {
  headers: { 'HX-Request': 'true' },
});
```

When checking partial content, verify the response contains a known Alpine or HTMX marker, not just length:

```typescript
const html = await res.text();
expect(html).toContain('hx-get');        // Has HTMX attributes
expect(html).toContain('x-data');        // Has Alpine component
expect(html).not.toContain('Traceback'); // No Python exception
```

---

## Authentication Patterns

AvailAI uses Starlette `SessionMiddleware` with a signed cookie. The `make_session_cookie()` helper in `tests/e2e/conftest.py` builds a valid cookie without going through Azure OAuth.

```python
# Inject auth cookie before navigating
def authed_page(page, base_url):
    cookie_val = make_session_cookie(user_id=1)
    page.context.add_cookies([{
        "name": "session",
        "value": cookie_val,
        "url": base_url,
    }])
    return page
```

The secret key is read from the running Docker container first, then from `SESSION_SECRET` env, then falls back to the dev default. In CI without Docker, set `SESSION_SECRET`.

---

## Anti-Patterns

### WARNING: Asserting exact 200 status on partials

**The Problem:**
```typescript
// BAD — fails for unauthenticated requests (returns 307/401)
expect(res.status()).toBe(200);
```

**Why This Breaks:**
The test server runs without authentication. Partials requiring login return 307 or 401. A hard 200 assertion makes tests environment-dependent.

**The Fix:**
```typescript
// GOOD — accept auth redirect as valid behavior
expect(res.status()).toBeLessThan(500);
```

---

### WARNING: No HX-Request header on partial routes

**The Problem:**
```typescript
// BAD — partial routes check for HX-Request dependency
const res = await request.get('/v2/partials/vendors');
```

**Why This Breaks:**
The FastAPI dependency `require_htmx_request` (if present) returns 422. Without it, some routes return the full page shell instead of the partial fragment — inflating response size and breaking content assertions.

**The Fix:**
```typescript
// GOOD
const res = await request.get('/v2/partials/vendors', {
  headers: { 'HX-Request': 'true' },
});
```

---

### WARNING: Skipping `wait_for_load_state` after HTMX navigation

**The Problem:**
```python
# BAD — Alpine state may not be updated yet
authed_page.locator("nav a[href='/v2/materials']").click()
cv = authed_page.evaluate("() => document.body._x_dataStack?.[0]?.currentView")
```

**Why This Breaks:**
HTMX fires the swap asynchronously. Alpine's reactive `:class` bindings update after the swap. Reading `currentView` immediately after a click returns the previous value.

**The Fix:**
```python
# GOOD — wait for network idle then allow Alpine reactivity
authed_page.locator("nav a[href='/v2/materials']").click()
authed_page.wait_for_load_state("networkidle")
authed_page.wait_for_timeout(300)
cv = authed_page.evaluate("() => document.body._x_dataStack?.[0]?.currentView || ''")
assert cv == "materials"
