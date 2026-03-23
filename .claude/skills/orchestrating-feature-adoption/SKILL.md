---
name: orchestrating-feature-adoption
description: |
  Plans feature discovery, nudges, and adoption flows for AvailAI's sourcing platform.
  Use when: adding feature discovery banners, building adoption nudges, designing empty-state CTAs,
  planning feature rollouts behind flags, wiring in-app guidance to new workflows, or tracking
  activation events for requisitions, search, RFQ, and proactive matching.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_close, mcp__playwright__browser_resize, mcp__playwright__browser_console_messages, mcp__playwright__browser_handle_dialog, mcp__playwright__browser_evaluate, mcp__playwright__browser_file_upload, mcp__playwright__browser_fill_form, mcp__playwright__browser_install, mcp__playwright__browser_press_key, mcp__playwright__browser_type, mcp__playwright__browser_navigate, mcp__playwright__browser_navigate_back, mcp__playwright__browser_network_requests, mcp__playwright__browser_run_code, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_snapshot, mcp__playwright__browser_click, mcp__playwright__browser_drag, mcp__playwright__browser_hover, mcp__playwright__browser_select_option, mcp__playwright__browser_tabs, mcp__playwright__browser_wait_for
---

# Orchestrating Feature Adoption

AvailAI's adoption surfaces run through three mechanisms: **empty states** (zero-data CTAs),
**toast nudges** (Alpine `$store.toast`), and **feature flags** (`config.py` + `SystemConfig`).
There is no dedicated onboarding wizard — adoption logic lives in HTMX partials, Jinja2 templates,
and APScheduler jobs. All new feature surfaces must hook into these existing primitives rather than
adding new libraries.

## Quick Start

### Show a Feature Discovery Toast

```html
<!-- In any Jinja2 template — fire after first RFQ send -->
{% if show_rfq_nudge %}
<script>
  Alpine.store('toast').message = 'Tip: Check Inbox monitors replies automatically every 30 min.';
  Alpine.store('toast').type = 'info';
  Alpine.store('toast').show = true;
</script>
{% endif %}
```

### Gate a Feature Behind a Config Flag

```python
# app/config.py
proactive_matching_enabled: bool = Field(default=True, alias="PROACTIVE_MATCHING_ENABLED")

# app/routers/htmx_views.py — guard the route
@router.get("/v2/proactive")
async def proactive_page(request: Request, user: User = Depends(require_user)):
    if not settings.proactive_matching_enabled:
        return RedirectResponse("/v2/requisitions")
    ...
```

### Empty State with Adoption CTA

```python
# Passing empty-state context from router
return templates.TemplateResponse("htmx/partials/search/form.html", {
    "request": request,
    "results": [],
    "empty_message": "No vendors found. Try a broader MPN.",
    "empty_action_url": "/v2/vendors/new",
    "empty_action_label": "Add Vendor Manually",
})
```

## Key Concepts

| Concept | Mechanism | Location |
|---------|-----------|----------|
| Feature flags | `config.py` booleans + `.env` | `app/config.py` |
| Runtime config | `SystemConfig` key-value table | `app/models/config.py` |
| Toast nudges | Alpine `$store.toast` | `app/static/htmx_app.js` |
| Empty state CTAs | `shared/empty_state.html` partial | `app/templates/htmx/partials/shared/` |
| Modal dialogs | `@open-modal.window` custom event | `app/templates/htmx/partials/shared/modal.html` |
| Activity tracking | `ActivityLog` model + jobs | `app/models/intelligence.py:257` |

## Common Patterns

### Lazy-Loaded Feature Section

**When:** Rolling out a new dashboard widget to test engagement before full build.

```html
<!-- Defers load until element is visible in viewport -->
<div
  hx-get="/v2/partials/new-feature-widget"
  hx-trigger="intersect once"
  hx-swap="outerHTML"
  class="animate-pulse bg-gray-100 rounded-lg h-32"
>
  <span class="sr-only">Loading...</span>
</div>
```

### Runtime Toggle via SystemConfig

**When:** You need ops to flip a feature without a redeploy.

```python
# app/services/config_service.py
def is_feature_enabled(db: Session, key: str, default: bool = False) -> bool:
    row = db.query(SystemConfig).filter_by(key=key).first()
    if row is None:
        return default
    return row.value.lower() in ("true", "1", "yes")
```

## See Also

- [activation-onboarding](references/activation-onboarding.md)
- [engagement-adoption](references/engagement-adoption.md)
- [in-app-guidance](references/in-app-guidance.md)
- [product-analytics](references/product-analytics.md)
- [roadmap-experiments](references/roadmap-experiments.md)
- [feedback-insights](references/feedback-insights.md)

## Related Skills

- See the **designing-onboarding-paths** skill for first-run flows, empty states, and onboarding checklists
- See the **htmx** skill for HTMX partial patterns, lazy loading, and swap targets
- See the **frontend-design** skill for Tailwind styling, Alpine.js state, and component patterns
- See the **jinja2** skill for template inheritance, macros, and context passing
- See the **fastapi** skill for route guards, dependencies, and feature-flag middleware
- See the **redis** skill for caching adoption state and suppressing repeat nudges
- See the **playwright** skill for E2E verification of adoption flows
