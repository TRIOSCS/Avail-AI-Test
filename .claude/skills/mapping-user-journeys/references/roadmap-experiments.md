# Roadmap & Experiments Reference

## Contents
- MVP Mode as Feature Flag
- Adding New Feature Flags
- Gradual Rollout Pattern
- Experiment Tracking
- Anti-Patterns

---

## MVP Mode as Feature Flag

`MVP_MODE` in `app/config.py` is the primary rollout gate. It controls four features: Dashboard, Enrichment, Teams, Task Manager.

```python
# app/config.py
class Settings(BaseSettings):
    mvp_mode: bool = False  # set MVP_MODE=true in .env to restrict
```

```python
# app/routers/htmx_views.py — checking the gate
from app.config import settings

@router.get("/v2/dashboard")
async def dashboard_page(request: Request, user=Depends(require_user)):
    if settings.mvp_mode:
        return templates.TemplateResponse("htmx/partials/feature_unavailable.html", {
            "request": request,
            "feature_name": "Dashboard",
        })
    ...
```

**The gate applies at the route level, not the template level.** Do NOT check `config.mvp_mode` inside Jinja2 templates for route-level gating — it leaks logic into the view layer.

---

## Adding New Feature Flags

For features beyond `MVP_MODE`, add named flags to `Settings` rather than overloading `mvp_mode`.

```python
# app/config.py
class Settings(BaseSettings):
    mvp_mode: bool = False
    email_mining_enabled: bool = True   # existing
    proactive_matching_enabled: bool = True  # new flag
    ai_enrichment_enabled: bool = False  # unreleased feature
```

```python
# app/routers/htmx_views.py
@router.get("/v2/proactive")
async def proactive_page(request: Request, user=Depends(require_user)):
    if not settings.proactive_matching_enabled:
        return templates.TemplateResponse("htmx/partials/feature_unavailable.html", {
            "request": request,
            "feature_name": "Proactive Matching",
        })
    ...
```

Pass the flag to templates only for UI variations (e.g., hiding a button), NOT for security-critical access control. Auth is handled by `require_buyer` / `require_admin` dependencies.

---

## Gradual Rollout Pattern

For user-percentage rollouts (e.g., A/B testing a new search UI), gate by `user.id % 100`:

```python
# app/routers/htmx_views.py
@router.get("/v2/requisitions/new")
async def new_requisition(request: Request, user=Depends(require_user)):
    use_new_ui = (user.id % 100) < 20  # 20% rollout
    template = "htmx/partials/requisitions/new_v2.html" if use_new_ui else \
                "htmx/partials/requisitions/new.html"

    logger.info("user_action", extra={
        "event": "new_requisition_ui_variant",
        "user_id": user.id,
        "variant": "v2" if use_new_ui else "v1",
    })
    return templates.TemplateResponse(template, {"request": request, "user": user})
```

---

## Experiment Tracking

Track variant exposure and outcome events using structured logs (same pattern as product analytics):

```python
# Exposure event — log when user sees the variant
logger.info("experiment_exposure", extra={
    "experiment": "new_search_ui",
    "variant": "v2",
    "user_id": user.id,
})

# Outcome event — log when user completes the funnel step
logger.info("experiment_outcome", extra={
    "experiment": "new_search_ui",
    "event": "search_submitted",
    "user_id": user.id,
})
```

Compare `search_submitted` rate between `variant=v1` and `variant=v2` users to measure impact.

---

## Anti-Patterns

### WARNING: Feature Flags in Alembic Migrations

**The Problem:**
```python
# BAD — migration reads a config flag
def upgrade():
    if settings.new_feature_enabled:
        op.add_column("requisitions", sa.Column("new_field", sa.String))
```

**Why This Breaks:** Migrations must be deterministic and environment-independent. A flag that's `true` in staging but `false` in production causes schema divergence.

**The Fix:** Migrations are always applied. Use feature flags only in application code to decide whether to read/write the new column.

### WARNING: Hardcoding Rollout Percentages

Store rollout percentages in `system_config` or `.env`, not hardcoded in route handlers. A hardcoded `20` requires a code deploy to adjust; an env var does not.

```python
# GOOD
rollout_pct = settings.new_search_ui_rollout_pct  # from .env
use_new_ui = (user.id % 100) < rollout_pct
