# Components Reference

## Contents
- Buttons
- Badges
- Cards
- Tables
- Forms
- Score Bars
- Tags

---

## Buttons

All defined as `@layer components` in `app/static/styles.css`. Never use raw Tailwind for buttons — always use the component class.

```html
<!-- Primary action -->
<button class="btn-primary">Send RFQ</button>

<!-- Secondary / outline -->
<button class="btn-secondary">Cancel</button>

<!-- Destructive -->
<button class="btn-danger">Delete</button>

<!-- Small variant — combine with size modifier -->
<button class="btn-primary btn-sm">Add</button>
<button class="btn-secondary btn-sm">View</button>
```

HTMX buttons: always add `hx-disabled-elt="this"` on forms to prevent double-submit.

```html
<button class="btn-primary" hx-post="/v2/partials/rfq/send"
        hx-target="#rfq-result" hx-disabled-elt="this">
  Send RFQ
</button>
```

---

## Badges

Status badges are `rounded-full` pills. Use semantic color, not arbitrary colors.

```html
<span class="badge-success">Active</span>      <!-- emerald -->
<span class="badge-warning">Pending Review</span> <!-- amber -->
<span class="badge-danger">Rejected</span>     <!-- rose -->
<span class="badge-info">Sourcing</span>        <!-- brand-blue -->
<span class="badge-neutral">Draft</span>        <!-- gray -->
```

For inline status that maps from a dict (avoids long if/elif chains):

```html
{% set status_colors = {
  'active': 'bg-emerald-50 text-emerald-700',
  'pending_review': 'bg-amber-50 text-amber-700',
  'rejected': 'bg-rose-50 text-rose-700',
  'sold': 'bg-gray-100 text-gray-600'
} %}
<span class="inline-flex px-1.5 py-0.5 text-xs font-medium rounded-full
             {{ status_colors.get(item.status, 'bg-gray-100 text-gray-600') }}">
  {{ item.status|replace('_', ' ')|capitalize }}
</span>
```

---

## Cards

```html
<!-- Static card -->
<div class="card-padded">
  <h2 class="text-sm font-semibold text-gray-900 mb-3">Section Title</h2>
  <!-- content -->
</div>

<!-- Interactive card (navigates on click) -->
<div class="bg-white rounded-lg border border-brand-200
            hover:border-brand-400 hover:shadow-md transition-all cursor-pointer p-4"
     hx-get="/v2/partials/vendors/{{ v.id }}"
     hx-target="#main-content"
     hx-push-url="/v2/vendors/{{ v.id }}">
  <!-- content -->
</div>
```

**WARNING:** Never put `hx-get` on a `<a>` tag that also has `href` — HTMX and browser both fire, causing double navigation. Use `<div>` with `cursor-pointer` for card-level clicks.

---

## Tables

Always wrap in `table-wrapper`. Use `responsive-table` or `data-table` class for consistent row styling.

```html
<div class="table-wrapper">
  <table class="responsive-table w-full">
    <thead>
      <tr>
        <th>MPN</th>
        <th>Vendor</th>
        <th class="text-right">Unit Price</th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr>
        <td class="font-data">{{ row.mpn }}</td>
        <td>{{ row.vendor_name }}</td>
        <td class="font-data text-right">${{ '%.4f'|format(row.unit_price|float) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
```

Table headers are styled globally: 11px, uppercase, `text-brand-500`, `bg-brand-50`, `border-b-2 border-brand-200`.

---

## Forms

```html
<!-- Standard input -->
<input type="text" name="mpn"
       class="w-full rounded-lg border border-brand-300 bg-white px-3 py-1.5
              text-sm text-gray-800 placeholder-brand-300 outline-none
              focus:border-brand-500 focus:ring-1 focus:ring-brand-500">

<!-- Filter bar — uses filter-bar component class -->
<div class="filter-bar flex items-center gap-3">
  <input type="search" name="q" placeholder="Search..."
         class="flex-1 rounded-lg border border-brand-300 px-3 py-1.5 text-sm
                focus:border-brand-500 focus:outline-none">
  <select name="status" class="rounded-lg border border-brand-300 px-3 py-1.5 text-sm
                               focus:border-brand-500 focus:outline-none">
    <option value="">All Statuses</option>
  </select>
</div>
```

---

## Score Bars

Used on vendor cards, confidence meters, and match scores. Three-tier coloring at 66/33.

```html
<div>
  <div class="flex items-center justify-between mb-1">
    <span class="text-xs text-gray-500">Score</span>
    <span class="text-sm font-semibold
      {% if score >= 66 %}text-emerald-700
      {% elif score >= 33 %}text-brand-600
      {% else %}text-amber-700{% endif %}">{{ score|round|int }}</span>
  </div>
  <div class="w-full bg-gray-200 rounded-full h-1.5">
    <div class="h-1.5 rounded-full transition-all
      {% if score >= 66 %}bg-emerald-500
      {% elif score >= 33 %}bg-brand-500
      {% else %}bg-amber-500{% endif %}"
         style="width: {{ [score, 100]|min }}%"></div>
  </div>
</div>
```

---

## Tags

Two semantic tag types used across the app:

```html
<!-- Brand/manufacturer tags — violet -->
<span class="px-1.5 py-0.5 text-[10px] font-medium rounded bg-violet-50 text-violet-600">
  Texas Instruments
</span>

<!-- Commodity tags — sky -->
<span class="px-1.5 py-0.5 text-[10px] font-medium rounded bg-sky-50 text-sky-600">
  Microcontrollers
</span>

<!-- Overflow indicator -->
<span class="px-1.5 py-0.5 text-[10px] text-gray-400">+3 more</span>
```

Cap tag display at 5 brand tags and 4 commodity tags — show "+N more" for overflow.
