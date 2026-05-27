# Unified Requisition Entry Form — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two separate import/create modals with a single unified form featuring inline AI parse, column-visibility table, and manufacturer typeahead on every row.

**Architecture:** Single Alpine.js component manages all state (parts array, parse status, column visibility). AI parse returns JSON, Alpine builds rows. Form submission uses hidden inputs synced from Alpine state to preserve the existing `import-save` backend contract. Shared components extracted to eliminate duplication.

**Tech Stack:** Jinja2, HTMX, Alpine.js, Tailwind CSS, FastAPI

**Spec:** `docs/superpowers/specs/2026-03-22-unified-req-form-design.md`

---

## Task Overview

| Task | Description | Dependencies |
|------|-------------|--------------|
| 1 | Extract shared components (customerPicker, deadline macro) | None |
| 2 | Update import-parse route to return JSON | None |
| 3 | Build unified modal template | Tasks 1, 2 |
| 4 | Wire up routes + deprecate old modals | Task 3 |
| 5 | Per-row validation + error handling | Task 3 |
| 6 | Integration testing + cleanup | Tasks 4, 5 |

---

### Task 1: Extract Shared Components

**Files:**
- Create: `app/static/js/customer_picker.js` or add to `app/static/htmx_app.js`
- Modify: `app/templates/htmx/partials/shared/_macros.html` — add deadline_asap_widget macro
- Modify: `app/templates/htmx/partials/requisitions/import_modal.html` — verify customerPicker works from shared location (temporary, will be deleted later)

- [ ] **Step 1: Extract `customerPicker()` to `htmx_app.js`**

Read `app/templates/htmx/partials/requisitions/import_modal.html` lines 161-229 to get the current `customerPicker()` function. Move it to `app/static/htmx_app.js` (or a new file included in the base template). The function should be globally available so any template can use `x-data="customerPicker()"`.

- [ ] **Step 2: Add `deadline_asap_widget` macro to `_macros.html`**

Read the deadline/ASAP widget from `import_modal.html` lines 98-111. Create a Jinja2 macro:

```jinja2
{% macro deadline_asap_widget(deadline_value='', urgency_value='normal') %}
<div x-data="{ asap: {{ 'true' if urgency_value == 'critical' else 'false' }} }" class="flex items-center gap-2">
  <input type="date" name="deadline" :disabled="asap"
         value="{{ deadline_value }}"
         class="px-2 py-1 text-xs border border-gray-300 rounded ...">
  <input type="hidden" name="urgency" :value="asap ? 'critical' : 'normal'">
  <button type="button" @click="asap = !asap"
          :class="asap ? 'bg-amber-500 text-white' : 'bg-gray-100 text-gray-600'"
          class="px-2 py-1 text-[10px] font-semibold rounded transition-colors">
    ASAP
  </button>
</div>
{% endmacro %}
```

- [ ] **Step 3: Test that existing import modal still works**

Verify `import_modal.html` loads correctly with the extracted customerPicker. Temporary validation — this modal will be replaced in Task 3.

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor: extract customerPicker and deadline widget to shared components

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Update import-parse Route to Return JSON

**Files:**
- Modify: `app/routers/htmx_views.py` — `requisition_import_parse` route (~line 608)

- [ ] **Step 1: Read current route**

Read `app/routers/htmx_views.py` lines 608-680 to understand the current import-parse flow.

- [ ] **Step 2: Add JSON response path**

Add an `Accept` header check or a query param (`?format=json`) so the route can return either HTML (backward compat) or JSON:

```python
# At the end of the route, before the template response:
accept = request.headers.get("accept", "")
if "application/json" in accept or request.query_params.get("format") == "json":
    return JSONResponse({
        "requirements": requirements,
        "inferred_name": name,
        "inferred_customer": customer_name,
    })

# Existing HTML response for backward compat (until old modal is removed)
return templates.TemplateResponse("htmx/partials/requisitions/import_preview.html", ctx)
```

- [ ] **Step 3: Write test**

```python
def test_import_parse_json_response(client, db_session, test_user):
    resp = client.post("/v2/partials/requisitions/import-parse",
        data={"raw_text": "LM317T 500 Texas Instruments", "name": "Test"},
        headers={"Accept": "application/json"})
    assert resp.status_code == 200
    data = resp.json()
    assert "requirements" in data
```

- [ ] **Step 4: Run test + commit**

```bash
git commit -m "feat: add JSON response mode to import-parse route

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Build Unified Modal Template

**Files:**
- Create: `app/templates/htmx/partials/requisitions/unified_modal.html`

This is the main implementation task. The template contains:

- [ ] **Step 1: Build the Alpine.js root component**

Define `unifiedReqModal()` function with all state and methods:
- `inputMode`, `rawText`, `parsing`, `parsed`, `parseError`
- `parts[]` array of part objects
- `showAllColumns` toggle
- `reqName`, `customerSiteId`, `customerName`, `deadline`, `urgency`
- `parseWithAI()` method using fetch + FormData → JSON
- `addBlankPart()`, `removePart(i)`, `resetParse()`
- Computed: `validParts`, `hasErrors`, `errorCount`

- [ ] **Step 2: Build the metadata zone**

Top row: Name input (required) + customer picker (using extracted `customerPicker()`) + deadline macro.

- [ ] **Step 3: Build the input zone**

Tab toggle (Paste / Upload). Paste shows 3-row textarea bound to `rawText`. Upload shows file input. "Process with AI" button calls `parseWithAI()`.

Post-parse: textarea replaced with status strip showing count + re-parse link.

- [ ] **Step 4: Build the parts table with Tier 1 columns**

Table with `x-for="(part, i) in parts"` rendering rows. Tier 1 columns: MPN (text input), Manufacturer (text input + typeahead), Qty (number), Condition (select with NEW/REFURB/USED), Target $ (number), delete button.

"+ Add row" button below table calls `addBlankPart()`.

Column visibility toggle button above table: "Show all columns ▾" / "Fewer columns ▴".

- [ ] **Step 5: Build Tier 2 columns**

Conditionally rendered with `x-show="showAllColumns"`: Brand, Customer PN, Date Codes, Packaging (select), Firmware, Hardware, Need By (date), Sales Notes.

Substitutes: when Tier 2 visible, each row shows a compact subs area with structured sub-row inputs (`sub_mpn` + `sub_manufacturer` per sub).

- [ ] **Step 6: Build the footer**

Cancel button + Create Requisition button with part count and validation state. Hidden inputs synced from `parts[]` for form submission to `import-save`.

- [ ] **Step 7: Wire manufacturer typeahead on table rows**

Each manufacturer input in the table uses HTMX typeahead to `/v2/partials/manufacturers/search`. Dropdown positioned below the input. Clicking a result fills the input value.

- [ ] **Step 8: Verify template renders**

Build the app, open the modal, verify all zones render correctly.

- [ ] **Step 9: Commit**

```bash
git commit -m "feat: build unified requisition entry modal with column-visibility table

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire Up Routes + Deprecate Old Modals

**Files:**
- Modify: `app/routers/htmx_views.py` — update `import-form` route, deprecate `create-form`
- Modify: `app/templates/htmx/partials/parts/workspace.html` or wherever the "+ Req" button lives — point to unified modal

- [ ] **Step 1: Update `import-form` GET route to return unified modal**

Change the `requisition_import_form` route to render `unified_modal.html` instead of `import_modal.html`.

- [ ] **Step 2: Update `create-form` GET route to redirect**

Either make `create-form` also return the unified modal, or remove it and update any buttons that pointed to it.

- [ ] **Step 3: Update the "+ Req" button**

The button in `list.html` (line ~24) fires `hx-get="/v2/partials/requisitions/import-form"`. Verify it now opens the unified modal.

- [ ] **Step 4: Test the full flow end-to-end**

1. Click "+ Req" → unified modal opens
2. Fill name, paste parts, click "Process with AI" → rows appear
3. Edit rows, add manual rows
4. Toggle "Show all columns" → Tier 2 appears
5. Click "Create Requisition" → saves, toast, modal closes

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: wire unified modal to routes, deprecate old create form

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Per-Row Validation + Error Handling

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/unified_modal.html`

- [ ] **Step 1: Add per-row validation styling**

Rows where MPN is filled but manufacturer is empty get:
- Red left border (`border-l-2 border-red-400`)
- Red border on manufacturer input
- Small `!` icon

Validation runs on blur from manufacturer input + on submit attempt.

- [ ] **Step 2: Add submit button validation state**

Button shows:
- Green state: `"Create Requisition (12 parts)"`
- Amber state: `"Create Requisition (10 OK · 2 need manufacturer)"`
- Disabled when any row has errors

- [ ] **Step 3: Add auto-scroll to first error on submit**

When user clicks Create with validation errors, scroll to the first invalid row and focus the manufacturer input.

- [ ] **Step 4: Add parse error handling**

Show `parseError` inline below the paste area (not a toast). Red text, subtle background. Paste content preserved.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: add per-row validation and error handling to unified modal

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Integration Testing + Cleanup

**Files:**
- Delete: `app/templates/htmx/partials/requisitions/import_modal.html`
- Delete: `app/templates/htmx/partials/requisitions/import_preview.html`
- Delete: `app/templates/htmx/partials/requisitions/create_modal.html`
- Create: `tests/test_unified_req_form.py`

- [ ] **Step 1: Write integration tests**

```python
def test_unified_modal_renders(client, db_session, test_user):
    resp = client.get("/v2/partials/requisitions/import-form")
    assert resp.status_code == 200
    assert "unifiedReqModal" in resp.text

def test_import_parse_returns_json(client, db_session, test_user):
    resp = client.post("/v2/partials/requisitions/import-parse",
        data={"raw_text": "LM317T 500", "name": "Test"},
        headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert "requirements" in resp.json()

def test_import_save_still_works(client, db_session, test_user):
    # Submit with indexed form fields matching the hidden-input pattern
    resp = client.post("/v2/partials/requisitions/import-save", data={
        "name": "Test Req",
        "customer_name": "",
        "reqs[0].primary_mpn": "LM317T",
        "reqs[0].manufacturer": "Texas Instruments",
        "reqs[0].target_qty": "500",
    })
    assert resp.status_code == 200
```

- [ ] **Step 2: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q
```

- [ ] **Step 3: Delete old templates**

Remove `import_modal.html`, `import_preview.html`, `create_modal.html`. Grep for any remaining references to these filenames and update.

- [ ] **Step 4: Remove old inline `customerPicker()` scripts**

Verify no template still defines `customerPicker()` inline. The shared version in `htmx_app.js` is the single source.

- [ ] **Step 5: Deploy and verify**

```bash
git push origin main && docker compose up -d --build && docker compose logs -f app
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat: complete unified req form, delete old modal templates

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
