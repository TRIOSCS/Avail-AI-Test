# REQ Page: Substitute Parts Visibility + Tech Debt Polish

**Date:** 2026-03-21
**Status:** Approved

## Context

Substitute parts are alternative MPNs that can fill the same requirement. A customer needing 500 units will accept any mix of the primary MPN + substitutes. Subs are equal to primary parts from a sourcing and quoting perspective.

Currently, substitutes are stored as a JSON array of MPN strings on each `Requirement` record (`substitutes` column). They are not separate Requirement records. The left panel has a "Subs" column but it is not in the default column set, making subs effectively invisible unless users manually enable it via column picker.

## Scope

1. **Always-expanded substitute rows in left panel** (primary feature)
2. **Remove redundant "Subs" column** from left panel column definitions
3. **Archive guard on Requisition Info edits** (tech debt)
4. **Sibling rows clickable** in REQ Detail tab (tech debt)
5. Escape key optimization deferred (works correctly, low impact)

## Design

### 1. Substitute Rows in Left Panel

**Location:** `app/templates/htmx/partials/parts/list.html`

After each primary requirement row in the `<tbody>`, render sub-rows for each MPN in `req.substitutes`:

```
┌────┬───────────────┬───────┬────────┬─────┬───────┐
│ ☐  │ LM317T        │ TI    │ Open   │ 500 │ ...   │  ← primary row
│    │  │ SUB LM338T  │       │        │     │       │  ← sub row
│    │  │ SUB SG3525  │       │        │     │       │  ← sub row
│ ☐  │ AD8232ACPZ    │ ADI   │ Src    │ 200 │ ...   │  ← next primary
└────┴───────────────┴───────┴────────┴─────┴───────┘
```

**Visual treatment:**
- Sub rows span the full table width via `colspan` on a single `<td>` (after the checkbox column)
- No checkbox — subs share the parent requirement's lifecycle
- Thin vertical connector line from primary row through subs (tree-view style)
- MPN displayed as amber chip matching header styling: `bg-amber-50 text-amber-700 border border-amber-200 font-mono text-[11px]`
- "SUB" label badge: small, muted amber, to the left of the MPN chip
- Background: `bg-amber-50/30` to subtly differentiate from primary rows
- Hover tooltip on each sub row: "Sources as alternative for {primary_mpn}"
- **Click behavior:** Clicking a sub row selects the parent requirement (`selectPart(req.id)`) and loads its detail tabs. Same behavior as clicking the primary row. The sub rows exist for visibility, not separate navigation.

**Template structure (inside the `{% for req in requirements %}` loop):**

```html
{# Primary row — existing code, unchanged #}
<tr data-part-id="{{ req.id }}" ...>
  ...existing columns...
</tr>
{# Sub rows — new, rendered immediately after primary #}
{% if req.substitutes %}
  {% for sub_mpn in req.substitutes %}
  <tr class="bg-amber-50/30 hover:bg-amber-100/40 cursor-pointer"
      title="Sources as alternative for {{ req.primary_mpn }}"
      @click="selectPart({{ req.id }}); htmx.ajax('GET', '/v2/partials/parts/{{ req.id }}/tab/' + activeTab, {target: '#part-detail'})">
    <td></td>{# empty checkbox cell #}
    <td colspan="{{ col_defs|length }}" class="pl-6">
      <div class="flex items-center gap-1.5">
        <span class="border-l-2 border-amber-300 h-4 -ml-2"></span>
        <span class="text-[9px] font-semibold text-amber-500 uppercase tracking-wider">Sub</span>
        <span class="inline-flex px-1.5 py-0.5 text-[11px] font-mono font-medium rounded bg-amber-50 text-amber-700 border border-amber-200">{{ sub_mpn }}</span>
      </div>
    </td>
  </tr>
  {% endfor %}
{% endif %}
```

### 2. Remove "Subs" Column

**Location:** `app/templates/htmx/partials/parts/list.html`

Remove `'substitutes': ('Subs', None)` from the `col_defs` dictionary in the template. The sub-rows make this column redundant.

Also remove the corresponding display logic in the `{% elif col_key == 'substitutes' %}` block.

### 3. Archive Guard on All Editable Fields

**Location:** `app/templates/htmx/partials/parts/tabs/req_details.html`

Two independent archive conditions — either blocks edits:

```html
{% set req_archived = req.status == 'archived' %}
{% set part_archived = requirement.sourcing_status == 'archived' %}
{% set is_archived = req_archived or part_archived %}
```

- `req_archived`: Requisition-level archive — blocks Requisition Info edits (name, status, urgency, deadline, owner)
- `part_archived`: Requirement-level archive — blocks Part Specification edits (customer_pn, condition, date_codes, packaging, firmware, hardware_codes)
- `is_archived`: Either condition — used as a single guard for all editable fields in the tab

For each field, conditionally render either the click-to-edit div (when not archived) or a read-only span (when archived):

```html
{% if not is_archived %}
  <div id="reqd-name" class="cursor-pointer hover:bg-brand-50 ..." hx-get="...">
    ...
  </div>
{% else %}
  <div class="px-1 py-0.5">
    <span class="text-gray-900">{{ req.name or '—' }}</span>
  </div>
{% endif %}
```

Note: The backend routes already have archive guards for spec fields (returns 403), but the template should also hide the edit affordance to prevent confusing 403 errors when users click.

### 4. Sibling Rows Clickable

**Location:** `app/templates/htmx/partials/parts/tabs/req_details.html`

Add click handler to each sibling row (except the current requirement) so clicking switches selection and loads that part's detail:

```html
<tr class="{{ 'bg-brand-50' if part.id == requirement.id else 'cursor-pointer hover:bg-gray-50' }}"
    {% if part.id != requirement.id %}
    @click="selectPart({{ part.id }}); htmx.ajax('GET', '/v2/partials/parts/{{ part.id }}/tab/' + activeTab, {target: '#part-detail'})"
    {% endif %}>
```

The current part row stays highlighted with `bg-brand-50` and is not clickable (already selected).

## What This Does NOT Change

- **Data model:** Substitutes remain a JSON array on Requirement. No new models or FK relationships.
- **Backend routes:** No new endpoints needed. All changes are template-only.
- **Offer matching:** Existing search uses `substitutes_text` for matching. No changes here.
- **Sibling table in REQ Detail:** Subs column stays as count-only display. No sub-rows in sibling table.
- **Part header:** Already shows subs as amber chips with click-to-edit. No changes.

## Testing

- Verify sub-rows render for requirements with substitutes
- Verify no sub-rows for requirements without substitutes
- Verify clicking sub row selects the parent requirement
- Verify "Subs" column no longer appears in left panel
- Verify archived requisitions block edits on all Requisition Info fields
- Verify sibling row click navigates to that part's detail
- Verify current part row in sibling table is not clickable

## Files Changed

| File | Change |
|------|--------|
| `app/templates/htmx/partials/parts/list.html` | Add sub-rows after primary rows, remove Subs column |
| `app/templates/htmx/partials/parts/tabs/req_details.html` | Archive guard on Req Info, clickable sibling rows |
