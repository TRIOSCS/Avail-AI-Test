# Growth Engineering Reference

## Contents
- Growth Levers in an Enterprise Internal Tool
- First-Session Activation
- Feature Loop Patterns
- Re-Engagement Patterns
- Technical Growth Hooks

---

## Growth Levers in an Enterprise Internal Tool

AvailAI's growth model is **depth over breadth** — the user base is fixed (Trio Supply Chain Solutions staff), so growth means:

1. **Activation:** Get new users to first RFQ sent
2. **Retention:** Get activated users to use the tool daily instead of email/spreadsheets
3. **Expansion:** Get activated users to adopt secondary workflows (proactive matching, email mining, buy plans)

No viral loops, no referral programs, no SEO. Growth engineering here is pure product — reduce friction in high-value workflows.

---

## First-Session Activation

The single highest-leverage action: ensure a new user creates their first requisition within the first session.

**Current state:** New users land at `/v2/requisitions` (empty list) with no guidance.

**Improved flow:**

```python
# app/routers/htmx_views.py
@router.get("/v2/partials/requisitions")
async def requisitions_partial(request: Request, db: Session = Depends(get_db),
                                user=Depends(require_user)):
    requisitions = db.query(Requisition).filter_by(created_by=user.id).all()
    is_first_time = len(requisitions) == 0 and user.login_count <= 2
    return templates.TemplateResponse("htmx/partials/requisitions/list.html", {
        "request": request,
        "requisitions": requisitions,
        "show_activation_prompt": is_first_time,
    })
```

```html
{# app/templates/htmx/partials/requisitions/list.html #}
{% if show_activation_prompt %}
<div class="bg-brand-50 border border-brand-200 rounded-xl p-6 mb-6 text-center">
  <p class="text-brand-900 font-semibold mb-1">Start sourcing in 30 seconds</p>
  <p class="text-brand-700 text-sm mb-4">
    Enter a part number to search 10 supplier APIs simultaneously.
  </p>
  <button class="px-5 py-2.5 bg-brand-600 text-white text-sm font-semibold rounded-lg hover:bg-brand-700"
          @click="$dispatch('open-create-requisition')">
    Create my first requisition
  </button>
</div>
{% endif %}
```

---

## Feature Loop Patterns

Each core workflow has a natural loop. Strengthen the loop by making the next step obvious:

### Search Loop

```
Search results  ->  "Send RFQ to these vendors" CTA  ->  RFQ sent  ->  "Check inbox" nudge
```

After search results load, the primary CTA should be "Send RFQ" not "Save sighting." The save is implicit; the send is the value moment.

### Proactive Matching Loop

```
New vendor offer arrives  ->  Proactive match score shown  ->  "Prepare batch" CTA  ->  Sent
```

**File:** `app/templates/htmx/partials/proactive/` — ensure the primary action button is the batch send, not a detail view.

### Email Mining Loop

```
Inbox monitored  ->  Reply parsed by Claude  ->  "Review parsed offer" notification  ->  Accept/reject
```

If this loop is not completing (users never review parsed offers), the notification surface is broken. Check `app/jobs/inbox_monitor.py` logs for successful parses with no downstream review actions.

---

## Re-Engagement Patterns

For users who logged in but never completed activation:

```python
# app/jobs/re_engagement.py (hypothetical)
# Run daily — find users with 0 requisitions after 3+ days
def find_unactivated_users(db: Session) -> list[User]:
    cutoff = datetime.utcnow() - timedelta(days=3)
    return (
        db.query(User)
        .filter(User.created_at < cutoff)
        .filter(~User.requisitions.any())
        .all()
    )
```

**Option A:** Show a re-engagement banner on next login (low-friction, no email required):

```html
{# dashboard.html — re-engagement banner for users with 0 requisitions #}
{% if show_reengagement %}
<div class="bg-amber-50 border border-amber-200 rounded-xl p-4 mb-6 flex items-start gap-3">
  <p class="text-amber-800 text-sm">
    You haven't created a requisition yet — search 10 APIs with one form.
    <button class="ml-2 underline font-medium" @click="$dispatch('open-create-requisition')">
      Try it now
    </button>
  </p>
</div>
{% endif %}
```

**Option B:** Microsoft Graph API email (already wired via `email_service.py`) — send a single nudge email after 3 days inactive. Use sparingly.

---

## Technical Growth Hooks

### HTMX Preload for Key Transitions

The `htmx-ext-preload` extension is already loaded. Use it on the dashboard's primary CTA destination to make the transition feel instant:

```html
{# dashboard.html — preload the requisitions partial on hover #}
<button hx-get="/v2/partials/search"
        hx-target="#main-content"
        hx-push-url="/v2/search"
        hx-preload="mouseover">
  Search Parts
</button>
```

### Pipeline Insights as a Retention Hook

**File:** `app/templates/htmx/partials/dashboard.html` (lines 94-107)

Pipeline insights lazy-load below the fold. This is a **retention hook** — users who see meaningful pipeline data return more often. Ensure the pipeline insights partial shows:
- Active deals with stale sightings (prompt re-search)
- Unreviewed proactive matches (prompt action)
- Replies awaiting manual review (prompt inbox check)

Each stat should link directly to the relevant list with one `hx-get`.

See the **orchestrating-feature-adoption** skill for wiring feature adoption nudges to these pipeline stats.
