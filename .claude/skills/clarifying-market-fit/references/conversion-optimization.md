# Conversion Optimization Reference

## Contents
- CTA Placement and Wiring
- Empty-State Conversion Patterns
- Login Page Optimization
- Anti-Patterns

AvailAI's conversion surfaces are entirely in-app: the login screen, empty states, and first-run flows. There is no funnel from external traffic — users arrive via direct invite/SSO. Conversion here means **activation** (first requisition created, first search run, first RFQ sent).

## CTA Placement and Wiring

Every CTA must be wired to a real HTMX partial — never a dead `#` href.

```html
{# GOOD — wired CTA that loads the new requisition form #}
<button hx-get="/v2/partials/requisitions/new"
        hx-target="#main-content"
        hx-push-url="/requisitions/new"
        class="px-4 py-2 text-sm font-medium text-white bg-brand-500 rounded hover:bg-brand-600">
  New Requisition
</button>
```

```html
{# BAD — dead href that breaks the HTMX navigation model #}
<a href="/requisitions/new" class="...">New Requisition</a>
```

**Why:** HTMX-driven navigation keeps users in the app shell. A full-page reload loses Alpine.js store state (sidebar open/closed, active filters).

## Empty-State Conversion Patterns

Empty states are the highest-leverage copy surface. A new user hits them on every tab.

```html
{# app/templates/htmx/partials/vendors/vendor_card.html — empty vendor state #}
{% if not vendors %}
<div class="text-center py-10 text-gray-400">
  <p class="text-sm font-medium text-gray-700">No vendors tracked yet</p>
  <p class="text-xs mt-1">
    Run a search — vendors are auto-added when sightings are created.
  </p>
</div>
{% endif %}
```

**Copy formula for empty states:**
1. State what's missing (not an error)
2. Explain how it gets populated (reduces anxiety)
3. Offer one CTA if manual action is needed

## Login Page Optimization

The login page (`app/templates/htmx/login.html`) is the only pre-auth surface. Keep value copy above the fold, credential form below.

```html
{# Correct layout order in login.html #}
<div class="flex flex-col items-center gap-6">
  {# 1. Brand + value prop — above fold #}
  <div class="text-center">
    <h1 class="text-xl font-bold">AvailAI</h1>
    <p class="text-sm text-gray-500">
      Search 10 supplier networks. Auto-parse RFQ replies.
    </p>
  </div>
  {# 2. Auth button — primary CTA #}
  <a href="/auth/login"
     class="w-full flex items-center justify-center gap-2 px-4 py-2.5
            text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600">
    Sign in with Microsoft
  </a>
</div>
```

**DON'T** put the value prop below the login button — users never read it there.

## WARNING: Vague Feature Names

### The Problem

```html
{# BAD — tells the user nothing about what they get #}
<p class="text-xs text-gray-500">Use our advanced search capabilities</p>
```

**Why This Breaks:** "Advanced search" is meaningless to a procurement buyer. It doesn't map to their workflow ("I need to find this part from 5 suppliers before 3pm"). Vague copy reduces confidence that the tool will actually solve their problem.

**The Fix:**

```html
{# GOOD — specific outcome language #}
<p class="text-xs text-gray-500">
  Search BrokerBin, Nexar, DigiKey, and 7 more in one click.
</p>
```

## Related Skills

- See the **designing-onboarding-paths** skill for first-run flow patterns
- See the **orchestrating-feature-adoption** skill for feature nudge placement
- See the **htmx** skill for CTA wiring patterns
