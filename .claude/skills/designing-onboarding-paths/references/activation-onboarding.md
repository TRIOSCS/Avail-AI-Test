# Activation & Onboarding Reference

## Contents
- Empty State Component
- First-Login Flow
- Split-Panel Entry Point
- Seed Data Pattern
- Anti-Patterns

## Empty State Component

`app/templates/htmx/partials/shared/empty_state.html` is the single reusable empty state. Always use it — never inline ad-hoc empty messages.

```jinja2
{# Caller sets these before including #}
{% set message = "No vendors found. Add one to start tracking." %}
{% set action_url = "/v2/partials/vendors/new" %}
{% set action_label = "Add Vendor" %}
{% include "htmx/partials/shared/empty_state.html" %}
```

The component renders a centered card with a gray icon, the message, and an optional button. If `action_url` is omitted, no button renders — use this for read-only contexts where creation isn't available.

## First-Login Flow

New users are created automatically on first Azure AD callback in `app/routers/auth.py`. No dedicated onboarding wizard exists — the dashboard IS the first-run screen.

```python
# app/routers/auth.py — first-login detection pattern
user = db.query(User).filter(User.azure_id == azure_id).first()
if not user:
    user = User(azure_id=azure_id, email=email, name=name, role=UserRole.VIEWER)
    db.add(user)
    db.commit()
    logger.info("New user created on first login", extra={"email": email})
    # Trigger first-time data backfill here if needed
```

After creation, the user lands on `/v2/` which loads `app/templates/base.html` → dashboard partial. Dashboard stat cards show zeros, which IS the first-run empty state.

## Split-Panel Entry Point

The requisitions workspace uses a split panel. When no requisition is selected (or none exist), `_detail_empty.html` fills the right pane:

```jinja2
{# app/templates/requisitions2/_detail_empty.html #}
<div class="flex items-center justify-center h-full text-gray-400">
  <div class="text-center">
    <p class="text-lg">Select a requisition to view details</p>
    <p class="text-sm mt-1">or create a new one to get started</p>
  </div>
</div>
```

For a true first-run state (zero requisitions), add a CTA:

```jinja2
{% if requisition_count == 0 %}
  <div class="text-center py-16">
    <h3 class="text-gray-500 font-medium">No requisitions yet</h3>
    <button hx-get="/v2/partials/requisitions/new"
            hx-target="#modal-content"
            hx-trigger="click"
            class="mt-4 btn-primary">
      Create your first requisition
    </button>
  </div>
{% endif %}
```

## Seed Data Pattern

`app/startup.py` uses `ON CONFLICT DO NOTHING` for all seed data. This is idempotent — safe on every restart. Use the same pattern for any first-run defaults:

```python
def _seed_system_config(db: Session) -> None:
    seeds = [
        ("inbox_scan_interval_min", "30", "Minutes between inbox scan cycles"),
        ("email_mining_enabled", "false", "Enable email mining background job"),
        ("proactive_matching_enabled", "true", "Enable proactive offer matching"),
    ]
    for key, value, description in seeds:
        db.execute(
            text(
                "INSERT INTO system_config (key, value, description) "
                "VALUES (:k, :v, :d) ON CONFLICT (key) DO NOTHING"
            ),
            {"k": key, "v": value, "d": description},
        )
    db.commit()
```

NEVER use `db.merge()` or `db.add()` for seed data — it will overwrite user-changed config values on restart.

## Anti-Patterns

### WARNING: Inline Empty Messages

**The Problem:**
```jinja2
{# BAD — copy scattered, no CTA, no consistency #}
{% if not vendors %}
  <p class="text-gray-400">Nothing here yet.</p>
{% endif %}
```

**Why This Breaks:** Inconsistent visual treatment, no actionable next step, copy can't be updated in one place.

**The Fix:** Always use the shared `empty_state.html` component with a `message`, `action_url`, and `action_label`.

### WARNING: Onboarding Wizard as Separate Route

AVOID creating a separate `/onboarding` route with a multi-step wizard. AvailAI's architecture routes all UI through HTMX partials. A separate page breaks the nav shell and requires its own auth/layout handling.

**The Fix:** Use the dashboard partial with progressive disclosure. Show "next step" cards based on what data exists.

See the **frontend-design** skill for styling patterns and the **jinja2** skill for include/block patterns.
