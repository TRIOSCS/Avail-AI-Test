# Measurement and Testing Reference

## Contents
- Activation Events to Track
- Copy A/B Testing Approach
- Analytics Hooks in Templates
- Anti-Patterns

AvailAI has no third-party analytics SDK (no GA4, Segment, or Mixpanel) in its current dependencies. Behavioral measurement is done through **structured Loguru logging** and **database event records** (activity tracking via `ACTIVITY_TRACKING_ENABLED`).

## Activation Events to Track

These are the events that signal a user has experienced the core value:

| Event | Signal | Where it fires |
|-------|--------|---------------|
| First requisition created | User understands the workflow | `app/routers/requisitions/` |
| First search run | User tried the sourcing engine | `app/search_service.py` |
| First RFQ sent | User engaged the RFQ workflow | `app/email_service.py` |
| First offer auto-parsed | User saw AI value | `app/services/response_parser.py` |
| First proactive match viewed | User explored vendor intelligence | `app/routers/proactive.py` |

## Logging Activation Events

Use Loguru with structured fields. These logs can be queried from Docker log output or piped to a log aggregator.

```python
# In app/routers/requisitions/__init__.py — after requisition creation
from loguru import logger

logger.info(
    "activation_event",
    extra={
        "event": "requisition_created",
        "user_id": current_user.id,
        "is_first": is_first_requisition,
        "req_id": new_req.id,
    }
)
```

## Activity Tracking Integration

When `ACTIVITY_TRACKING_ENABLED=true`, user actions are stored in the database. Use this for in-app "last active" signals and retention analysis.

```python
# app/services/activity_service.py pattern — log a key action
from app.services.activity_service import record_activity

await record_activity(
    db=db,
    user_id=user.id,
    action="search_run",
    entity_type="requisition",
    entity_id=req.id,
    metadata={"source_count": 10, "result_count": result_count},
)
```

## Copy A/B Testing Approach

No A/B testing framework is installed. Use **feature flags in `app/config.py`** to gate copy variants, then compare activation rates in Loguru logs.

```python
# app/config.py — add a copy variant flag
class Settings(BaseSettings):
    login_copy_variant: str = "v1"  # "v1" | "v2"
```

```html
{# app/templates/htmx/login.html — render copy variant #}
{% if settings.login_copy_variant == "v2" %}
  <p class="text-sm text-gray-500">
    Stop chasing quotes. Find parts across 10 suppliers in seconds.
  </p>
{% else %}
  <p class="text-sm text-gray-500">
    Search 10 supplier networks in parallel. Auto-parse RFQ replies with AI.
  </p>
{% endif %}
```

## WARNING: No Analytics SDK

### The Problem

There is no client-side analytics library in `package.json` (no GA4, Segment, PostHog, etc.). Copy optimization decisions currently rely on developer intuition, not data.

**Impact:** You cannot measure click-through rates on CTAs, time-to-first-action, or drop-off points in onboarding flows.

### Recommended Solution

Install PostHog (self-hosted or cloud) for product analytics:

```bash
npm install posthog-js
```

```javascript
// app/static/htmx_app.js — initialize PostHog
import posthog from 'posthog-js'
posthog.init('YOUR_KEY', { api_host: 'https://app.posthog.com' })

// Track HTMX navigation events
document.addEventListener('htmx:afterSwap', (e) => {
  posthog.capture('page_view', { path: window.location.pathname })
})
```

Until analytics is added, rely on Loguru structured logs for activation event proxies.

## Related Skills

- See the **orchestrating-feature-adoption** skill for feature flag patterns
- See the **fastapi** skill for middleware-level event capture
- See the **playwright** skill for behavioral E2E test coverage as a proxy for UX quality
