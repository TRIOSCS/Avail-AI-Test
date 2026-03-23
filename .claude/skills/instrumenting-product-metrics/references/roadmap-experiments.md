# Roadmap & Experiments Reference

## Contents
- Feature flag system
- MVP mode gating
- Adding a new feature flag
- Rolling out behind a flag
- Scheduler-gated jobs
- Anti-patterns

---

## Feature Flag System

All feature flags are Pydantic `Settings` fields in `app/config.py`. They are read from environment variables at startup. There is no runtime flag store — flags are deployment-time decisions.

| Flag | Default | Gates |
|------|---------|-------|
| `activity_tracking_enabled` | `True` | All `activity_log` writes |
| `email_mining_enabled` | `False` | Inbox scan / Claude parsing jobs |
| `proactive_matching_enabled` | `True` | Vendor→customer offer matching |
| `customer_enrichment_enabled` | `True` | Customer enrichment pipeline |
| `material_enrichment_enabled` | `False` | Material card enrichment |
| `on_demand_enrichment_enabled` | `True` | Per-request enrichment endpoints |
| `prospecting_enabled` | `True` | Prospecting job and routes |
| `ownership_sweep_enabled` | `False` | Ownership assignment background sweep |
| `mvp_mode` | `True` | Dashboard, Teams, Task Manager, Enrichment UI |

---

## MVP Mode Gating

`mvp_mode=True` hides entire feature areas from the UI and disables their routes. Gate new features the same way:

```python
# app/config.py
class Settings(BaseSettings):
    my_new_feature_enabled: bool = False
```

```python
# app/routers/my_feature.py
from app.config import get_settings

@router.get("/api/my-feature/data")
async def get_data(settings=Depends(get_settings)):
    if not settings.my_new_feature_enabled:
        raise HTTPException(status_code=404)
    ...
```

```html
<!-- Template gating -->
{% if not settings.mvp_mode %}
<a hx-get="/v2/my-feature">My Feature</a>
{% endif %}
```

Pass `settings` into template context from the router — do not call `get_settings()` in templates.

---

## Adding a New Feature Flag

Copy this checklist for every new flag:

```
- [ ] Add field to Settings in app/config.py with safe default (usually False)
- [ ] Add to .env.example with comment explaining what it enables
- [ ] Gate the service function: if settings.my_flag_enabled: ...
- [ ] Gate the router: raise 404 or return empty if disabled
- [ ] Gate the template nav link: {% if not settings.mvp_mode %}
- [ ] Add to pytest fixture: monkeypatch.setattr(settings, "my_flag_enabled", True)
- [ ] Write one test with flag=True and one with flag=False
```

---

## Scheduler-Gated Jobs

Jobs in `app/jobs/` should respect their feature flag at job execution time, not at registration time. This allows flags to be toggled without restarting the scheduler.

```python
# app/jobs/my_job.py
from app.config import get_settings
from loguru import logger

async def _run_my_job() -> None:
    settings = get_settings()
    if not settings.my_new_feature_enabled:
        logger.debug("my_job skipped: feature disabled")
        return
    # ... job logic ...
```

---

## Gradual Rollout Pattern

AvailAI has no percentage-based rollout infrastructure. Use a `system_config` key to target specific user IDs for early access.

```python
# app/services/rollout_service.py
from app.models.config import SystemConfig
from sqlalchemy.orm import Session
import json

def is_in_early_access(db: Session, user_id: int, feature: str) -> bool:
    row = db.get(SystemConfig, f"early_access_{feature}")
    if not row:
        return False
    allowed_ids: list[int] = json.loads(row.value)
    return user_id in allowed_ids
```

---

## Anti-Patterns

**NEVER use a database table as a feature flag store** unless you need runtime toggling without redeploy. The current config-based approach is correct for AvailAI's deployment cadence.

**NEVER check feature flags in templates directly.** Pass a boolean from the router. Templates should not import `get_settings()`.

**NEVER gate metrics collection behind an experiment flag.** Always collect data even when the UI is hidden — you need the baseline.

---

## Related Skills

- See the **fastapi** skill for dependency injection of settings
- See the **pytest** skill for monkeypatching feature flags in tests
- See the **orchestrating-feature-adoption** skill for rollout communication patterns
