# Engagement & Adoption Reference

## Contents
- Feature Discovery Surfaces
- Progressive Disclosure
- Navigation Activation Tracking
- Anti-Patterns
- Adoption Audit Checklist

---

## Feature Discovery Surfaces

AvailAI's primary navigation is the sidebar in `app/templates/base.html`. Feature adoption starts with sidebar visibility — items hidden via MVP gate or permission level are never discovered.

```html
<!-- app/templates/base.html — sidebar nav item pattern -->
<nav id="sidebar">
  {% if not config.mvp_mode %}
    <a hx-get="/v2/dashboard" hx-target="#main-content"
       class="nav-item {% if active_page == 'dashboard' %}active{% endif %}">
      Dashboard
    </a>
  {% endif %}

  <a hx-get="/v2/requisitions" hx-target="#main-content"
     class="nav-item {% if active_page == 'requisitions' %}active{% endif %}">
    Requisitions
  </a>
</nav>
```

**Key insight:** Feature adoption for AvailAI's power features (proactive matching, email mining) depends on users first completing the core loop: create requisition → search → send RFQ → receive response. Don't surface advanced features before this loop is complete.

---

## Progressive Disclosure

Surface advanced features contextually within the workflow, not in the nav.

```html
<!-- app/templates/htmx/partials/requisitions/detail.html -->
<!-- Show proactive matching nudge only after first RFQ is sent -->
{% if requisition.rfqs_sent_count > 0 and not user.has_seen_proactive_tip %}
  <div class="feature-nudge" data-feature="proactive-matching">
    <strong>New:</strong> AvailAI can automatically match vendor offers to your purchase history.
    <a hx-get="/v2/proactive" hx-target="#main-content">See Proactive Matches</a>
    <button hx-post="/api/user/dismiss-tip" hx-vals='{"tip": "proactive-matching"}'>Dismiss</button>
  </div>
{% endif %}
```

```python
# app/routers/api/user.py — dismiss tip endpoint
@router.post("/api/user/dismiss-tip")
async def dismiss_tip(
    tip: str = Form(...),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    user.dismissed_tips = (user.dismissed_tips or []) + [tip]
    db.commit()
    return HTMLResponse("")  # HTMX removes the nudge via hx-swap="outerHTML"
```

---

## Navigation Activation Tracking

Track which nav items users actually click to identify underused features. Use Loguru structured logging — do NOT use a third-party analytics SDK.

```python
# app/routers/htmx_views.py — log navigation events
@router.get("/v2/proactive")
async def proactive_page(request: Request, user=Depends(require_user)):
    logger.info("feature_visited", extra={
        "feature": "proactive_matching",
        "user_id": user.id,
        "request_id": request.state.request_id,
    })
    return templates.TemplateResponse("htmx/partials/proactive/list.html", {...})
```

Parse these logs to build a feature adoption funnel:

```bash
# Count unique users who visited proactive matching this week
docker compose logs app | grep '"feature": "proactive_matching"' | \
  python3 -c "import sys,json; ids=set(); [ids.add(json.loads(l)['user_id']) for l in sys.stdin if 'user_id' in l]; print(len(ids))"
```

See the **fastapi** skill for request_id middleware setup.

---

## Anti-Patterns

### WARNING: Feature Gating Without Discovery Path

**The Problem:**
```html
<!-- BAD — feature is hidden entirely, user never learns it exists -->
{% if not config.mvp_mode %}
  <a href="/v2/dashboard">Dashboard</a>
{% endif %}
```

**Why This Breaks:** MVP-gated features disappear silently. When `MVP_MODE` is later set to `false`, users have no context for the new feature and adoption is low.

**The Fix:**
```html
<!-- GOOD — show a "coming soon" or upgrade prompt -->
{% if config.mvp_mode %}
  <a class="nav-item nav-item--locked" title="Available in full version">
    Dashboard <span class="badge">Soon</span>
  </a>
{% else %}
  <a hx-get="/v2/dashboard" hx-target="#main-content" class="nav-item">Dashboard</a>
{% endif %}
```

---

## Adoption Audit Checklist

```
Copy this checklist when reviewing a feature for adoption:
- [ ] Feature is reachable from primary navigation or contextual nudge
- [ ] Empty state has a clear CTA to use the feature
- [ ] Feature is logged via logger.info() for funnel analysis
- [ ] Feature works correctly when MVP_MODE=true (hidden or teased)
- [ ] Feature works correctly when MVP_MODE=false (fully visible)
- [ ] Advanced features surface only after core loop completion
- [ ] Dismissible nudges store state server-side (not localStorage)
