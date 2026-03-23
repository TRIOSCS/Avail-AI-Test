---
name: mapping-user-journeys
description: |
  Maps in-app journeys and identifies friction points in code for the AvailAI sourcing platform.
  Use when: tracing how users move through search → RFQ → response workflows, identifying dead-ends in HTMX partials, auditing empty states, finding where users drop off, or reviewing route-to-template chains for gaps.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_close, mcp__playwright__browser_resize, mcp__playwright__browser_console_messages, mcp__playwright__browser_handle_dialog, mcp__playwright__browser_evaluate, mcp__playwright__browser_file_upload, mcp__playwright__browser_fill_form, mcp__playwright__browser_install, mcp__playwright__browser_press_key, mcp__playwright__browser_type, mcp__playwright__browser_navigate, mcp__playwright__browser_navigate_back, mcp__playwright__browser_network_requests, mcp__playwright__browser_run_code, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_snapshot, mcp__playwright__browser_click, mcp__playwright__browser_drag, mcp__playwright__browser_hover, mcp__playwright__browser_select_option, mcp__playwright__browser_tabs, mcp__playwright__browser_wait_for
---

# Mapping User Journeys

AvailAI's UX is driven entirely by HTMX partial swaps — there is no client-side router. Every user journey is a chain of `hx-get` → FastAPI route → Jinja2 template → `#main-content` swap. Friction appears as dead-end partials (no empty state), missing loading indicators, broken swap targets, or routes that return 200 with no actionable content.

## Quick Start

### Trace a Journey from Link to Template

```bash
# 1. Find where an HTMX link points
grep -r 'hx-get="/v2/requisitions' app/templates/ --include="*.html" -l

# 2. Find the FastAPI route handler
grep -r 'def.*requisition' app/routers/ --include="*.py" -n

# 3. Find which template it renders
grep -r 'template_response\|TemplateResponse' app/routers/htmx_views.py | grep requisition
```

### Audit Dead-End Partials with Playwright

```javascript
// tests/e2e/dead-ends/requisition-empty.spec.ts
test('empty requisition list shows CTA', async ({ page }) => {
  await page.goto('/v2/requisitions');
  await page.waitForSelector('#main-content');
  const empty = page.locator('[data-testid="empty-state"]');
  await expect(empty).toBeVisible();
  await expect(empty.locator('a, button')).toHaveCount({ minimum: 1 });
});
```

## Key Concepts

| Concept | Location | Example |
|---------|----------|---------|
| Page shell | `app/templates/base.html` | `#main-content` swap target |
| Lazy loader | `app/templates/htmx/base_page.html` | spinner → `hx-get` partial |
| Route registry | `app/routers/htmx_views.py` | `@router.get("/v2/*")` |
| Empty states | `app/templates/htmx/partials/*/` | `{% if not items %}` blocks |
| MVP gate | `app/config.py` → `mvp_mode` | hides Dashboard, Enrichment |

## Journey Audit Workflow

```
Copy this checklist for each journey being audited:
- [ ] Identify entry point (link/button with hx-get)
- [ ] Trace to FastAPI route handler
- [ ] Confirm route renders correct partial (not wrong template)
- [ ] Check partial has empty state ({% if not items %} with CTA)
- [ ] Check partial has error state (hx-swap-oob or response-targets)
- [ ] Verify loading indicator (htmx-ext-loading-states or hx-indicator)
- [ ] Run Playwright dead-ends project to confirm
```

## Common Patterns

### Finding a Broken Swap Target

**When:** A partial loads but content appears in the wrong place.

```bash
# Find all hx-target overrides in templates
grep -r 'hx-target' app/templates/ --include="*.html" | grep -v '#main-content'
```

### Checking MVP-Gated Routes

```python
# app/routers/htmx_views.py — gate pattern
from app.config import settings

@router.get("/v2/dashboard")
async def dashboard(user=Depends(require_user)):
    if settings.mvp_mode:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("htmx/dashboard.html", {...})
```

## See Also

- [activation-onboarding](references/activation-onboarding.md)
- [engagement-adoption](references/engagement-adoption.md)
- [in-app-guidance](references/in-app-guidance.md)
- [product-analytics](references/product-analytics.md)
- [roadmap-experiments](references/roadmap-experiments.md)
- [feedback-insights](references/feedback-insights.md)

## Related Skills

- See the **htmx** skill for partial swap patterns and HTMX extensions
- See the **jinja2** skill for template inheritance and empty-state rendering
- See the **fastapi** skill for route handler conventions
- See the **playwright** skill for E2E dead-end and workflow test projects
- See the **designing-onboarding-paths** skill for first-run flow patterns
- See the **frontend-design** skill for loading states and visual feedback patterns
