# Activation & Onboarding Reference

## Contents
- First-Run Detection
- Empty State Patterns
- Onboarding Checklist Pattern
- Anti-Patterns
- Playwright Verification

---

## First-Run Detection

AvailAI has no dedicated onboarding model. First-run state is inferred from data absence. The correct pattern is to query for existence in the route handler and pass a flag to the template.

```python
# app/routers/htmx_views.py
@router.get("/v2/requisitions")
async def requisitions_page(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    has_requisitions = db.query(Requisition).filter_by(created_by=user.id).first() is not None
    return templates.TemplateResponse("htmx/partials/requisitions/list.html", {
        "request": request,
        "user": user,
        "is_first_run": not has_requisitions,
    })
```

```html
<!-- app/templates/htmx/partials/requisitions/list.html -->
{% if is_first_run %}
  <div class="empty-state" data-testid="empty-state">
    <h2>Start your first search</h2>
    <p>Enter part numbers to find vendors across 10 supplier sources.</p>
    <a href="/v2/requisitions/new" class="btn-primary">Create Requisition</a>
  </div>
{% else %}
  <!-- normal list -->
{% endif %}
```

**Why this matters:** Without a CTA in the empty state, new users see a blank screen and have no activation path. This is the most common dead-end in HTMX apps.

---

## Empty State Patterns

Every list partial MUST handle three states: loading, empty, and populated.

```html
<!-- DO: all three states covered -->
<div id="requisitions-list" hx-indicator="#list-spinner">
  <div id="list-spinner" class="htmx-indicator">Loading...</div>

  {% if not requisitions %}
    <div class="empty-state" data-testid="empty-state">
      <p>No requisitions yet.</p>
      <a hx-get="/v2/requisitions/new" hx-target="#main-content">New Requisition</a>
    </div>
  {% else %}
    {% for req in requisitions %}
      <!-- item row -->
    {% endfor %}
  {% endif %}
</div>
```

```html
<!-- DON'T: silent empty — users see nothing -->
{% for req in requisitions %}
  <!-- item row -->
{% endfor %}
```

**Why the DON'T breaks:** HTMX swaps the partial into `#main-content`. If the for-loop produces zero HTML, the swap replaces the previous content with nothing. Users assume the page is broken.

---

## Onboarding Checklist Pattern

For progressive onboarding (e.g., "complete your vendor profile"), track completion steps in `system_config` or a dedicated model — NEVER in a cookie or localStorage.

```python
# app/services/onboarding_service.py
from app.models.system_config import SystemConfig

def get_onboarding_status(db: Session, user_id: int) -> dict[str, bool]:
    return {
        "has_requisition": db.query(Requisition).filter_by(created_by=user_id).count() > 0,
        "has_vendor": db.query(Vendor).filter_by(created_by=user_id).count() > 0,
        "has_rfq_sent": db.query(Requisition).filter(
            Requisition.created_by == user_id,
            Requisition.status == RequisitionStatus.SENT
        ).count() > 0,
    }
```

See the **designing-onboarding-paths** skill for full checklist UI patterns.

---

## Anti-Patterns

### WARNING: Empty State Behind MVP Gate

**The Problem:**
```python
# BAD — MVP gate hides the page but leaves no fallback
if settings.mvp_mode:
    raise HTTPException(status_code=404)
```

**Why This Breaks:** Users navigating to a gated route get a 404, not an explanation. In HTMX, this replaces `#main-content` with an error partial (if one exists) or nothing.

**The Fix:**
```python
# GOOD — redirect to a safe landing with a message
if settings.mvp_mode:
    return templates.TemplateResponse("htmx/partials/mvp_gate.html", {
        "request": request,
        "feature": "Dashboard",
    })
```

---

## Playwright Verification

```javascript
// tests/e2e/dead-ends/empty-states.spec.ts
test('all list pages have empty states with CTAs', async ({ page }) => {
  const routes = ['/v2/requisitions', '/v2/vendors', '/v2/companies'];
  for (const route of routes) {
    await page.goto(route);
    await page.waitForSelector('#main-content');
    // If empty state is shown, it must have an action
    const empty = page.locator('[data-testid="empty-state"]');
    if (await empty.isVisible()) {
      await expect(empty.locator('a[hx-get], button[hx-get], a[href]')).toHaveCount({ minimum: 1 });
    }
  }
});
```

Run with: `npx playwright test --project=dead-ends`
