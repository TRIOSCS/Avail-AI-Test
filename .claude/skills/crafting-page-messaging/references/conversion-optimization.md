# Conversion Optimization Reference

## Contents
- Page-Level Conversion Patterns
- CTA Design and Placement
- Form and Modal Copy
- Anti-Patterns
- Validation Loop

AvailAI's conversion surfaces are entirely in-app. "Conversion" means: new user completes first requisition, buyer sends first RFQ, team adopts proactive matching. There are no funnels with paid traffic — optimize for activation and workflow adoption.

## Page-Level Conversion Patterns

### Login Page — Single Conversion Goal

The login page has one job: get the user to click "Sign in with Microsoft". Every element either supports that goal or creates noise.

```html
{# app/templates/htmx/login.html — minimal, high-contrast CTA #}
<a href="/auth/login"
   class="w-full flex items-center justify-center gap-3 px-4 py-3 bg-white
          text-gray-800 rounded-lg font-medium hover:bg-gray-50 transition-colors shadow-sm">
  {# Microsoft logo SVG #}
  Sign in with Microsoft
</a>
```

**DO:** Keep the login page to logo + tagline + single CTA. Every additional element dilutes click-through.

**DON'T:** Add feature bullets, testimonials, or "learn more" links on the login screen. The user already has an account — they want in, not a pitch.

### Empty States — Convert to First Action

Empty states are the highest-leverage copy surface. A user seeing an empty requisitions list is one click away from activation or abandonment.

```html
{# app/templates/htmx/partials/materials/list.html — empty materials state #}
{% if not materials %}
<div class="text-center py-16 px-4">
  <div class="mx-auto h-12 w-12 text-gray-300 mb-4">
    {# Part icon SVG #}
  </div>
  <h3 class="text-sm font-semibold text-gray-900">No material cards yet</h3>
  <p class="text-xs text-gray-500 mt-1 max-w-xs mx-auto">
    Material cards are created automatically when you run a search.
    Start by creating a requisition.
  </p>
  <a hx-get="/v2/partials/requisitions/create-form"
     hx-target="#modal-content"
     @click="$dispatch('open-modal')"
     class="mt-4 inline-flex items-center px-3 py-1.5 text-xs font-medium
            text-white bg-brand-500 rounded-lg hover:bg-brand-600">
    Create Requisition
  </a>
</div>
{% endif %}
```

**DO:** Explain WHY the list is empty and what action creates data. Users don't know that material cards are auto-generated from searches.

**DON'T:** Show "No results found" alone. That's a dead end. Always provide a next action.

### Modal Headers — Anchor the Task

Modal headers orient the user. Vague headers cause abandonment.

```html
{# GOOD — specific task #}
<h2 class="text-base font-semibold text-gray-900">Send RFQ to selected vendors</h2>

{# BAD — vague action #}
<h2 class="text-base font-semibold text-gray-900">Confirm action</h2>
```

## CTA Design and Placement

### Primary vs. Secondary Hierarchy

```html
{# app/templates/htmx/partials/requisitions/list.html — action hierarchy #}

{# Primary — brand color, filled #}
<button class="px-4 py-2 bg-brand-500 text-white text-sm font-medium rounded-lg
               hover:bg-brand-600 transition-colors">
  New Requisition
</button>

{# Secondary — outlined, same size #}
<button class="px-4 py-2 border border-gray-300 text-gray-700 text-sm font-medium
               rounded-lg hover:bg-gray-50 transition-colors">
  Export
</button>
```

**Rule:** One primary CTA per view. If you have two filled buttons, neither is primary.

### Destructive Actions

```html
{# Destructive — red, requires confirmation #}
<button class="px-3 py-1.5 text-xs font-medium text-red-600 hover:text-red-700
               border border-red-200 rounded hover:bg-red-50 transition-colors">
  Delete Requisition
</button>
```

Never style destructive and primary CTAs identically. Users scan, not read.

## Form and Modal Copy

### Field Labels — Instruction Over Label

```html
{# GOOD — tells the user what to enter #}
<label class="text-xs font-medium text-gray-500">
  Part numbers (one per line or comma-separated)
</label>

{# BAD — just names the field #}
<label class="text-xs font-medium text-gray-500">Part Numbers</label>
```

### Inline Validation Messages

```html
{# Inline error — specific, actionable #}
<p class="text-xs text-red-500 mt-1">
  At least one part number is required to run a search.
</p>

{# NOT: "Invalid input" or "Error" #}
```

## WARNING: Hedged Copy Kills Conversion

**The Problem:**

```html
{# BAD — hedged language #}
<p class="text-sm text-gray-500">
  AvailAI may be able to find vendor offers for your parts.
</p>
```

**Why This Breaks:** "May be able to" signals uncertainty. Procurement buyers won't adopt a tool that hedges on its core promise. If the feature works, state it as fact.

**The Fix:**

```html
{# GOOD — declarative #}
<p class="text-sm text-gray-500">
  AvailAI searches 10 supplier networks and returns ranked results in under 30 seconds.
</p>
```

## Validation Loop

```
1. Write copy
2. View in browser: npm run dev, navigate to surface
3. Ask: does every element point toward one action?
4. Ask: is there a dead-end (empty state with no CTA)?
5. Fix dead-ends and re-check
6. Only proceed when every state has a next action
```

## Related Skills

- See the **designing-onboarding-paths** skill for first-run flow patterns
- See the **orchestrating-feature-adoption** skill for feature nudges
- See the **frontend-design** skill for Tailwind CTA component patterns
- See the **htmx** skill for wiring HTMX partials to CTAs
