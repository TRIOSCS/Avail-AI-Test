# Engagement & Adoption Reference

## Contents
- Dashboard as Activation Hub
- Progressive Disclosure via Feature Flags
- Nudge Patterns in Partials
- SSE Progress as Engagement Hook
- Anti-Patterns

## Dashboard as Activation Hub

`app/templates/htmx/partials/dashboard.html` is the primary engagement surface. Stat cards link directly to filtered list views — clicking "Open Requisitions" navigates to the requisitions partial pre-filtered to open status.

```jinja2
{# dashboard.html — stat card with HTMX nav #}
<a hx-get="/v2/partials/requisitions?status=open"
   hx-target="#main-content"
   hx-push-url="/v2/requisitions"
   class="stat-card hover:shadow-md transition-shadow">
  <span class="stat-number">{{ stats.open_reqs }}</span>
  <span class="stat-label">Open Requisitions</span>
</a>
```

When `open_reqs` is 0, replace the number with a CTA instead:

```jinja2
{% if stats.open_reqs == 0 %}
  <a hx-get="/v2/partials/requisitions/new" hx-target="#modal-content"
     class="stat-card stat-card--empty">
    <span class="text-sm text-indigo-600 font-medium">+ Create first requisition</span>
  </a>
{% else %}
  <span class="stat-number">{{ stats.open_reqs }}</span>
{% endif %}
```

## Progressive Disclosure via Feature Flags

`app/config.py` has per-feature flags used to gate UI surfaces. Pass flags into template context from the router, then use `{% if %}` to show/hide features:

```python
# app/routers/htmx_views.py
@router.get("/v2/partials/dashboard")
async def dashboard_partial(request: Request, db: Session = Depends(get_db), ...):
    settings = get_settings()
    ctx = {
        "mvp_mode": settings.mvp_mode,
        "email_mining_enabled": settings.email_mining_enabled,
        "activity_tracking_enabled": settings.activity_tracking_enabled,
    }
    return template_response("htmx/partials/dashboard.html", request, ctx)
```

```jinja2
{# In template — show enrichment card only when feature is on #}
{% if not mvp_mode %}
  <div class="feature-card">
    <h3>Vendor Enrichment</h3>
    <p>Auto-enrich vendor profiles with company data.</p>
    <button hx-post="/api/enrichment/start">Run Enrichment</button>
  </div>
{% endif %}
```

## Nudge Patterns in Partials

Add contextual nudges directly in list partials when the user has data but hasn't used a feature:

```jinja2
{# In requisitions list — nudge to send RFQ if sightings exist but no RFQ sent #}
{% if req.sighting_count > 0 and req.rfq_count == 0 %}
  <div class="nudge-banner bg-indigo-50 border border-indigo-200 rounded p-3 mb-2">
    <span class="text-indigo-700 text-sm">
      {{ req.sighting_count }} vendors found — ready to send RFQ?
    </span>
    <button hx-get="/v2/partials/rfq/compose/{{ req.id }}"
            hx-target="#modal-content"
            class="ml-2 text-indigo-600 underline text-sm">
      Send RFQ
    </button>
  </div>
{% endif %}
```

## SSE Progress as Engagement Hook

The sourcing search uses SSE streaming to show real-time progress. This keeps users engaged during the 5-10 second multi-source search. See `app/templates/htmx/partials/sourcing/search_progress.html`:

```jinja2
{# search_progress.html — SSE-driven progress bars #}
<div hx-ext="sse"
     sse-connect="/v2/partials/sourcing/{{ requirement_id }}/stream"
     sse-swap="message"
     hx-target="this">
  {% for source in sources %}
    <div class="source-row" id="source-{{ source.name }}">
      <span>{{ source.label }}</span>
      <span class="spinner" id="spinner-{{ source.name }}"></span>
    </div>
  {% endfor %}
</div>
```

When the stream closes, HTMX auto-swaps in the results partial. Users who watch the progress bar are primed to engage with results.

## Anti-Patterns

### WARNING: Feature Nudges as Blocking Modals

AVOID showing feature discovery as a modal that blocks the main workflow. AvailAI users are procurement professionals — interrupting their workflow to pitch a feature destroys trust.

**The Fix:** Inline banner nudges (dismissible via Alpine `x-show`) that appear once per session and are anchored to the relevant workflow step.

### WARNING: Counting Logins for Nudge Timing

Avoid `user.login_count % 3 == 0` patterns for nudge timing. They're unreliable and fire at wrong moments.

**The Fix:** Gate nudges on workflow state: "has sightings but no RFQ sent" is a better trigger than "logged in 3 times."

See the **orchestrating-feature-adoption** skill for structured adoption flow planning.
