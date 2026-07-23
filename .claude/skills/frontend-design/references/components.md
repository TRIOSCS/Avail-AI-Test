# Components Reference

## Contents
- Page Header
- Buttons
- Badges
- Cards
- Tables
- Forms
- Score Bars
- Tags

---

## Page Header

The canonical page-header pattern for every list/detail partial. Defined once in
`app/templates/htmx/partials/shared/_macros.html` — do not hand-roll the
title/subtitle/actions row inline; import and call the macro.

```jinja
{% from "htmx/partials/shared/_macros.html" import page_header %}

{% call page_header("Vendors", subtitle=total ~ " suppliers") %}
  <button class="btn-primary btn-sm">Add Vendor</button>
{% endcall %}
```

Renders a `flex items-center justify-between` row: `.h1` title (+ optional
`.text-secondary` subtitle) on the left, and any content passed via the `{% call %}`
block rendered as action buttons on the **right**. Canonical CTA placement is always
right-aligned — do not move page-level actions to the left or center. `subtitle` is
optional (`None` omits the `<p>` entirely); pass a plain string, not markup.

---

## Buttons

All defined as `@layer components` in `app/static/styles.css`. Never use raw Tailwind
for buttons — always use the component class.

```html
<!-- Primary action — background is var(--accent), the Trio azure accent, not brand-* -->
<button class="btn-primary">Send RFQ</button>

<!-- Secondary / outline -->
<button class="btn-secondary">Cancel</button>

<!-- Destructive -->
<button class="btn-danger">Delete</button>

<!-- Ghost -->
<button class="btn-ghost">Dismiss</button>

<!-- Small variant — combine with size modifier -->
<button class="btn-primary btn-sm">Add</button>
<button class="btn-secondary btn-sm">View</button>
```

`.btn-primary`, `.btn-secondary`, and `.btn-ghost` all set `--tw-ring-color:
var(--accent-ring)` for their focus ring — the accent azure, never `ring-brand-*`.

HTMX buttons: always add `hx-disabled-elt="this"` on forms to prevent double-submit.

```html
<button class="btn-primary" hx-post="/v2/partials/rfq/send"
        hx-target="#rfq-result" hx-disabled-elt="this">
  Send RFQ
</button>
```

---

## Badges

Status badges are `rounded-full` pills (`.badge`). Rectangular metadata tags use
`.chip` (`rounded`, not `rounded-full`). Use semantic color, not arbitrary colors.

```html
<span class="badge-success">Active</span>       <!-- emerald -->
<span class="badge-warning">Pending Review</span> <!-- amber -->
<span class="badge-danger">Rejected</span>       <!-- rose -->
<span class="badge-info">Sourcing</span>         <!-- neutral brand-100/700 gray, NOT accent -->
<span class="badge-neutral">Draft</span>         <!-- gray -->
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

`.card`, `.card-sm`, and `.card-lg` are the only card component classes — there is no
`.card-padded`. All three use a 1px `border-brand-200` (not `border-2`).

```html
<!-- Static card -->
<div class="card">
  <h2 class="h3 mb-3">Section Title</h2>
  <!-- content -->
</div>

<!-- Denser variant (p-2.5, no shadow) -->
<div class="card-sm">...</div>

<!-- Roomier variant (p-5, shadow-card) -->
<div class="card-lg">...</div>

<!-- Interactive card (navigates on click) — accent on hover -->
<div class="bg-white rounded-xl border border-brand-200
            hover:border-accent-300 hover:shadow-float transition-all cursor-pointer p-3.5"
     hx-get="/v2/partials/vendors/{{ v.id }}"
     hx-target="#main-content"
     hx-push-url="/v2/vendors/{{ v.id }}">
  <!-- content -->
</div>
```

**WARNING:** Never put `hx-get` on a `<a>` tag that also has `href` — HTMX and browser
both fire, causing double navigation. Use `<div>` with `cursor-pointer` for card-level
clicks.

---

## Tables

Always wrap in `.table-wrapper`. Use `.responsive-table` or `.data-table` class for
consistent row styling.

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

Table headers are styled globally: 11px uppercase, `color: #6B7280` (gray, not
`text-brand-500`), `background: #fff` (plain white, not `bg-brand-50`), `border-bottom:
2px solid #BFC4CE` (the `--line` hairline). There is no tinted brand header fill.

---

## Forms

```html
<!-- Standard input — use the .input component class -->
<input type="text" name="mpn" class="input" placeholder="Enter MPN">

<!-- Small variant -->
<input type="text" name="qty" class="input input-sm">
```

`.input` focus state uses the accent ring (`--accent-ring`) and accent border on
`:focus`, not brand:

```css
.input { @apply ... focus:outline-none focus:ring-2 ...; --tw-ring-color: var(--accent-ring); }
.input:focus { border-color: var(--accent); }
```

Native controls that can't take the full `.input` class (checkboxes, `<select>`) use
the `.input-focus` mixin for the same accent-ring behavior. There is no `.filter-bar`
component class — a filter row is a plain flex container of `.input`-classed controls:

```html
<div class="flex items-center gap-3">
  <input type="search" name="q" placeholder="Search..." class="input flex-1">
  <select name="status" class="input">
    <option value="">All Statuses</option>
  </select>
</div>
```

Do not write `focus:border-brand-500 focus:ring-brand-500` on new inputs — that's the
legacy pattern several older templates still carry; new/edited forms should use `.input`
or `.input-focus` so the accent ring stays consistent.

---

## Score Bars

Used on vendor cards, confidence meters, and match scores. Three-tier coloring at
66/33.

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

The mid-tier `brand-600`/`brand-500` here is neutral gray (matching the real palette),
not a blue accent — this is a neutral "middling score" read, distinct from the
interactive `accent-*` used on buttons/links.

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
