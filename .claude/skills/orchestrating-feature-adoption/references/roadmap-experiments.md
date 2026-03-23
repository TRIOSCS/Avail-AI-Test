# Roadmap & Experiments Reference

## Contents
- Feature Flag System
- MVP Mode Gate
- Soft Rollout Pattern
- Runtime SystemConfig Toggles
- Rollout Checklist
- DO / DON'T Pairs

---

## Feature Flag System

All feature flags are Pydantic fields in `app/config.py`, read from environment variables at
startup. They are immutable at runtime unless paired with a `SystemConfig` toggle.

```python
# app/config.py — current feature flags
class Settings(BaseSettings):
    mvp_mode: bool = Field(default=True, alias="MVP_MODE")
    activity_tracking_enabled: bool = Field(default=True, alias="ACTIVITY_TRACKING_ENABLED")
    email_mining_enabled: bool = Field(default=False, alias="EMAIL_MINING_ENABLED")
    proactive_matching_enabled: bool = Field(default=True, alias="PROACTIVE_MATCHING_ENABLED")
    customer_enrichment_enabled: bool = Field(default=True, alias="CUSTOMER_ENRICHMENT_ENABLED")
    material_enrichment_enabled: bool = Field(default=False, alias="MATERIAL_ENRICHMENT_ENABLED")
    contacts_sync_enabled: bool = Field(default=True, alias="CONTACTS_SYNC_ENABLED")
    on_demand_enrichment_enabled: bool = Field(default=True, alias="ON_DEMAND_ENRICHMENT_ENABLED")
```

**Adding a new flag:**

```python
# 1. Add to app/config.py
new_feature_enabled: bool = Field(default=False, alias="NEW_FEATURE_ENABLED")

# 2. Gate the route or service
@router.get("/v2/new-feature")
async def new_feature_page(request: Request, user: User = Depends(require_user)):
    if not settings.new_feature_enabled:
        raise HTTPException(status_code=404)
    ...
```

## MVP Mode Gate

`MVP_MODE=true` is meant to strip non-core features for a focused demo or initial deployment.
Currently `mvp_mode` is defined in `config.py` but **routers are not conditionally mounted** —
the gate must be enforced in individual route handlers or templates.

```python
# app/routers/htmx_views.py — hide MVP-gated nav items in context
@router.get("/v2/dashboard")
async def dashboard(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse("htmx/partials/dashboard.html", {
        "request": request,
        "mvp_mode": settings.mvp_mode,
    })
```

```html
<!-- Hide enrichment and teams nav in MVP mode -->
{% if not mvp_mode %}
  <a href="/v2/enrichment" hx-get="/v2/partials/enrichment" hx-target="#main-content">Enrichment</a>
{% endif %}
```

## Soft Rollout Pattern

For features you want to test with partial traffic (e.g., new search ranking, proactive UI),
gate on a `SystemConfig` key set per deployment — not a `.env` change that requires restart.

```python
# Soft rollout: enable for a percentage of requests via SystemConfig
import random

def is_in_rollout(db: Session, feature_key: str, rollout_pct: int = 50) -> bool:
    """Returns True for ~rollout_pct% of calls. Not user-sticky — use with care."""
    row = db.query(SystemConfig).filter_by(key=feature_key).first()
    if row is None:
        return False
    if row.value == "100":
        return True
    if row.value == "0":
        return False
    return random.randint(1, 100) <= int(row.value)
```

**Set via the admin settings tab or direct DB update:**

```sql
INSERT INTO system_config (key, value, description)
VALUES ('new_search_ui_rollout_pct', '50', 'New search UI rollout percentage (0-100)')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

## Runtime SystemConfig Toggles

For ops-controlled toggles that don't require a restart, seed defaults in `startup.py` and
read them via `SystemConfig`.

```python
# app/startup.py — seed a new feature toggle
SYSTEM_CONFIG_DEFAULTS = [
    ...
    ("proactive_auto_send_enabled", "false", "Auto-send proactive matches without review"),
]
```

```python
# app/services/config_service.py
def get_bool_config(db: Session, key: str, default: bool = False) -> bool:
    row = db.query(SystemConfig).filter_by(key=key).first()
    if row is None:
        return default
    return row.value.lower() in ("true", "1", "yes")
```

## Rollout Checklist

Copy this checklist when shipping a new feature behind a flag:

- [ ] Add flag to `app/config.py` with `default=False`
- [ ] Add seed entry to `SYSTEM_CONFIG_DEFAULTS` in `startup.py` (if runtime-toggleable)
- [ ] Gate route handler: `if not settings.new_feature_enabled: raise HTTPException(404)`
- [ ] Gate nav/UI: `{% if not mvp_mode %}` or `{% if feature_flag %}`
- [ ] Add migration if new DB columns are needed (see **sqlalchemy** skill)
- [ ] Enable in `.env.example` with a comment explaining what it does
- [ ] Write a Playwright test that verifies the feature is hidden when flag is off
- [ ] Enable in staging, verify, then set `default=True` in the next release

## DO / DON'T Pairs

**DO: Default new flags to `False` until the feature is stable**
```python
new_feature_enabled: bool = Field(default=False, alias="NEW_FEATURE_ENABLED")
```

**DON'T: Default new flags to `True` and rely on operators to turn them off**
Operators don't read changelogs. If the feature is broken in production, a `default=True`
flag guarantees impact.

**DO: Check flag in the service layer, not only in templates**
```python
# GOOD — prevents direct API access bypassing the UI gate
if not settings.proactive_matching_enabled:
    raise HTTPException(status_code=404, detail="Feature not enabled")
```

**DON'T: Gate only in the Jinja2 template**
A hidden nav link doesn't protect the API route — any HTTP client can call it directly.

See the **fastapi** skill for dependency-based feature guards.
See the **pytest** skill for testing flag-off behavior.
