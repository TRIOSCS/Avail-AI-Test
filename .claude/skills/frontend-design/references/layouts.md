# Layouts Reference

## Contents
- App Shell
- Page Structure
- Grid & Spacing
- Responsive Patterns
- List + Detail Split

---

## App Shell

Defined in `app/templates/base.html`. Fixed structure — do not modify for individual partials.

```
┌──────────────────────────────────────┐
│ topbar (sticky, h-12, z-20)          │
├──────────────────────────────────────┤
│ #main-content                        │
│ (HTMX swap target, pb-[52px])        │
│                                      │
│ ← partials render here →             │
└──────────────────────────────────────┘
│ mobile_nav (fixed bottom, h-[52px])  │
└──────────────────────────────────────┘
```

`#main-content` has `padding-bottom: 52px` to avoid mobile nav overlap. Every partial must account for this — do not add extra bottom padding beyond what content needs.

---

## Page Structure

Standard partial layout for list views:

```html
<div class="p-4 sm:p-6 space-y-4">
  {# Page header #}
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-lg font-semibold text-gray-900">Vendors</h1>
      <p class="text-sm text-gray-500">{{ total }} suppliers</p>
    </div>
    <button class="btn-primary btn-sm">Add Vendor</button>
  </div>

  {# Filters #}
  <div class="filter-bar flex items-center gap-3">
    <!-- filter inputs -->
  </div>

  {# Content #}
  <div class="table-wrapper">
    <!-- table or card grid -->
  </div>
</div>
```

Detail views use tab pattern:

```html
<div class="p-4 sm:p-6">
  {# Detail header — always a card-padded #}
  <div class="card-padded mb-4">
    <!-- entity name, key stats, action buttons -->
  </div>

  {# Tab nav #}
  <div class="flex border-b border-gray-200 mb-4 gap-1">
    <button class="px-4 py-2 text-sm font-medium border-b-2
                   border-brand-500 text-brand-600"
            hx-get="/v2/partials/vendors/{{ id }}/activity"
            hx-target="#tab-content">Activity</button>
    <!-- more tabs -->
  </div>

  <div id="tab-content"><!-- tab partial loads here --></div>
</div>
```

---

## Grid & Spacing

Use `space-y-4` between stacked sections. Use `gap-4` for grid/flex layouts.

```html
<!-- Card grid — vendor/customer list in card mode -->
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
  {% for v in vendors %}
    {% include "htmx/partials/vendors/vendor_card.html" %}
  {% endfor %}
</div>

<!-- Two-column detail layout -->
<div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
  <div class="lg:col-span-2 space-y-4"><!-- main content --></div>
  <div class="space-y-4"><!-- sidebar/stats --></div>
</div>
```

Padding scale: `p-4` (16px) for mobile, `sm:p-6` (24px) for desktop. Never use `p-8` or higher — it eats too much of the data-dense screen space.

---

## Responsive Patterns

The topbar + bottom nav layout means full-width content on all breakpoints. There is no sidebar — navigation is bottom-tab-style. Mobile-first:

```html
<!-- Stack on mobile, side-by-side on sm+ -->
<div class="flex flex-col sm:flex-row sm:items-center gap-3">
  <span class="text-sm text-gray-500">Filter:</span>
  <input class="flex-1 ...">
</div>

<!-- Hide non-critical columns on mobile -->
<th class="hidden sm:table-cell">Location</th>
<td class="hidden sm:table-cell">{{ v.hq_country }}</td>
```

`app/static/htmx_mobile.css` handles mobile-specific overrides. Check it before adding mobile-targeted styles inline.

---

## List + Detail Split

AvailAI doesn't use a split-pane layout. List and detail are full-page HTMX swaps into `#main-content` with `hx-push-url` to update the browser URL.

```html
<!-- In list partial: each row navigates to detail -->
<tr class="cursor-pointer hover:bg-brand-50"
    hx-get="/v2/partials/requisitions/{{ r.id }}"
    hx-target="#main-content"
    hx-push-url="/v2/requisitions/{{ r.id }}">
  <td>{{ r.customer_name }}</td>
</tr>

<!-- Back navigation in detail partial -->
<button hx-get="/v2/partials/requisitions/list"
        hx-target="#main-content"
        hx-push-url="/v2/requisitions"
        class="text-sm text-brand-500 hover:text-brand-700 flex items-center gap-1">
  ← Back to Requisitions
</button>
```

**Never** use `window.history.back()` — HTMX partial swaps don't create real history entries.
