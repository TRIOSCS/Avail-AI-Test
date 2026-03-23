# HTMX Workflows Reference

## Contents
- Adding a New Page
- Inline Editing Workflow
- Modal Workflow
- Bulk Actions with Alpine State
- OOB Multi-Target Updates
- Checklist: New HTMX Page

---

## Adding a New Page

Every new section follows the same three-file pattern. Do not deviate from it.

**Step 1 — FastAPI route** (`app/routers/htmx_views.py`):

```python
# Shell route (browser navigation) + partial route (HTMX swap)
@router.get("/v2/my-section", response_class=HTMLResponse)
@router.get("/v2/my-section/{item_id:int}", response_class=HTMLResponse)
async def my_section_page(
    request: Request,
    item_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    ctx = {"request": request, "user": current_user, **_vite_assets()}
    partial_url = f"/v2/partials/my-section/{item_id}" if item_id else "/v2/partials/my-section"

    if request.headers.get("HX-Request") == "true":
        # HTMX navigation — return partial directly
        items = db.execute(select(MyModel)).scalars().all()
        ctx["items"] = items
        return templates.TemplateResponse("htmx/partials/my_section/list.html", ctx)

    # Direct browser hit — wrap in lazy-loader
    ctx["partial_url"] = partial_url
    return templates.TemplateResponse("htmx/base_page.html", ctx)
```

**Step 2 — Partial template** (`app/templates/htmx/partials/my_section/list.html`):

```html
{# my_section/list.html — list view partial.
   Receives: items, user.
   Called by: my_section_page route.
   Depends on: brand palette.
#}
<div class="max-w-7xl mx-auto">
  <title hx-swap-oob="true">My Section — AvailAI</title>
  {% for item in items %}
    <div>{{ item.name }}</div>
  {% endfor %}
</div>
```

**Step 3 — Nav link** (in `app/templates/base.html` sidebar):

```html
<a hx-get="/v2/partials/my-section"
   hx-target="#main-content"
   hx-push-url="/v2/my-section"
   class="nav-link">
  My Section
</a>
```

---

## Inline Editing Workflow

The pattern is always: display → edit → save/cancel → display. Three endpoints, two templates.

```python
# 1. Load edit form
@router.get("/v2/partials/items/{item_id}/edit/{field}")
async def edit_cell(item_id: int, field: str, ...):
    item = db.get(MyModel, item_id)
    return templates.TemplateResponse("htmx/partials/items/cell_edit.html",
                                      {"request": request, "item": item, "field": field,
                                       "cell_id": f"cell-{item_id}-{field}"})

# 2. Save and return display
@router.patch("/v2/partials/items/{item_id}/cell")
async def save_cell(item_id: int, field: str = Form(...), value: str = Form(...), ...):
    item = db.get(MyModel, item_id)
    setattr(item, field, value)
    db.commit()
    return templates.TemplateResponse("htmx/partials/items/cell_display.html",
                                      {"request": request, "item": item, "field": field,
                                       "cell_id": f"cell-{item_id}-{field}"})

# 3. Cancel — return display without saving (same as #2 but GET)
@router.get("/v2/partials/items/{item_id}/cell/display/{field}")
async def display_cell(item_id: int, field: str, ...):
    item = db.get(MyModel, item_id)
    return templates.TemplateResponse("htmx/partials/items/cell_display.html", ...)
```

`cell_display.html` — always wraps in the container `<td>` with the edit trigger:
```html
<td id="{{ cell_id }}"
    hx-get="/v2/partials/items/{{ item.id }}/cell/edit/{{ field }}"
    hx-target="#{{ cell_id }}"
    hx-swap="outerHTML"
    class="cursor-pointer hover:bg-brand-50">
  {{ item[field] }}
</td>
```

`cell_edit.html` — focused input, submits on blur/Enter, cancels on Escape:
```html
<td id="{{ cell_id }}" @click.stop class="!p-0.5">
  <input type="text" name="value" value="{{ item[field] }}"
         hx-patch="/v2/partials/items/{{ item.id }}/cell"
         hx-target="#{{ cell_id }}"
         hx-swap="outerHTML"
         hx-vals='{"field": "{{ field }}"}'
         hx-trigger="keyup[key=='Enter']"
         x-init="$el.focus(); $el.select()"
         @blur="$el.form ? $el.form.requestSubmit() : htmx.trigger($el, 'keyup', {key: 'Enter'})"
         @keydown.escape.prevent="htmx.ajax('GET', '/v2/partials/items/{{ item.id }}/cell/display/{{ field }}', {target: '#{{ cell_id }}', swap: 'outerHTML'})">
</td>
```

---

## Modal Workflow

The modal shell lives in `base.html`. Partials load content into `#modal-content` and close via `$dispatch('close-modal')`.

**Opening a modal:**
```html
{# Dispatch event to show modal shell + load content simultaneously #}
<button @click="$dispatch('open-modal')"
        hx-get="/v2/partials/my-section/create-form"
        hx-target="#modal-content"
        data-loading-disable>
  New Item
</button>
```

**Modal content partial:**
```html
{# create_form.html #}
<div class="p-6">
  <div class="flex items-center justify-between mb-6">
    <h2 class="text-lg font-semibold">New Item</h2>
    <button type="button" @click="$dispatch('close-modal')">✕</button>
  </div>

  <form hx-post="/api/items"
        hx-target="#items-list"
        hx-swap="afterbegin"
        hx-ext="json-enc"
        @htmx:after-request.camel="if(event.detail.successful) {
          $dispatch('close-modal');
          Alpine.store('toast').message = 'Item created';
          Alpine.store('toast').show = true;
        }">
    <input type="text" name="name" required>
    <button type="submit">Create</button>
  </form>
</div>
```

**NEVER** close the modal before checking `event.detail.successful`. A failed POST should keep the form open so the user can correct errors.

---

## Bulk Actions with Alpine State

Use Alpine `x-data` on the list container to track selected IDs, then pass them to HTMX via a hidden form.

```html
<div x-data="{ selectedIds: new Set() }">
  {# Select-all checkbox #}
  <input type="checkbox"
         @change="if($event.target.checked) {
           document.querySelectorAll('#list-body input[type=checkbox]').forEach(c => {
             c.checked = true; selectedIds.add(parseInt(c.value))
           })
         } else {
           selectedIds = new Set();
           document.querySelectorAll('#list-body input[type=checkbox]').forEach(c => c.checked = false)
         }; selectedIds = new Set(selectedIds)">

  {# Bulk action bar — only visible when items selected #}
  <div x-show="selectedIds.size > 0" x-cloak x-collapse>
    <form id="bulk-form">
      <input type="hidden" name="ids" :value="Array.from(selectedIds).join(',')">
    </form>
    <button hx-post="/v2/partials/items/bulk/archive"
            hx-target="#main-content"
            hx-include="#bulk-form"
            hx-confirm="Archive selected items?">
      Archive
    </button>
  </div>

  {# Per-row checkbox #}
  <input type="checkbox" :value="item.id"
         @change="$event.target.checked ? selectedIds.add(item.id) : selectedIds.delete(item.id); selectedIds = new Set(selectedIds)">
</div>
```

`selectedIds = new Set(selectedIds)` at the end of each mutation forces Alpine reactivity — Sets are not inherently reactive.

---

## OOB Multi-Target Updates

When a single action needs to update multiple page regions, use `hx-swap-oob` in the response. The primary swap target gets the main content; OOB elements update by ID.

```python
# FastAPI: return a response that updates multiple targets
@router.post("/v2/partials/requisitions/{req_id}/send-rfq")
async def send_rfq(req_id: int, ...):
    # ... send RFQ logic ...
    # Return partial with OOB badge update
    return templates.TemplateResponse("htmx/partials/requisitions/rfq_sent.html",
                                      {"request": request, "req": req})
```

```html
{# rfq_sent.html — primary content + OOB updates #}

{# OOB: update status badge in the header without reloading it #}
<span id="req-status-badge" hx-swap-oob="true"
      class="px-2 py-0.5 text-xs rounded-full bg-amber-100 text-amber-700">
  RFQ Sent
</span>

{# OOB: update title #}
<title hx-swap-oob="true">{{ req.name }} — RFQ Sent — AvailAI</title>

{# Primary content: confirmation message swapped into hx-target #}
<div class="p-4 bg-emerald-50 rounded-lg border border-emerald-200">
  RFQ sent to {{ vendor_count }} vendors.
</div>
```

---

## Checklist: New HTMX Page

Copy this checklist when adding a new section:

- [ ] Shell route: `@router.get("/v2/my-section")` returns `base_page.html` with `partial_url`
- [ ] Partial route: `@router.get("/v2/partials/my-section")` returns the list partial
- [ ] Check `HX-Request` header in shell route to handle direct HTMX navigation
- [ ] Template has header comment: what it receives, what calls it, what it depends on
- [ ] Navigation link uses `hx-push-url` with the canonical `/v2/` URL
- [ ] Partial updates `<title hx-swap-oob="true">` for correct browser tab title
- [ ] Error handling: `hx-target-error` on forms pointing to a visible error div
- [ ] Loading state: `hx-indicator` or `data-loading-disable` on submit buttons
- [ ] Filter forms: `hx-include="#filter-form-id"` on all filter inputs
- [ ] Tests: add `test_routers.py` coverage for the new routes
```

---

The skill files are ready. To write them to disk, you'll need to grant write permissions to `.claude/skills/htmx/`. The files cover:

**SKILL.md** — core page/partial pattern, 5 common patterns (lazy load, live search, tab nav, inline editing, modals), key attribute reference table

**references/patterns.md** — swap strategies, loading states extension, error handling with `hx-target-error`, filter+pagination with `hx-include`, JSON POST with `hx-ext="json-enc"`, and 3 anti-patterns with full problem/fix documentation

**references/workflows.md** — step-by-step guides for adding new pages, inline editing (3-endpoint pattern), modal workflow, bulk Alpine state, OOB multi-target updates, and a copy-paste completion checklist
