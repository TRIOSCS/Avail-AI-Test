# Distribution Reference

## Contents
- AvailAI's Distribution Model
- Internal Adoption Channels
- Onboarding New Users
- Referral Patterns Within the Tool
- Anti-Patterns

---

## AvailAI's Distribution Model

AvailAI is a **closed enterprise tool** — no public signup, no SEO, no paid acquisition. Distribution means: getting existing Trio Supply Chain Solutions staff to adopt it and use it daily.

The distribution surface is:
1. **New user activation:** User is added to Azure AD → auto-created on first OAuth login → must reach first RFQ sent
2. **Feature adoption:** Existing users must discover and adopt new workflows (proactive matching, email mining, buy plans)
3. **Word-of-mouth within team:** Buyers share useful searches or vendor contacts internally

There are no external distribution channels to optimize.

---

## Internal Adoption Channels

### Direct URL / Bookmark

Users arrive via direct URL. The root `GET /` redirects to `/v2/requisitions` for authenticated users. Make this destination worth bookmarking — it should show meaningful state, not an empty list.

```python
# app/routers/auth.py — root redirect
@router.get("/")
async def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/v2/requisitions")
    return RedirectResponse("/auth/login")
```

**Optimization opportunity:** Redirect to `/v2/` (dashboard) instead of `/v2/requisitions` for new users. Dashboard provides orientation; requisitions list starts empty and confuses new users.

### Sidebar Navigation

**File:** `app/templates/htmx/partials/shared/sidebar.html`

The sidebar is the primary discovery surface for all features. Section labels ("Opportunity", "Relationships") are the only information architecture framing users see.

```html
{# Current sidebar sections #}
<span class="uppercase text-xs font-semibold text-brand-300 tracking-wider">Opportunity</span>
{# Requisitions, Part Search, Proactive, Buy Plans #}

<span class="uppercase text-xs font-semibold text-brand-300 tracking-wider">Relationships</span>
{# Vendors, My Vendors, Customers, Quotes, Prospecting #}
```

If adoption of "Proactive" matching is low, the label placement is likely the cause — it's buried in a list with no visual differentiation from older features.

### In-App Feature Nudges

See the **orchestrating-feature-adoption** skill for banners, tooltips, and activation nudges wired to feature flags.

---

## Onboarding New Users

New users are auto-created on first OAuth callback (`app/routers/auth.py`). There is no explicit onboarding flow — they land directly at `/v2/requisitions`.

**Recommended pattern** (from **designing-onboarding-paths** skill): detect first-session users and route them through a checklist before showing the main UI.

```python
# app/routers/htmx_views.py — dashboard_partial endpoint
# Check for first-time user and inject onboarding state
@router.get("/v2/partials/dashboard")
async def dashboard_partial(request: Request, db: Session = Depends(get_db),
                             user=Depends(require_user)):
    is_new = user.login_count == 1  # first login
    return templates.TemplateResponse("htmx/partials/dashboard.html", {
        "request": request,
        "is_new_user": is_new,
        ...
    })
```

```html
{# dashboard.html — conditional onboarding banner #}
{% if is_new_user %}
<div class="bg-brand-50 border border-brand-200 rounded-xl p-4 mb-6">
  <p class="text-brand-800 text-sm font-medium">Welcome to AvailAI</p>
  <p class="text-brand-600 text-sm mt-1">Start by creating a requisition to search supplier APIs.</p>
</div>
{% endif %}
```

---

## Referral Patterns Within the Tool

Internal "referral" in AvailAI means: one user's action surfaces value to another user or to management.

- **Shared requisitions:** Requisitions are shared across the team — a buyer who creates one generates a discovery artifact for colleagues
- **Proactive matching:** Vendors flagged by one user appear in the proactive feed for all buyers
- **Buy plan exports:** Quote reports (PDF, `templates/documents/`) get shared outside the app

**Distribution insight:** If the PDF quote report quality is poor, it reduces perceived credibility of the tool in stakeholder reviews. See the **clarifying-market-fit** skill for copy on exported documents.

---

## Anti-Patterns

### WARNING: Root Redirects to Empty List

**The Problem:**
```python
# BAD — new users land on an empty requisitions list
return RedirectResponse("/v2/requisitions")
```

**Why This Fails:** A new user's first view is "No items found." with no context. This creates the impression the tool is broken or useless before they've had a chance to understand it.

**The Fix:**
```python
# GOOD — route authenticated users to the dashboard first
return RedirectResponse("/v2/")
```

The dashboard shows stat cards (even at zero), quick actions, and a welcome message — all of which orient the user better than an empty list.

### WARNING: No New User Detection

**The Problem:** The OAuth callback auto-creates users but sets no `first_login` or `login_count` flag, making it impossible to show onboarding UI or track activation.

**The Fix:** Add `login_count: int` to the `User` model (via Alembic migration). Increment on each successful callback. Use this to gate onboarding flows and measure time-to-activation.
