# Roadmap & Experiments Reference

## Contents
- Feature Flag Architecture
- Adding a New Feature Flag
- MVP Mode Gating Pattern
- Gradual Rollout via User Role
- Anti-Patterns

## Feature Flag Architecture

Feature flags in AvailAI are Pydantic settings in `app/config.py` — environment variable backed, not database-backed. This means:
- All instances share the same flag state (set via `.env` or Docker env)
- Flags cannot be toggled per-user without code changes
- Changes require container restart

```python
# app/config.py — existing feature flags
class Settings(BaseSettings):
    mvp_mode: bool = True                    # Hides Dashboard, Enrichment, Teams
    email_mining_enabled: bool = False       # Email inbox mining
    activity_tracking_enabled: bool = True   # CRM activity log
    contacts_sync_enabled: bool = False      # Contacts sync job
    ai_features_enabled: str = "all"         # "all" | "mike_only" | "off"
```

## Adding a New Feature Flag

1. Add to `app/config.py`:

```python
# app/config.py
class Settings(BaseSettings):
    proactive_matching_v2_enabled: bool = False  # New matching algorithm
```

2. Expose in router context:

```python
# app/routers/htmx_views.py
@router.get("/v2/partials/vendors")
async def vendors_partial(request: Request, ...):
    settings = get_settings()
    ctx = {
        "proactive_v2": settings.proactive_matching_v2_enabled,
        ...
    }
    return template_response("htmx/partials/vendors/list.html", request, ctx)
```

3. Gate in template:

```jinja2
{% if proactive_v2 %}
  <div class="badge-new">New</div>
  <button hx-post="/api/proactive/match-v2">Try New Matching</button>
{% else %}
  <button hx-post="/api/proactive/match">Match Offers</button>
{% endif %}
```

4. Add seed config entry if the flag controls a runtime behavior:

```python
# app/startup.py — _seed_system_config()
seeds = [
    ...
    ("proactive_matching_v2_enabled", "false", "Enable v2 matching algorithm"),
]
```

## MVP Mode Gating Pattern

`mvp_mode` gates entire feature areas. The pattern is consistent: pass `mvp_mode` in context, wrap feature sections in `{% if not mvp_mode %}`:

```python
# Always pass mvp_mode in context for any partial that might have gated features
ctx["mvp_mode"] = get_settings().mvp_mode
```

```jinja2
{# Show feature teaser to MVP users instead of hiding it entirely #}
{% if mvp_mode %}
  <div class="feature-locked opacity-50 pointer-events-none relative">
    <div class="absolute inset-0 flex items-center justify-center z-10">
      <span class="bg-white border rounded px-2 py-1 text-xs text-gray-500">
        Available in full version
      </span>
    </div>
    <!-- Ghost of the feature -->
    <div class="stat-card blur-sm">Enrichment</div>
  </div>
{% else %}
  <!-- Full feature -->
{% endif %}
```

## Gradual Rollout via User Role

Without per-user flags, use role-based rollout as a proxy:

```python
# app/routers/htmx_views.py
@router.get("/v2/partials/proactive")
async def proactive_partial(request: Request, user=Depends(require_user), ...):
    # Roll out new feature to admins first
    show_new_ui = user.role == UserRole.ADMIN or get_settings().proactive_matching_v2_enabled
    ctx = {"show_new_ui": show_new_ui}
    return template_response("htmx/partials/proactive/list.html", request, ctx)
```

## Anti-Patterns

### WARNING: Database-Backed Feature Flags Without Infrastructure

AVOID storing feature flags in `system_config` (the key-value table) and toggling them via admin UI without a plan for cache invalidation. Every request hitting the DB for flag state is an N+1 pattern.

**The Fix:** For simple on/off flags, use environment variables. For per-user experiments, use user roles as a gate. Only add DB-backed flags if you cache them with Redis TTL.

### WARNING: Flags That Never Get Cleaned Up

Every `{% if mvp_mode %}` conditional is tech debt. When a feature graduates, remove the flag and the branch — don't leave dead conditionals in templates.

**The Fix:** File a cleanup ticket when a flag is promoted. Comment the flag with its intended removal condition:

```python
# CLEANUP: Remove when proactive_v2 ships to all users (target: Q2)
proactive_matching_v2_enabled: bool = False
```

See the **fastapi** skill for dependency injection patterns and the **redis** skill for caching flag state at scale.
