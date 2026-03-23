# Activation & Onboarding Reference

## Contents
- First-Run Detection
- Dashboard Welcome Pattern
- Empty State CTAs
- DO / DON'T Pairs
- Anti-Patterns

---

## First-Run Detection

AvailAI has no dedicated onboarding wizard. "First run" is inferred from data absence — no
requisitions, no vendors searched, no contacts synced. Detect it server-side and pass a flag
to the template.

```python
# app/routers/htmx_views.py
@router.get("/v2/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    req_count = db.query(func.count(Requisition.id)).scalar() or 0
    vendor_count = db.query(func.count(Vendor.id)).scalar() or 0
    is_first_run = req_count == 0 and vendor_count == 0

    return templates.TemplateResponse("htmx/partials/dashboard.html", {
        "request": request,
        "user": user,
        "is_first_run": is_first_run,
        "req_count": req_count,
    })
```

```html
<!-- app/templates/htmx/partials/dashboard.html -->
{% if is_first_run %}
  <div class="bg-brand-50 border border-brand-200 rounded-lg p-6 mb-6">
    <h2 class="text-lg font-semibold text-brand-800">Welcome to AvailAI</h2>
    <p class="text-sm text-brand-600 mt-1">Start by creating your first requisition or uploading a vendor stock list.</p>
    <div class="mt-4 flex gap-3">
      <a href="/v2/requisitions/new" class="btn-primary text-sm">Create Requisition</a>
      <a href="/v2/vendors" class="btn-secondary text-sm">Browse Vendors</a>
    </div>
  </div>
{% endif %}
```

## Dashboard Welcome Pattern

The existing `dashboard.html` already renders a stat-card welcome. Build on it — don't replace it.
Add quick-action cards only when counts are zero.

```html
<!-- Extend existing dashboard.html quick actions -->
{% if open_req_count == 0 %}
<div class="col-span-full">
  {% with message="No open requisitions. Create one to start sourcing.",
          action_url="/v2/requisitions/new",
          action_label="New Requisition" %}
    {% include "htmx/partials/shared/empty_state.html" %}
  {% endwith %}
</div>
{% endif %}
```

## Empty State CTAs

`app/templates/htmx/partials/shared/empty_state.html` is the canonical empty state. Always use it.

**Variables it accepts:**
- `message` — descriptive text
- `action_url` — href for the CTA button (optional)
- `action_label` — button text (optional)

```html
<!-- Correct usage in a list partial -->
{% if not items %}
  {% with message="No sightings yet. Run a search to find vendor stock.",
          action_url="/v2/search",
          action_label="Search Parts" %}
    {% include "htmx/partials/shared/empty_state.html" %}
  {% endwith %}
{% endif %}
```

## DO / DON'T Pairs

**DO: Detect first-run server-side**
```python
# GOOD — single DB query, no client-side guessing
is_first_run = db.query(func.count(Requisition.id)).scalar() == 0
```

**DON'T: Use Alpine.js to decide if a user is new**
```javascript
// BAD — Alpine has no access to DB state; this will be wrong after page refresh
x-data="{ isNew: localStorage.getItem('has_visited') === null }"
```
Why: localStorage resets per device/browser; it doesn't reflect actual account activity.

**DO: Reuse `empty_state.html` with context**
```html
{% include "htmx/partials/shared/empty_state.html" %}
```

**DON'T: Inline custom empty states per page**
```html
<!-- BAD — duplicates styling, breaks visual consistency -->
<div class="text-center py-12 text-gray-400">Nothing here yet.</div>
```
Why: Inconsistent look, no CTA, no path forward for the user.

## Anti-Patterns

### WARNING: Showing Onboarding to Returning Users

**The Problem:** Checking `user.created_at > (now - 7 days)` to decide who is "new" shows
onboarding to returning users who just haven't done the target action yet.

**The Fix:** Gate on data absence, not account age. A user with 0 requisitions needs activation
guidance regardless of when they signed up.

### WARNING: Hard-Coding First-Run Logic in Multiple Routes

Every page that could be a user's first stop (dashboard, requisitions list, search) should share
a common `is_first_run` dependency — not duplicate the query.

```python
# app/dependencies.py — shared first-run check
def get_first_run_context(db: Session = Depends(get_db)) -> dict:
    return {
        "is_first_run": db.query(func.count(Requisition.id)).scalar() == 0
    }
```

See the **designing-onboarding-paths** skill for full checklist and flow patterns.
See the **htmx** skill for lazy-loading welcome panels.
