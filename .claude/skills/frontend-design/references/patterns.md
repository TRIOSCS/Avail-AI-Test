# Patterns Reference

## Contents
- DO / DON'T Pairs
- Anti-Patterns
- Template Header Comments
- Empty States
- Error States

---

## DO / DON'T Pairs

### Status display

```html
<!-- DON'T: raw colors inline -->
<span style="color: green">Active</span>
<span class="text-green-600 bg-green-100 px-2 py-0.5 rounded-full text-xs">Active</span>

<!-- DO: semantic badge class -->
<span class="badge-success">Active</span>
```

**Why:** Inline colors bypass the design system. When the brand changes (e.g., switching emerald → teal for success), you'd need a codebase-wide search instead of one CSS change.

---

### Data values

```html
<!-- DON'T: proportional font for part numbers and prices -->
<td>ATtiny85-20PU</td>
<td>$0.4523</td>

<!-- DO: monospace font-data class -->
<td class="font-data">ATtiny85-20PU</td>
<td class="font-data text-right">${{ '%.4f'|format(price) }}</td>
```

**Why:** Part numbers have critical visual alignment differences (e.g., `ATtiny85` vs `AT tiny85`). Proportional fonts make these hard to scan in dense tables.

---

### HTMX navigation

```html
<!-- DON'T: anchor with href AND hx-get -->
<a href="/v2/vendors/{{ id }}" hx-get="/v2/partials/vendors/{{ id }}"
   hx-target="#main-content">View</a>

<!-- DO: either pure hx-get (for SPA behavior) or plain anchor (for full reload) -->
<div class="cursor-pointer" hx-get="/v2/partials/vendors/{{ id }}"
     hx-target="#main-content" hx-push-url="/v2/vendors/{{ id }}">
  View
</div>
```

**Why:** Dual-mode links fire twice — browser navigates AND HTMX swaps. Results in double requests and history stack corruption.

---

### Alpine state for UI vs server state

```html
<!-- DON'T: use Alpine to store data fetched from the server -->
<div x-data="{ vendors: [] }" x-init="fetch('/api/vendors').then(r=>r.json()).then(d=>vendors=d)">

<!-- DO: let HTMX load server data; use Alpine only for UI state -->
<div x-data="{ filterOpen: false, selectedIds: [] }"
     hx-get="/v2/partials/vendors/list"
     hx-trigger="load"
     hx-target="this">
```

**Why:** Mixing Alpine fetch with HTMX defeats the purpose of server-driven UI. HTMX handles data loading; Alpine handles UI state (open/closed, selected, expanded).

---

## WARNING: Anti-Patterns

### WARNING: x-show without x-cloak

**The Problem:**
```html
<!-- BAD — flash of visible content on Alpine init -->
<div x-show="open">Dropdown</div>
```

**Why This Breaks:** Alpine initializes asynchronously. The element is visible in the DOM for ~50-100ms before Alpine hides it. Users see a flash.

**The Fix:**
```html
<!-- GOOD -->
<div x-show="open" x-cloak>Dropdown</div>
```

`[x-cloak] { display: none }` is defined in `styles.css`.

---

### WARNING: Hardcoded colors for dynamic thresholds

**The Problem:**
```html
<!-- BAD — score color hardcoded, not data-driven -->
<span class="text-green-600">{{ score }}</span>
```

**The Fix:**
```html
<!-- GOOD — color follows the 66/33 threshold used everywhere -->
<span class="
  {% if score >= 66 %}text-emerald-700
  {% elif score >= 33 %}text-brand-600
  {% else %}text-amber-700{% endif %}">{{ score }}</span>
```

**Why:** The 66/33 threshold is used on score bars, badge colors, and stat displays. Deviating creates visual inconsistency that confuses users about meaning.

---

## Template Header Comments

Every partial must start with a header comment. This is enforced by code review.

```html
{#
  vendors/vendor_card.html — One-line description of what this partial renders.
  Called by: vendors/list.html (card grid mode)
  Depends on: Alpine.js, brand palette, Tailwind CSS
  Context vars: v (VendorCard schema), claim_map (dict[int, ClaimInfo])
#}
```

No header = PR blocked.

---

## Empty States

Never render an empty container. Every list/table needs an empty state.

```html
{% if items %}
  <div class="table-wrapper">
    <!-- table content -->
  </div>
{% else %}
  <div class="card-padded text-center py-12">
    <svg class="mx-auto h-10 w-10 text-gray-300 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
    </svg>
    <p class="text-sm text-gray-500">No items found</p>
    <p class="text-xs text-gray-400 mt-1">Try adjusting your filters</p>
  </div>
{% endif %}
```

---

## Error States

For HTMX error responses, return an HTML fragment with the error — never a JSON error in a partial endpoint.

```html
{# error_partial.html — rendered by FastAPI on 4xx/5xx for HTMX requests #}
<div class="card-padded border-rose-200">
  <div class="flex items-center gap-3 text-rose-700">
    <svg class="h-5 w-5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
    </svg>
    <span class="text-sm font-medium">{{ error_message }}</span>
  </div>
</div>
