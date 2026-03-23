# Playwright Workflows Reference

## Contents
- Adding a new test project
- Writing dead-end coverage for a new partial
- Testing an authenticated workflow (Python)
- Route interception for error recovery tests
- Accessibility audit workflow
- Running the full E2E suite

---

## Adding a New Test Project

1. Add a new project entry to `playwright.config.ts`:

```typescript
{ name: 'my-feature', testMatch: /my-feature\.spec\.ts$/ },
```

2. Create `e2e/my-feature.spec.ts`:

```typescript
import { test, expect } from '@playwright/test';

test.describe('My Feature', () => {
  test('key partial renders', async ({ request }) => {
    const res = await request.get('/v2/partials/my-feature', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});
```

3. Run it: `npx playwright test --project=my-feature`

---

## Writing Dead-End Coverage for a New Partial

Add the partial URL to the `LIST_PARTIALS` array in `e2e/dead-ends.spec.ts`:

```typescript
const LIST_PARTIALS = [
  // existing entries...
  '/v2/partials/my-new-feature',
];
```

The existing test loop generates a test case per URL automatically — no additional test code needed.

Checklist for new partial coverage:
- [ ] Add URL to `LIST_PARTIALS` in `e2e/dead-ends.spec.ts`
- [ ] Run `npx playwright test --project=dead-ends` — confirm new test appears
- [ ] Verify it passes (200 or auth redirect, not 500)
- [ ] If the partial requires path params, add a separate `workflows` test with a valid ID

---

## Testing an Authenticated Workflow (Python)

Use `tests/e2e/conftest.py`'s `authed_page` fixture. It pre-injects a signed Starlette session cookie for `user_id=1`.

```python
# tests/e2e/test_my_feature.py
import pytest
from playwright.sync_api import Page

def test_my_feature_page_loads(authed_page: Page, base_url: str):
    authed_page.goto(f"{base_url}/v2/my-feature", wait_until="networkidle")
    authed_page.wait_for_timeout(500)

    # Verify URL updated
    assert authed_page.url.endswith("/v2/my-feature")

    # Verify Alpine currentView synced
    cv = authed_page.evaluate("() => document.body._x_dataStack?.[0]?.currentView || ''")
    assert cv == "my-feature"

    # Verify nav item active
    has_active = authed_page.evaluate(
        "() => document.querySelector(\"nav a[href='/v2/my-feature']\")?.classList.contains('text-brand-500') ?? false"
    )
    assert has_active
```

Run: `pytest tests/e2e/test_my_feature.py -v`

---

## Route Interception for Error Recovery Tests

Use `page.route()` to intercept HTMX partial requests and simulate server errors:

```python
def test_failed_partial_keeps_previous_view(authed_page, base_url):
    # Start at a known view
    authed_page.goto(f"{base_url}/v2/vendors", wait_until="networkidle")
    authed_page.wait_for_timeout(500)
    assert authed_page.evaluate(
        "() => document.body._x_dataStack?.[0]?.currentView || ''"
    ) == "vendors"

    # Intercept the target partial
    authed_page.route(
        "**/v2/partials/materials/workspace",
        lambda route: route.fulfill(status=500, body="error")
    )

    authed_page.locator("nav a[href='/v2/materials']").click()
    authed_page.wait_for_timeout(1000)

    # currentView must NOT update on failure
    cv = authed_page.evaluate("() => document.body._x_dataStack?.[0]?.currentView || ''")
    assert cv == "vendors"

    # Always clean up route intercepts
    authed_page.unroute("**/v2/partials/materials/workspace")
```

---

## Accessibility Audit Workflow

The `accessibility` project uses `@axe-core/playwright`. The helper in `e2e/accessibility.spec.ts` wraps axe with WCAG 2.1 AA tags and disables `color-contrast` (Tailwind brand palette requires separate audit).

```typescript
import AxeBuilder from '@axe-core/playwright';

test('my page has no critical violations', async ({ page }) => {
  await page.goto('/v2/my-feature');
  await page.waitForLoadState('networkidle');

  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .disableRules(['color-contrast'])
    .analyze();

  const critical = results.violations.filter(
    v => v.impact === 'critical' || v.impact === 'serious'
  );
  expect(critical).toHaveLength(0);
});
```

Run: `npm run test:a11y` (runs `npx playwright test --project=accessibility`)

---

## Running the Full E2E Suite

The full suite requires the Docker stack to be up (for Python tests) and the test server started by `playwright.config.ts` (for TypeScript tests).

```bash
# TypeScript projects (test server auto-started by config)
npx playwright test --project=smoke
npx playwright test --project=dead-ends
npx playwright test --project=workflows
npx playwright test --project=accessibility

# Python projects (requires Docker app running)
pytest tests/e2e/ -v

# Combined (npm script)
npm run test:all-frontend
```

Validate → fix loop:
1. Run `npx playwright test --project=dead-ends`
2. If any partial returns 500, trace the route in `app/routers/htmx_views.py`
3. Fix the template or route handler
4. Repeat until all partials return `< 500`

See the **pytest** skill for Python fixture conventions. See the **htmx** skill for partial route structure. See the **vite** skill for bundle smoke tests run after `npm run build`.
