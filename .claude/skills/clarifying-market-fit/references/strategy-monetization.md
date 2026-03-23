# Strategy and Monetization Reference

## Contents
- Current Business Model Context
- Feature Tiering with MVP_MODE
- Value Metric Identification
- Packaging Recommendations
- Anti-Patterns

AvailAI is a B2B SaaS tool deployed for **Trio Supply Chain Solutions** as an internal/white-label platform. The current repo does not include a billing system, paywall, or subscription management. Monetization strategy operates at the contract/deployment level, not in-app.

## Current Business Model Context

| Dimension | Current State |
|-----------|--------------|
| **Billing** | Not in codebase — handled externally |
| **Tiers** | Binary: MVP_MODE on/off |
| **User access** | Azure AD — no self-serve signup |
| **Value metric** | Implicit: searches run, RFQs sent, offers parsed |

## Feature Tiering with MVP_MODE

`app/config.py` exposes `mvp_mode: bool`. When `True`, Dashboard, Enrichment, Teams, and Task Manager are hidden. Core value (Requisitions, Search, RFQ, Vendors) is always available.

```python
# app/config.py
class Settings(BaseSettings):
    mvp_mode: bool = False
```

```html
{# Gate premium features in templates #}
{% if not settings.mvp_mode %}
  <li>
    <a hx-get="/v2/partials/dashboard" hx-target="#main-content">
      Dashboard
    </a>
  </li>
{% endif %}
```

**DON'T** use `mvp_mode` as a permanent pricing gate — it's a feature flag, not a billing tier. For true tiering, add a `plan: str` field to the User model and gate on that instead.

## Value Metric Identification

The strongest value metrics for AvailAI pricing would be:

| Metric | Why it works | How to measure |
|--------|-------------|----------------|
| Searches run / month | Directly tied to buyer activity | `app/search_service.py` log events |
| RFQs sent / month | Measures engagement with core workflow | `app/email_service.py` |
| Offers auto-parsed / month | AI value delivered | `app/services/response_parser.py` |
| Vendors tracked | Data moat indicator | `SELECT COUNT(*) FROM vendors` |

Surface usage counts in the settings/admin panel to make value visible to admins:

```html
{# app/templates/htmx/partials/admin/data_ops.html — already shows counts #}
<div class="grid grid-cols-3 gap-4 mb-6">
  <div class="bg-white rounded-lg shadow border border-gray-200 p-4 text-center">
    <p class="text-xs text-gray-500">Vendors</p>
    <p class="text-xl font-bold text-gray-900">{{ "{:,}".format(vendor_count) }}</p>
  </div>
  {# Add: searches_this_month, rfqs_sent_this_month #}
</div>
```

## Packaging Recommendations

If tiered pricing is added, align tiers to the activation loop stages:

| Tier | Included | Excluded |
|------|---------|---------|
| **Starter** | Requisitions + Search (5 connectors) | AI parsing, Proactive Matching |
| **Pro** | All 10 connectors + AI RFQ parsing | Enrichment, Teams |
| **Enterprise** | Full platform + enrichment worker | — |

```python
# Future: plan-gated feature check (not yet implemented)
def require_plan(min_plan: str):
    def dependency(user: User = Depends(require_user)):
        if user.plan < min_plan:
            raise HTTPException(403, "Upgrade your plan to access this feature")
        return user
    return dependency
```

## WARNING: Using MVP_MODE as a Permanent Pricing Gate

### The Problem

```python
# BAD — using mvp_mode to gate features for different paying customers
if settings.mvp_mode and customer_tier == "free":
    return HTMLResponse("")
```

**Why This Breaks:** `mvp_mode` is a global flag — it affects all users equally. It cannot express per-user or per-tenant billing tiers. Using it this way creates a maintenance trap: you can't have one user on "free" and another on "pro" in the same deployment.

**The Fix:** Add `plan: str` to the `User` model via an Alembic migration, then gate on `user.plan`.

## Related Skills

- See the **orchestrating-feature-adoption** skill for feature flag rollout patterns
- See the **sqlalchemy** skill for adding plan/tier fields to the User model
- See the **fastapi** skill for dependency-based access control
```

---

All 7 files are ready. To write them to disk, I need write permission for `.claude/skills/clarifying-market-fit/`. The files are:

1. `SKILL.md` — overview, ICP summary, quick-start patterns, positioning rules
2. `references/conversion-optimization.md` — CTA wiring, empty-state patterns, login optimization
3. `references/content-copy.md` — voice/tone, microcopy, feature description templates
4. `references/distribution.md` — in-app distribution, RFQ as a brand touchpoint, MVP_MODE gating
5. `references/measurement-testing.md` — activation events, Loguru logging, missing analytics SDK warning
6. `references/growth-engineering.md` — flywheel architecture, proactive matching as retention, nudge patterns
7. `references/strategy-monetization.md` — MVP_MODE tiering, value metrics, packaging recommendations

Shall I proceed with writing these files?
