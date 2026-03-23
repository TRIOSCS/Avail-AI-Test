# Distribution Reference

## Contents
- In-App Distribution Channels
- Email-Based Distribution (RFQ Workflow)
- Word-of-Mouth Signals
- Anti-Patterns

AvailAI has no public marketing site, blog, or social presence tracked in this repo. Distribution is entirely **product-led**: users invite colleagues, and the RFQ workflow creates external touchpoints with vendors that can generate inbound interest.

## In-App Distribution Channels

### User Invitation (via Azure AD)

User access is controlled via Azure AD OAuth2 (`app/routers/auth.py`). New users arrive through IT provisioning, not self-serve signup. Copy in the login and onboarding flows should assume the user was invited and is task-ready, not discovery-browsing.

```html
{# app/templates/htmx/login.html — message for first-time users #}
<p class="text-xs text-gray-400 text-center mt-4">
  Sign in with your company Microsoft account.
  Contact your admin if you need access.
</p>
```

### RFQ as a Distribution Surface

Every outbound RFQ email sent via `email_service.send_batch_rfq()` includes the `[AVAIL-{id}]` tag in the subject. This tag is a passive brand impression with vendors. If the subject line prefix is visible, make it professional:

```python
# app/email_service.py — subject line format
subject = f"[AVAIL-{req_id}] RFQ: {part_numbers_summary} — {company_name}"
```

**DON'T** use generic subjects like "Quote Request" — the `[AVAIL-{id}]` tag should appear alongside a professional, specific subject that signals the sender is organized.

## Word-of-Mouth Signals

AvailAI's best distribution mechanism is buyers sharing results with colleagues. Design for shareable moments:

```html
{# Shareable success state — after search returns results #}
{# app/templates/htmx/partials/search/results.html #}
<div class="flex items-center justify-between mb-3">
  <p class="text-xs text-gray-500">
    Found {{ result_count }} offers from {{ vendor_count }} suppliers
    in {{ search_duration_ms }}ms
  </p>
  {# Export button creates a shareable artifact #}
  <button hx-get="/v2/partials/requisitions/{{ req_id }}/export"
          hx-target="#export-result"
          class="text-xs text-brand-600 hover:underline">
    Export Results
  </button>
</div>
```

Showing the search duration and vendor count in results is a **trust signal and a shareable stat** — buyers screenshot these and send them to managers.

## WARNING: Feature-Gated Surfaces That Block Sharing

### The Problem

```python
# BAD — hiding results behind MVP_MODE gates stops users from sharing wins
if not settings.mvp_mode:
    return templates.TemplateResponse("htmx/partials/search/results.html", context)
else:
    return HTMLResponse("")  # Silent empty response
```

**Why This Breaks:** If a buyer can't show a colleague the search results page, they can't advocate for the tool. Silent empty responses are worse than "feature coming soon" copy.

**The Fix:**

```python
# GOOD — show a teaser or "coming soon" state instead of nothing
if settings.mvp_mode:
    return templates.TemplateResponse(
        "htmx/partials/shared/feature_gated.html",
        {"feature_name": "Advanced Export", "context": context}
    )
```

## Related Skills

- See the **orchestrating-feature-adoption** skill for feature flag patterns
- See the **fastapi** skill for route gating with MVP_MODE
