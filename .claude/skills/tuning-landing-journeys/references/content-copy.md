# Content Copy Reference

## Contents
- Copy Hierarchy in AvailAI Templates
- Login Page Copy
- Dashboard Copy
- Empty State Copy Patterns
- Microcopy Standards
- Anti-Patterns

---

## Copy Hierarchy in AvailAI Templates

AvailAI copy lives in three places:

| Location | Where | Examples |
|----------|-------|---------|
| Static template copy | Jinja2 `.html` files | "Sign in to continue", "Welcome back" |
| Dynamic copy | Template variables | `{{ user_name }}`, `{{ open_reqs_count }}` |
| Sidebar nav labels | `partials/shared/sidebar.html` | "Opportunity", "Relationships" |

There is no CMS. All copy changes require editing Jinja2 templates directly and redeploying.

---

## Login Page Copy

**File:** `app/templates/htmx/login.html`

The login page is the only surface a new user sees before authentication. It has three copy slots:

```
[Logo]
[Tagline under logo]       ← "Trio Supply Chain Solutions"
[Card heading]             ← "Sign in to continue"
[CTA button]               ← "Sign in with Microsoft"
[Card footer]              ← "Trio Supply Chain Solutions" (repeated)
```

**Current problem:** "Trio Supply Chain Solutions" appears twice and the card heading is generic. The tagline slot is wasted on a company name the user already knows.

### DO: Use the tagline slot for a value statement

```html
{# Under the logo — app/templates/htmx/login.html #}
<p class="mt-2 text-sm text-brand-300 tracking-wide">
  Electronic component sourcing, automated.
</p>
```

### DO: Remove the duplicate footer copy

```html
{# Remove this — it's already above the card #}
{# <p class="mt-6 text-xs text-brand-400 text-center">Trio Supply Chain Solutions</p> #}
```

---

## Dashboard Copy

**File:** `app/templates/htmx/partials/dashboard.html`

### Welcome Message

Current: `Welcome back, {{ user_name }}`

This is correct — personalized, warm, functional. Do not change it.

### Stat Card Labels

Current labels ("Open Requisitions", "Active Vendors", "Companies") are factual and accurate. Keep them.

### Quick Actions Heading

```html
{# Current — fine for a functional tool #}
<h3 class="text-base font-semibold text-gray-900 mb-4">Quick Actions</h3>
```

If users are consistently bypassing the dashboard to use the sidebar, consider renaming to "Start here" or removing the heading entirely — the buttons are self-explanatory.

---

## Empty State Copy Patterns

Write empty state copy as: **[What's missing] + [Why it matters] + [What to do]**

```html
{# Pattern: three-line empty state #}
<p class="text-gray-900 font-medium mb-1">No vendors yet</p>
<p class="text-gray-500 text-sm mb-4">
  Add vendors to send RFQs and track reliability scores.
</p>
<button ...>Add vendor</button>
```

| List | Heading | Subtext | CTA |
|------|---------|---------|-----|
| Requisitions | "No requisitions yet" | "Create one to search 10 supplier APIs at once." | "New requisition" |
| Vendors | "No vendors yet" | "Add vendors to send RFQs and track scores." | "Add vendor" |
| Customers | "No customers yet" | "Add customers to manage quotes and buy plans." | "Add customer" |
| Sightings | "No results yet" | "Run a search to find available stock." | "Search parts" |

---

## Microcopy Standards

### Button Labels

- Use **verb + noun** for primary actions: "Create requisition", "Send RFQ", "Add vendor"
- Use **verb only** for secondary/destructive: "Cancel", "Delete", "Dismiss"
- NEVER use "Click here", "Submit", or "OK" — they have no meaning without context

### Status Copy

Status labels come from `app/constants.py` StrEnum — do not hardcode them in templates:

```html
{# GOOD — use the constant value #}
<span class="badge">{{ req.status }}</span>

{# BAD — hardcoded string that diverges from the model #}
<span class="badge">open</span>
```

### Loading States

Match the loading message to the action being awaited:

```html
{# GOOD — specific #}
Loading pipeline insights...

{# BAD — generic, creates uncertainty #}
Loading...
Please wait...
```

---

## Anti-Patterns

### WARNING: Repeated Company Name

**The Problem:**
```html
<!-- login.html — "Trio Supply Chain Solutions" appears twice -->
<p class="mt-2 text-sm text-brand-300">Trio Supply Chain Solutions</p>
...
<p class="mt-6 text-xs text-brand-400 text-center">Trio Supply Chain Solutions</p>
```

**Why This Fails:** The second instance adds no information. It signals "we ran out of things to say" and wastes the only below-button copy slot — which is premium real estate for trust signals or contact info.

**The Fix:** Remove the footer instance. Replace with a support link or leave blank.

### WARNING: Generic Action Labels

**The Problem:**
```html
<!-- BAD — action label detached from context -->
<button>Create Requisition</button>
<!-- On a page where the user just landed from search results -->
```

**The Fix:** Context-match the label. If the user just searched for a part, "Source this part" or "Create requisition for this part" converts better than the generic label.
