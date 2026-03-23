# Conversion Optimization Reference

## Contents
- Journey Stages and Drop-off Points
- Login Page Optimization
- Dashboard Activation
- Empty State CTAs
- Anti-Patterns
- Playwright Audit Workflow

---

## Journey Stages and Drop-off Points

The AvailAI conversion funnel has four stages. Each stage has one template to optimize:

```
Login  ->  Dashboard  ->  First Requisition  ->  First RFQ Send
  |              |                 |                    |
login.html   dashboard.html   requisitions/         email_service
                              create modal          send_batch_rfq()
```

**Activation definition:** User who has sent at least one RFQ is "activated." Everything before that is pre-activation.

---

## Login Page Optimization

**File:** `app/templates/htmx/login.html`

### Current state (minimal)

```html
<h2 class="text-lg font-semibold text-white text-center mb-6">Sign in to continue</h2>
<a href="/auth/login" class="w-full flex items-center justify-center ...">
  Sign in with Microsoft
</a>
<p class="mt-6 text-xs text-brand-400 text-center">Trio Supply Chain Solutions</p>
```

### DO: Add a value hook above the button

```html
<h2 class="text-lg font-semibold text-white text-center mb-2">Sign in to continue</h2>
<p class="text-sm text-brand-300 text-center mb-6">
  Source electronic components from 10 APIs in seconds.
</p>
<a href="/auth/login" class="w-full flex items-center justify-center ...">
  Sign in with Microsoft
</a>
```

**Why:** First-time users need context on what they're signing into. "Sign in to continue" assumes they already know the value.

### DON'T: Add marketing noise to the login card

```html
<!-- BAD — overloads the single-action page -->
<ul class="text-brand-300 text-sm mb-6">
  <li>✓ 10 supplier APIs</li>
  <li>✓ Automated RFQs</li>
  <li>✓ Claude-powered parsing</li>
</ul>
```

**Why:** The login page has one job: get the user to click "Sign in with Microsoft." Feature lists create visual noise that delays or distracts from that click.

---

## Dashboard Activation

**File:** `app/templates/htmx/partials/dashboard.html`

### DO: Single dominant primary CTA

```html
{# Quick actions — clear primary/secondary hierarchy #}
<div class="flex flex-wrap gap-3">
  <button class="px-6 py-3 bg-brand-600 text-white text-sm font-semibold rounded-lg
                 hover:bg-brand-700 shadow-sm"
          @click="$dispatch('open-create-requisition')">
    New Requisition
  </button>
  <button class="px-4 py-2.5 border border-gray-200 text-gray-600 text-sm rounded-lg
                 hover:bg-gray-50"
          hx-get="/v2/partials/search" hx-target="#main-content" hx-push-url="/v2/search">
    Search Parts
  </button>
</div>
```

### DON'T: Equal visual weight for primary and secondary actions

```html
<!-- BAD — both buttons look the same weight, user hesitates -->
<button class="px-5 py-2.5 bg-brand-500 text-white ...">Create Requisition</button>
<button class="px-5 py-2.5 bg-white border border-brand-200 text-brand-700 ...">Search Parts</button>
```

**Why:** When primary and secondary CTAs have the same visual weight (same padding, same border-radius, similar colors), users pause to decide. One clear primary reduces cognitive load.

### Stat Cards as Navigation

The three stat cards (Open Requisitions, Active Vendors, Companies) already work as CTAs via `hx-get`. Keep this pattern — zero-state users with `0` counts still see the navigation affordance.

---

## Empty State CTAs

**File:** `app/templates/htmx/partials/shared/empty_state.html`

### DO: Context-specific empty state copy and actions

```html
{# For requisitions list #}
<div class="text-center py-12">
  <div class="inline-flex items-center justify-center w-12 h-12 rounded-full bg-gray-100 mb-3">
    <svg class="h-6 w-6 text-gray-400" ...></svg>
  </div>
  <p class="text-gray-900 font-medium mb-1">No requisitions yet</p>
  <p class="text-gray-500 text-sm mb-4">Create a requisition to search 10 supplier APIs at once.</p>
  <button class="px-4 py-2 bg-brand-600 text-white text-sm rounded-lg hover:bg-brand-700"
          @click="$dispatch('open-create-requisition')">
    Create requisition
  </button>
</div>
```

### DON'T: Generic empty state with no action

```html
<!-- BAD — dead end, user has nowhere to go -->
<div class="text-center py-12">
  <p class="text-gray-500">No items found.</p>
</div>
```

**Why:** Generic empty states are dead ends. Users who reach an empty list have already taken a journey step — they need a clear next action, not a wall of gray text.

---

## WARNING: Missing Analytics

**Detected:** No conversion tracking (no gtag, Segment, PostHog, or custom events) in `app/static/htmx_app.js` or any template.

**Impact:** You cannot measure which CTAs work, where users drop off, or whether changes improve conversion.

**Recommended Solution:** Wire activity tracking to key conversion events via the existing `ActivityService`. See the **instrumenting-product-metrics** skill for how to track activation events against the existing activity schema.

---

## Playwright Audit Workflow

Use Playwright to audit the current login-to-dashboard journey:

```typescript
// tests/e2e/conversion-audit.spec.ts
test('login page has value hook', async ({ page }) => {
  await page.goto('/auth/login');
  // Check value copy exists above the sign-in button
  await expect(page.locator('p.text-brand-300')).toBeVisible();
});

test('dashboard primary CTA is visually dominant', async ({ page }) => {
  // After auth...
  const primaryBtn = page.locator('button:has-text("New Requisition")');
  const secondaryBtn = page.locator('button:has-text("Search Parts")');
  // Primary should have bg-brand-600, secondary should NOT
  await expect(primaryBtn).toHaveClass(/bg-brand-6/);
  await expect(secondaryBtn).not.toHaveClass(/bg-brand-6/);
});
```

See the **playwright** skill for full E2E setup and authenticated test patterns.
