# Product Analytics Reference

## Contents
- Logging as Analytics
- Key Funnel Events
- Journey Drop-Off Detection
- Querying Logs
- Anti-Patterns

---

## Logging as Analytics

AvailAI has no dedicated analytics SDK. All product metrics come from Loguru structured logs. Every user action that matters to the funnel MUST emit a structured log event with consistent field names.

```python
# Canonical event log pattern — use this everywhere
logger.info(
    "user_action",
    extra={
        "event": "rfq_sent",           # snake_case event name
        "user_id": user.id,
        "entity_id": requisition.id,   # relevant object ID
        "entity_type": "requisition",
        "request_id": request.state.request_id,
        "meta": {"vendor_count": 3},   # event-specific payload
    }
)
```

**Consistency is critical.** If half your routes log `"event": "rfq_sent"` and half log `"event": "RFQ Sent"`, your funnel queries will be wrong. Define event names as constants.

```python
# app/constants.py — add event name constants
class AnalyticsEvent(StrEnum):
    SEARCH_SUBMITTED = "search_submitted"
    RFQ_SENT = "rfq_sent"
    RESPONSE_PARSED = "response_parsed"
    OFFER_CREATED = "offer_created"
    QUOTE_CREATED = "quote_created"
```

---

## Key Funnel Events

The AvailAI activation funnel has five stages:

| Stage | Event Name | Route |
|-------|-----------|-------|
| 1. Search | `search_submitted` | `POST /api/requirements/search` |
| 2. Results viewed | `search_results_viewed` | `GET /v2/requisitions/{id}/parts` |
| 3. RFQ sent | `rfq_sent` | `POST /v2/requisitions/{id}/send-rfq` |
| 4. Response parsed | `response_parsed` | APScheduler → `inbox_monitor.py` |
| 5. Offer created | `offer_created` | `POST /api/offers` |

Each stage should emit a log event with `user_id` and `entity_id` so you can compute per-user conversion.

---

## Journey Drop-Off Detection

Identify where users stop in the funnel by comparing event counts per user:

```bash
# Count users who reached each funnel stage today
docker compose logs app --since 24h | python3 - <<'EOF'
import sys, json, collections

stages = ["search_submitted", "rfq_sent", "offer_created"]
counts = {s: set() for s in stages}

for line in sys.stdin:
    try:
        record = json.loads(line)
        event = record.get("event")
        uid = record.get("user_id")
        if event in counts and uid:
            counts[event].add(uid)
    except Exception:
        pass

for stage in stages:
    print(f"{stage}: {len(counts[stage])} users")
EOF
```

Drop-off between `search_submitted` and `rfq_sent` typically means the search returned no results (check connector API keys) or the sighting list had no actionable offers.

---

## Querying Logs for Specific Users

```bash
# Trace a specific user's journey
docker compose logs app | grep '"user_id": 42' | \
  python3 -c "
import sys, json
for line in sys.stdin:
    try:
        r = json.loads(line)
        print(r.get('timestamp',''), r.get('event',''), r.get('entity_id',''))
    except: pass
"
```

---

## Anti-Patterns

### WARNING: Logging in Templates Instead of Routes

**The Problem:**
```html
<!-- BAD — no way to log from Jinja2 -->
{# User viewed this page #}
```

Jinja2 templates have no side effects. Activation events MUST be logged in the FastAPI route handler, not in templates.

**The Fix:**
```python
# GOOD — log in the route that renders the template
@router.get("/v2/requisitions/{id}")
async def requisition_detail(id: int, request: Request, user=Depends(require_user)):
    logger.info("user_action", extra={"event": "requisition_viewed", "user_id": user.id, "entity_id": id})
    return templates.TemplateResponse(...)
```

### WARNING: Missing `request_id` on Analytics Events

Without `request_id`, you cannot correlate a user action log with the request that triggered it. Always include `request.state.request_id` in every analytics event. The middleware injects this automatically — use it.

See the **fastapi** skill for request_id middleware setup.
