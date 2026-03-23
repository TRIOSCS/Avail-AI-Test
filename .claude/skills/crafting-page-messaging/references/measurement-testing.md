# Measurement and Testing Reference

## Contents
- What to Measure in AvailAI
- Activation Events
- Copy A/B Testing Approach
- Instrumentation Patterns
- Anti-Patterns

There is no analytics SDK (Segment, Amplitude, Mixpanel) in the current dependencies. Measurement is done through server-side logging (Loguru) and database event records. Copy testing is manual: deploy a variant, observe completion rates in the database.

## What to Measure in AvailAI

| Metric | What it signals | Where to query |
|--------|----------------|----------------|
| Requisitions created per user | Activation | `requisitions` table, `created_by` |
| RFQs sent per requisition | Workflow depth | `rfq_sends` table |
| Offers auto-parsed vs. manual | AI trust | `offers.confidence_score` |
| Proactive matches acted on | Feature adoption | `proactive_matches.status` |
| Empty-state views with no follow-on | Copy failure | Loguru request logs |

## Activation Events

The core activation sequence for a new buyer:

```
1. Login                          → session created
2. First Requisition created      → activation
3. First Search run               → sourcing engine adopted
4. First RFQ sent                 → full workflow activated
5. First Reply parsed             → AI value realized
```

Logging these events enables cohort analysis even without an analytics platform.

```python
# app/routers/requisitions/ — log activation events
from loguru import logger

@router.post("/requisitions")
async def create_requisition(data: RequisitionCreate, user=Depends(require_buyer), db=Depends(get_db)):
    req = RequisitionService.create(db, data, user.id)
    logger.info("requisition_created", extra={
        "user_id": user.id,
        "requisition_id": req.id,
        "event": "activation",
    })
    return req
```

## Copy A/B Testing Approach

Without a client-side analytics SDK, A/B testing is done via feature flags in `app/config.py`.

```python
# app/config.py — copy variant flag
class Settings(BaseSettings):
    copy_variant: str = "control"  # "control" | "variant_a"
```

```html
{# app/templates/htmx/login.html — variant copy #}
{% if config.copy_variant == "variant_a" %}
  <h1 class="text-2xl font-bold text-white">
    Find parts. Close deals. Zero spreadsheets.
  </h1>
{% else %}
  <h1 class="text-2xl font-bold text-white">
    Source parts. Close quotes. Skip the spreadsheets.
  </h1>
{% endif %}
```

**Measure the variant** by comparing sign-in completion rates and first-requisition creation rates between user cohorts exposed to each variant (filter by `created_at` window matching deployment date).

## Instrumentation Patterns

### Log Empty-State Impressions

```python
# app/routers/htmx_views.py — log when user hits an empty list
@router.get("/v2/partials/requisitions")
async def requisitions_partial(db=Depends(get_db), user=Depends(require_user)):
    reqs = RequisitionService.list(db, user.id)
    if not reqs:
        logger.info("empty_state_impression", extra={
            "user_id": user.id,
            "surface": "requisitions_list",
        })
    return template_response("htmx/partials/requisitions/list.html", {"requisitions": reqs})
```

### Track CTA Clicks via HTMX Headers

```html
{# Pass a tracking hint via hx-headers — read in the route handler #}
<button hx-get="/v2/partials/requisitions/create-form"
        hx-headers='{"X-CTA-Source": "empty_state_requisitions"}'
        hx-target="#modal-content"
        @click="$dispatch('open-modal')">
  Create Requisition
</button>
```

```python
# app/routers/requisitions/ — read tracking header
@router.get("/v2/partials/requisitions/create-form")
async def create_form(request: Request, user=Depends(require_user)):
    cta_source = request.headers.get("X-CTA-Source", "unknown")
    logger.info("cta_click", extra={"user_id": user.id, "source": cta_source})
    return template_response("htmx/partials/requisitions/create_form.html", {})
```

## WARNING: Logging PII in Event Payloads

**The Problem:**

```python
# BAD — logs email address in event payload
logger.info("cta_click", extra={"user_email": user.email, "source": cta_source})
```

**Why This Breaks:** Loguru writes to `logs/` files on disk. Logging PII (email, name) in event payloads means PII ends up in log files that may be retained indefinitely or shipped to external log aggregators.

**The Fix:** Log only `user_id` (an opaque integer). Join to user records in the database for analysis.

## Related Skills

- See the **orchestrating-feature-adoption** skill for activation event tracking patterns
- See the **mapping-user-journeys** skill for identifying dead-end HTMX partials
- See the **fastapi** skill for route instrumentation and dependency injection
