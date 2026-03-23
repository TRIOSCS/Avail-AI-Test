---
name: designing-onboarding-paths
description: |
  Designs onboarding paths, checklists, and first-run UI for AvailAI.
  Use when: building empty states, first-run flows, onboarding checklists, welcome screens,
  feature discovery nudges, or any UI shown to users with no data or new to a workflow.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Designing Onboarding Paths

AvailAI uses HTMX partials + Jinja2 for all onboarding UI — no SPA, no client routing. Empty states live in templates, first-run logic lives in routers, and feature gating lives in `app/config.py`. New users are created on first Azure AD login; onboarding surfaces are the same partials regular users see, conditionally rendered based on data presence.

## Quick Start

### Reusable Empty State

```jinja2
{# app/templates/htmx/partials/shared/empty_state.html #}
{% if not items %}
  {% set message = "No requisitions yet. Create one to start sourcing." %}
  {% set action_url = "/v2/partials/requisitions/create" %}
  {% set action_label = "Create Requisition" %}
  {% include "htmx/partials/shared/empty_state.html" %}
{% endif %}
```

### Feature-Gated Section

```python
# app/routers/htmx_views.py
from app.config import get_settings

@router.get("/v2/partials/dashboard")
async def dashboard_partial(request: Request, db: Session = Depends(get_db), user=Depends(require_user)):
    settings = get_settings()
    ctx = {
        "mvp_mode": settings.mvp_mode,
        "open_reqs": db.query(func.count(Requisition.id)).scalar() or 0,
    }
    return template_response("htmx/partials/dashboard.html", request, ctx)
```

```jinja2
{# app/templates/htmx/partials/dashboard.html #}
{% if not mvp_mode %}
  <div class="stat-card">...</div>
{% endif %}
```

### First-Run Seed Check

```python
# app/startup.py — idempotent seed pattern
def _seed_system_config(db: Session) -> None:
    seeds = [
        ("inbox_scan_interval_min", "30", "Minutes between inbox scan cycles"),
        ("proactive_matching_enabled", "true", "Enable proactive offer matching"),
    ]
    for key, value, description in seeds:
        db.execute(
            text("INSERT INTO system_config (key, value, description) VALUES (:k, :v, :d) ON CONFLICT (key) DO NOTHING"),
            {"k": key, "v": value, "d": description},
        )
```

## Key Concepts

| Concept | Pattern | Location |
|---------|---------|----------|
| Empty state | `{% include "htmx/partials/shared/empty_state.html" %}` | Any list partial |
| Feature gate | `{% if not mvp_mode %}` | Templates + routers |
| First-run data | `ON CONFLICT DO NOTHING` seeds | `app/startup.py` |
| Progress UI | SSE stream → `search_progress.html` | Sourcing workflow |
| Split-panel empty | `_detail_empty.html` | Requisitions workspace |

## Common Patterns

### Contextual Empty State CTA

When a list is empty, the CTA should create the first item — not navigate away. The empty state partial accepts `action_url` pointing to an HTMX create form swap.

```jinja2
{% set items = requisitions %}
{% set message = "No open requisitions. Start by adding parts to source." %}
{% set action_url = "/v2/partials/requisitions/new" %}
{% set action_label = "New Requisition" %}
{% include "htmx/partials/shared/empty_state.html" %}
```

### Checklist Pattern (Tasks Tab)

```jinja2
{# app/templates/htmx/partials/requisitions/tabs/tasks.html #}
<div x-data="{ filter: 'all' }">
  <button @click="filter = 'todo'" :class="filter === 'todo' && 'active'">To Do</button>
  <button @click="filter = 'done'" :class="filter === 'done' && 'active'">Done</button>
  {% for task in tasks %}
    <div x-show="filter === 'all' || filter === task.status">
      <input type="checkbox" hx-post="/api/tasks/{{ task.id }}/toggle" hx-target="closest div">
      {{ task.title }}
    </div>
  {% endfor %}
</div>
```

## See Also

- [activation-onboarding](references/activation-onboarding.md)
- [engagement-adoption](references/engagement-adoption.md)
- [in-app-guidance](references/in-app-guidance.md)
- [product-analytics](references/product-analytics.md)
- [roadmap-experiments](references/roadmap-experiments.md)
- [feedback-insights](references/feedback-insights.md)

## Related Skills

- See the **frontend-design** skill for styling empty states and welcome screens
- See the **htmx** skill for HTMX swap patterns used in first-run flows
- See the **jinja2** skill for template inheritance and include patterns
- See the **orchestrating-feature-adoption** skill for feature discovery nudges and adoption tracking
- See the **fastapi** skill for route-level onboarding logic and dependency injection
- See the **playwright** skill for E2E testing onboarding flows
