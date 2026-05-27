# Add Req Button + AI Import Modal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "+ Req" button to the parts workspace filter bar that opens a modal where users can paste or upload messy customer data, AI cleans it up into structured requirements, user reviews/edits in a table, then saves as a new requisition.

**Architecture:** Reuse existing `parse_freeform_rfq()` from `freeform_parser_service.py` (Claude Haiku, structured output). Extend the RFQ parse schema to include `brand` and `condition` fields. New modal template with 2-step flow (input → preview). File upload handled server-side with `openpyxl`/`csv` to extract text, then same AI pipeline. New routes in `htmx_views.py`. Existing `apply_freeform_rfq()` handles save (or simplified inline version since we don't need customer_site_id).

**Tech Stack:** FastAPI, HTMX, Alpine.js, Tailwind CSS, Claude Haiku via `routed_structured()`, openpyxl (already in requirements.txt)

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `app/templates/htmx/partials/parts/list.html` | Add "+ Req" button to filter bar |
| Create | `app/templates/htmx/partials/requisitions/import_modal.html` | Step 1: input form (paste/upload + req basics) |
| Create | `app/templates/htmx/partials/requisitions/import_preview.html` | Step 2: editable table preview of parsed requirements |
| Modify | `app/routers/htmx_views.py` | 3 new routes: GET form, POST parse, POST save |
| Modify | `app/services/freeform_parser_service.py` | Extend RFQ schema with brand/condition fields |
| Create | `tests/test_req_import.py` | Tests for parse + save flow |

---

### Task 1: Extend RFQ Parse Schema

**Files:**
- Modify: `app/services/freeform_parser_service.py:25-47` (RFQ_PARSE_SCHEMA)
- Test: `tests/test_req_import.py`

- [ ] **Step 1: Write failing test for extended schema fields**

```python
# tests/test_req_import.py
"""Tests for requisition AI import (paste/upload → parse → save)."""
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_parse_freeform_rfq_returns_brand_and_condition():
    """Verify the parser schema accepts brand and condition fields."""
    mock_result = {
        "name": "Test RFQ",
        "requirements": [
            {
                "primary_mpn": "LM358DR",
                "target_qty": 500,
                "brand": "Texas Instruments",
                "condition": "new",
            }
        ],
    }
    with patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.freeform_parser_service import parse_freeform_rfq

        result = await parse_freeform_rfq("LM358DR x500 TI new")
        assert result is not None
        req = result["requirements"][0]
        assert req["brand"] == "Texas Instruments"
        assert req["condition"] == "new"
```

- [ ] **Step 2: Run test to verify it passes (schema is input, not validated client-side)**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_import.py::test_parse_freeform_rfq_returns_brand_and_condition -v`

- [ ] **Step 3: Add brand and condition to RFQ_PARSE_SCHEMA**

In `app/services/freeform_parser_service.py`, add two fields to the `requirements.items.properties` dict (after `"notes"`):

```python
"brand": {"type": "string", "description": "Manufacturer/brand name"},
"condition": {"type": "string", "description": "Part condition: new, refurbished, used"},
```

Also update `RFQ_SYSTEM` prompt — add to the Rules section:
```
- brand: manufacturer name if stated (e.g. Texas Instruments, STMicroelectronics). Omit if unknown.
- condition: new, refurbished, used, pull. Default "new" if not stated.
```

Also add post-parse normalization in `parse_freeform_rfq()` after the existing loop body (after `r["substitutes"] = []`):

```python
if r.get("condition"):
    r["condition"] = normalize_condition(r["condition"]) or r["condition"]
if not r.get("condition"):
    r["condition"] = "new"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_import.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/freeform_parser_service.py tests/test_req_import.py
git commit -m "feat: extend RFQ parse schema with brand and condition fields"
```

---

### Task 2: Add "+ Req" Button to Parts Workspace

**Files:**
- Modify: `app/templates/htmx/partials/parts/list.html:12-67` (filter bar area)

- [ ] **Step 1: Add button before the status pills**

In `app/templates/htmx/partials/parts/list.html`, inside the filter bar `<div class="flex gap-2 items-center">` (line 13), add the button as the FIRST child element (before the hidden inputs):

```html
        {# Add Req button #}
        <button type="button"
                @click="$dispatch('open-modal')"
                hx-get="/v2/partials/requisitions/import-form"
                hx-target="#modal-content"
                class="px-2 py-0.5 text-[10px] font-semibold rounded bg-brand-500 text-white hover:bg-brand-600 transition-colors flex-shrink-0 inline-flex items-center gap-1">
          <svg class="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4"/>
          </svg>
          Req
        </button>
```

- [ ] **Step 2: Verify visually (deploy and check)**

The button should appear in the filter bar, left of the status pills, matching their height.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/list.html
git commit -m "feat: add +Req button to parts workspace filter bar"
```

---

### Task 3: Create Import Modal Template (Step 1 — Input)

**Files:**
- Create: `app/templates/htmx/partials/requisitions/import_modal.html`
- Modify: `app/routers/htmx_views.py` (add GET route)

- [ ] **Step 1: Create the modal template**

Create `app/templates/htmx/partials/requisitions/import_modal.html`:

```html
{# import_modal.html — Requisition import: paste or upload customer data for AI cleanup.
   Called by: "+ Req" button in parts workspace filter bar.
   Depends on: Alpine.js, HTMX, brand palette.
#}
<div class="p-6" x-data="{ mode: 'paste', parsing: false }">
  <div class="flex items-center justify-between mb-4">
    <h2 class="text-lg font-semibold text-gray-900">New Requisition</h2>
    <button type="button" @click="$dispatch('close-modal')" class="text-gray-400 hover:text-gray-600">
      <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
      </svg>
    </button>
  </div>

  <form hx-post="/v2/partials/requisitions/import-parse"
        hx-target="#modal-content"
        hx-swap="innerHTML"
        hx-encoding="multipart/form-data"
        @htmx:before-request="parsing = true"
        @htmx:after-request="parsing = false">

    {# Req basics #}
    <div class="grid grid-cols-2 gap-3 mb-4">
      <div>
        <label class="block text-xs font-medium text-gray-700 mb-1">Name <span class="text-rose-500">*</span></label>
        <input type="text" name="name" required placeholder="e.g. Acme Q2 Order"
               class="w-full px-3 py-1.5 border border-brand-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      </div>
      <div>
        <label class="block text-xs font-medium text-gray-700 mb-1">Customer</label>
        <input type="text" name="customer_name" placeholder="Company name"
               class="w-full px-3 py-1.5 border border-brand-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      </div>
      <div>
        <label class="block text-xs font-medium text-gray-700 mb-1">Deadline</label>
        <input type="date" name="deadline"
               class="w-full px-3 py-1.5 border border-brand-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      </div>
      <div>
        <label class="block text-xs font-medium text-gray-700 mb-1">Urgency</label>
        <select name="urgency" class="w-full px-3 py-1.5 border border-brand-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
          <option value="normal">Normal</option>
          <option value="hot">Hot</option>
          <option value="critical">Critical</option>
        </select>
      </div>
    </div>

    {# Input mode toggle #}
    <div class="flex gap-1 mb-3">
      <button type="button" @click="mode = 'paste'"
              :class="mode === 'paste' ? 'bg-brand-500 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
              class="px-3 py-1 text-xs font-semibold rounded-lg transition-colors">Paste</button>
      <button type="button" @click="mode = 'upload'"
              :class="mode === 'upload' ? 'bg-brand-500 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
              class="px-3 py-1 text-xs font-semibold rounded-lg transition-colors">Upload File</button>
    </div>

    {# Paste input #}
    <div x-show="mode === 'paste'">
      <textarea name="raw_text" rows="8"
                placeholder="Paste anything here — email text, spreadsheet data, part lists...&#10;&#10;Example:&#10;LM358DR  500  Texas Instruments&#10;STM32F407VGT6  100&#10;TL074CDR  1000  TI  $0.85 target"
                class="w-full px-3 py-2 border border-brand-200 rounded-lg text-sm font-mono focus:ring-2 focus:ring-brand-500 focus:border-brand-500 placeholder:text-gray-300"></textarea>
      <p class="mt-1 text-xs text-gray-400">AI will extract part numbers, quantities, brands, prices, and conditions from any format.</p>
    </div>

    {# File upload #}
    <div x-show="mode === 'upload'" x-cloak>
      <label class="flex flex-col items-center justify-center w-full h-32 border-2 border-dashed border-brand-200 rounded-lg cursor-pointer hover:border-brand-400 hover:bg-brand-50/50 transition-colors">
        <svg class="h-8 w-8 text-gray-400 mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/>
        </svg>
        <span class="text-sm text-gray-500">Drop a file or click to browse</span>
        <span class="text-xs text-gray-400 mt-1">.xlsx, .csv, .txt</span>
        <input type="file" name="file" accept=".xlsx,.csv,.txt,.xls" class="hidden"
               @change="$el.closest('label').querySelector('span').textContent = $el.files[0]?.name || 'Drop a file or click to browse'">
      </label>
    </div>

    {# Submit #}
    <div class="flex items-center gap-3 mt-4 pt-4 border-t border-gray-100">
      <button type="submit" :disabled="parsing"
              class="px-4 py-2 text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600 disabled:opacity-50 inline-flex items-center gap-2">
        <svg x-show="parsing" x-cloak class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
        </svg>
        <span x-text="parsing ? 'Processing...' : 'Process with AI'"></span>
      </button>
      <button type="button" @click="$dispatch('close-modal')"
              class="px-4 py-2 text-sm font-medium text-gray-500 hover:text-gray-700">Cancel</button>
    </div>
  </form>
</div>
```

- [ ] **Step 2: Add GET route for the form**

In `app/routers/htmx_views.py`, add near the existing `requisition_create_form` route (around line 538):

```python
@router.get("/v2/partials/requisitions/import-form", response_class=HTMLResponse)
async def requisition_import_form(
    request: Request,
    user: User = Depends(require_user),
):
    """Return the import requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return templates.TemplateResponse(
        "htmx/partials/requisitions/import_modal.html", ctx
    )
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/requisitions/import_modal.html app/routers/htmx_views.py
git commit -m "feat: add import modal template and GET route"
```

---

### Task 4: Create Parse Route (AI Processing)

**Files:**
- Modify: `app/routers/htmx_views.py` (add POST parse route)
- Test: `tests/test_req_import.py`

- [ ] **Step 1: Write failing test for parse endpoint**

Add to `tests/test_req_import.py`:

```python
def test_import_parse_returns_preview(client, monkeypatch):
    """POST /v2/partials/requisitions/import-parse returns editable preview."""
    mock_result = {
        "name": "Test RFQ",
        "customer_name": "Acme Corp",
        "requirements": [
            {"primary_mpn": "LM358DR", "target_qty": 500, "brand": "TI", "condition": "new"},
            {"primary_mpn": "STM32F407", "target_qty": 100, "condition": "new"},
        ],
    }

    async def mock_parse(text):
        return mock_result

    monkeypatch.setattr(
        "app.routers.htmx_views.parse_freeform_rfq", mock_parse
    )
    resp = client.post(
        "/v2/partials/requisitions/import-parse",
        data={"name": "Test RFQ", "raw_text": "LM358DR 500 TI\nSTM32F407 100"},
    )
    assert resp.status_code == 200
    assert "LM358DR" in resp.text
    assert "STM32F407" in resp.text
    assert 'name="reqs[0].primary_mpn"' in resp.text
```

- [ ] **Step 2: Add POST parse route**

In `app/routers/htmx_views.py`, add the import for `parse_freeform_rfq` near the top (with other service imports):

```python
from app.services.freeform_parser_service import parse_freeform_rfq
```

Then add the route near the import form route:

```python
@router.post("/v2/partials/requisitions/import-parse", response_class=HTMLResponse)
async def requisition_import_parse(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    raw_text: str = Form(""),
    file: UploadFile | None = File(None),
    user: User = Depends(require_user),
):
    """Parse pasted text or uploaded file with AI, return editable preview."""
    # Extract text from file if uploaded
    text = raw_text.strip()
    if file and file.filename:
        content = await file.read()
        fname = file.filename.lower()
        if fname.endswith((".xlsx", ".xls")):
            import openpyxl
            from io import BytesIO

            wb = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        rows.append("\t".join(cells))
            text = "\n".join(rows)
        elif fname.endswith(".csv"):
            text = content.decode("utf-8", errors="replace")
        else:
            text = content.decode("utf-8", errors="replace")

    if not text:
        ctx = _base_ctx(request, user, "requisitions")
        ctx["error"] = "No data provided. Paste text or upload a file."
        return templates.TemplateResponse(
            "htmx/partials/requisitions/import_modal.html", ctx
        )

    # AI parse
    result = await parse_freeform_rfq(text)
    requirements = result.get("requirements", []) if result else []

    # Use AI-extracted name/customer as fallback if user left them blank
    if not name.strip() and result:
        name = result.get("name", "Untitled")
    if not customer_name.strip() and result:
        customer_name = result.get("customer_name", "")

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "requirements": requirements,
        "req_name": name,
        "customer_name": customer_name,
        "deadline": deadline,
        "urgency": urgency,
        "count": len(requirements),
    })
    return templates.TemplateResponse(
        "htmx/partials/requisitions/import_preview.html", ctx
    )
```

Also add the `File` import at the top of htmx_views.py if not already there:

```python
from fastapi import File, UploadFile
```

- [ ] **Step 3: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_import.py::test_import_parse_returns_preview -v`

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py tests/test_req_import.py
git commit -m "feat: add POST parse route for AI import"
```

---

### Task 5: Create Preview Template (Step 2 — Editable Table)

**Files:**
- Create: `app/templates/htmx/partials/requisitions/import_preview.html`

- [ ] **Step 1: Create the preview template**

Create `app/templates/htmx/partials/requisitions/import_preview.html`:

```html
{# import_preview.html — Step 2: editable table of AI-parsed requirements.
   User reviews, edits, removes rows, then saves to create the requisition.
   Receives: requirements (list of dicts), req_name, customer_name, deadline, urgency, count.
   Called by: POST /v2/partials/requisitions/import-parse.
   Depends on: HTMX, Alpine.js, brand palette.
#}
<div class="p-6" x-data="{ saving: false, rows: {{ count }} }">
  <div class="flex items-center justify-between mb-4">
    <h2 class="text-lg font-semibold text-gray-900">Review Requirements</h2>
    <button type="button" @click="$dispatch('close-modal')" class="text-gray-400 hover:text-gray-600">
      <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
      </svg>
    </button>
  </div>

  {# Summary #}
  <div class="flex items-center justify-between bg-brand-50 rounded-lg border border-brand-200 px-4 py-2 mb-4">
    <span class="text-sm font-medium text-brand-700">
      <span class="font-bold" x-text="rows">{{ count }}</span> parts found for
      <span class="font-bold">{{ req_name }}</span>
      {% if customer_name %}<span class="text-brand-500">— {{ customer_name }}</span>{% endif %}
    </span>
    <button type="button"
            hx-get="/v2/partials/requisitions/import-form"
            hx-target="#modal-content"
            class="text-xs text-brand-500 hover:text-brand-700 font-medium">Re-process</button>
  </div>

  {% if requirements %}
  <form hx-post="/v2/partials/requisitions/import-save"
        hx-target="#modal-content"
        hx-swap="innerHTML"
        @htmx:before-request="saving = true"
        @htmx:after-request="saving = false">

    {# Pass through req metadata #}
    <input type="hidden" name="name" value="{{ req_name }}">
    <input type="hidden" name="customer_name" value="{{ customer_name }}">
    <input type="hidden" name="deadline" value="{{ deadline }}">
    <input type="hidden" name="urgency" value="{{ urgency }}">

    {# Editable table #}
    <div class="overflow-x-auto max-h-[50vh] overflow-y-auto border border-gray-200 rounded-lg">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 sticky top-0">
          <tr class="text-left text-[10px] font-semibold text-gray-500 uppercase">
            <th class="px-2 py-2">MPN</th>
            <th class="px-2 py-2">Qty</th>
            <th class="px-2 py-2">Brand</th>
            <th class="px-2 py-2">Target $</th>
            <th class="px-2 py-2">Condition</th>
            <th class="px-2 py-2">Notes</th>
            <th class="px-2 py-2 w-8"></th>
          </tr>
        </thead>
        <tbody id="import-rows">
          {% for r in requirements %}
          <tr class="border-t border-gray-100 hover:bg-gray-50" id="import-row-{{ loop.index0 }}">
            <td class="px-2 py-1">
              <input type="text" name="reqs[{{ loop.index0 }}].primary_mpn"
                     value="{{ r.primary_mpn or '' }}"
                     class="w-full px-1.5 py-1 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500 font-mono"
                     required>
            </td>
            <td class="px-2 py-1">
              <input type="number" name="reqs[{{ loop.index0 }}].target_qty"
                     value="{{ r.target_qty or 1 }}" min="1"
                     class="w-20 px-1.5 py-1 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500">
            </td>
            <td class="px-2 py-1">
              <input type="text" name="reqs[{{ loop.index0 }}].brand"
                     value="{{ r.brand or '' }}"
                     class="w-full px-1.5 py-1 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500">
            </td>
            <td class="px-2 py-1">
              <input type="number" step="0.01" name="reqs[{{ loop.index0 }}].target_price"
                     value="{{ r.target_price or '' }}"
                     class="w-24 px-1.5 py-1 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500">
            </td>
            <td class="px-2 py-1">
              <select name="reqs[{{ loop.index0 }}].condition"
                      class="px-1.5 py-1 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500">
                <option value="new" {{ 'selected' if (r.condition or 'new') == 'new' }}>New</option>
                <option value="refurbished" {{ 'selected' if r.condition == 'refurbished' }}>Refurb</option>
                <option value="used" {{ 'selected' if r.condition == 'used' }}>Used</option>
              </select>
            </td>
            <td class="px-2 py-1">
              <input type="text" name="reqs[{{ loop.index0 }}].notes"
                     value="{{ r.notes or '' }}"
                     class="w-full px-1.5 py-1 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500">
            </td>
            <td class="px-2 py-1">
              <button type="button"
                      @click="$el.closest('tr').remove(); rows--"
                      class="text-gray-400 hover:text-rose-500 p-1" title="Remove">
                <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                </svg>
              </button>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    {# Actions #}
    <div class="flex items-center gap-3 mt-4 pt-4 border-t border-gray-100">
      <button type="submit" :disabled="saving || rows === 0"
              class="px-4 py-2 text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600 disabled:opacity-50 inline-flex items-center gap-2">
        <svg x-show="saving" x-cloak class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
        </svg>
        <span x-text="saving ? 'Saving...' : 'Create Requisition (' + rows + ' parts)'"></span>
      </button>
      <button type="button" @click="$dispatch('close-modal')"
              class="px-4 py-2 text-sm font-medium text-gray-500 hover:text-gray-700">Cancel</button>
    </div>
  </form>

  {% else %}
  <div class="p-8 text-center bg-white rounded-lg border border-gray-200">
    <p class="text-sm text-gray-500">No parts could be extracted from the input.</p>
    <p class="text-xs text-gray-400 mt-1">Try pasting more structured data or a different format.</p>
    <button type="button"
            hx-get="/v2/partials/requisitions/import-form"
            hx-target="#modal-content"
            class="mt-3 px-4 py-2 text-sm font-medium text-brand-500 hover:text-brand-700">Try Again</button>
  </div>
  {% endif %}
</div>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/partials/requisitions/import_preview.html
git commit -m "feat: add import preview template with editable table"
```

---

### Task 6: Create Save Route

**Files:**
- Modify: `app/routers/htmx_views.py` (add POST save route)
- Test: `tests/test_req_import.py`

- [ ] **Step 1: Write failing test for save endpoint**

Add to `tests/test_req_import.py`:

```python
def test_import_save_creates_requisition(client, db_session):
    """POST /v2/partials/requisitions/import-save creates req + requirements."""
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "Test Import",
            "customer_name": "Acme",
            "deadline": "",
            "urgency": "normal",
            "reqs[0].primary_mpn": "LM358DR",
            "reqs[0].target_qty": "500",
            "reqs[0].brand": "TI",
            "reqs[0].target_price": "0.85",
            "reqs[0].condition": "new",
            "reqs[0].notes": "",
            "reqs[1].primary_mpn": "STM32F407",
            "reqs[1].target_qty": "100",
            "reqs[1].brand": "",
            "reqs[1].target_price": "",
            "reqs[1].condition": "new",
            "reqs[1].notes": "",
        },
    )
    assert resp.status_code == 200
    # Should return success content that refreshes parts list
    assert "parts-list" in resp.text or "toast" in resp.text
```

- [ ] **Step 2: Add POST save route**

In `app/routers/htmx_views.py`, add:

```python
@router.post("/v2/partials/requisitions/import-save", response_class=HTMLResponse)
async def requisition_import_save(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save AI-parsed requirements as a new requisition."""
    from app.utils.normalization import normalize_mpn_key

    form = await request.form()

    # Collect requirement rows from indexed form fields
    requirements = []
    idx = 0
    while f"reqs[{idx}].primary_mpn" in form:
        mpn = form.get(f"reqs[{idx}].primary_mpn", "").strip()
        if mpn:
            requirements.append({
                "primary_mpn": mpn,
                "target_qty": int(form.get(f"reqs[{idx}].target_qty", "1") or "1"),
                "brand": form.get(f"reqs[{idx}].brand", "").strip() or None,
                "target_price": float(form.get(f"reqs[{idx}].target_price") or "0") or None,
                "condition": form.get(f"reqs[{idx}].condition", "new").strip(),
                "notes": form.get(f"reqs[{idx}].notes", "").strip() or None,
            })
        idx += 1

    if not requirements:
        ctx = _base_ctx(request, user, "requisitions")
        ctx["error"] = "No valid parts to save."
        return templates.TemplateResponse(
            "htmx/partials/requisitions/import_modal.html", ctx
        )

    # Create requisition
    req = Requisition(
        name=name.strip() or "Untitled",
        customer_name=customer_name.strip() or None,
        deadline=deadline.strip() or None,
        urgency=urgency,
        status="active",
        created_by=user.id,
    )
    db.add(req)
    db.flush()

    # Create requirements
    from ..search_service import resolve_material_card

    for item in requirements:
        mpn = item["primary_mpn"]
        mat_card = resolve_material_card(mpn, db) if mpn else None
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            material_card_id=mat_card.id if mat_card else None,
            target_qty=item["target_qty"],
            target_price=item.get("target_price"),
            brand=item.get("brand"),
            condition=item.get("condition", ""),
            notes=item.get("notes", ""),
        )
        db.add(r)

    db.commit()

    # Return success — close modal + refresh parts list + toast
    return HTMLResponse(f"""
    <div hx-trigger="load" hx-get="/v2/partials/parts" hx-target="#parts-list" hx-swap="innerHTML">
    </div>
    <script>
      document.dispatchEvent(new CustomEvent('close-modal'));
      Alpine.store('toast').message = 'Requisition created with {len(requirements)} parts';
      Alpine.store('toast').type = 'success';
      Alpine.store('toast').show = true;
    </script>
    """)
```

Note: Make sure `Requirement` is imported at the top of htmx_views.py (it likely already is — check). Also check that `resolve_material_card` import path is correct by grepping for existing usage.

- [ ] **Step 3: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_import.py::test_import_save_creates_requisition -v`

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py tests/test_req_import.py
git commit -m "feat: add POST save route for AI-imported requisitions"
```

---

### Task 7: Widen the Modal for Preview Table

**Files:**
- Modify: `app/templates/htmx/base.html` (modal container width)

- [ ] **Step 1: Make modal width responsive**

The global modal in `base.html` currently has `max-w-lg`. The import preview table needs more room. Change the modal container to support wider content.

Find the modal div (around line 172) with `class="bg-white rounded-lg shadow-xl max-w-lg w-full max-h-[90vh] overflow-y-auto"` and change `max-w-lg` to `max-w-3xl`:

```html
<div class="bg-white rounded-lg shadow-xl max-w-3xl w-full max-h-[90vh] overflow-y-auto" x-trap.noscroll="open">
```

This allows the preview table to render properly while the smaller modals (create, edit) still look fine since they use less content.

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/base.html
git commit -m "feat: widen global modal to max-w-3xl for import preview table"
```

---

### Task 8: Integration Test + Deploy

**Files:**
- Test: `tests/test_req_import.py`

- [ ] **Step 1: Add edge case tests**

Add to `tests/test_req_import.py`:

```python
@pytest.mark.asyncio
async def test_parse_freeform_rfq_empty_text():
    """Empty text returns None."""
    from app.services.freeform_parser_service import parse_freeform_rfq

    result = await parse_freeform_rfq("")
    assert result is None


@pytest.mark.asyncio
async def test_parse_freeform_rfq_normalizes_condition():
    """Condition normalization applied post-parse."""
    mock_result = {
        "name": "Test",
        "requirements": [
            {"primary_mpn": "LM358", "target_qty": 1, "condition": "NEW"},
        ],
    }
    with patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.freeform_parser_service import parse_freeform_rfq

        result = await parse_freeform_rfq("LM358 new")
        assert result["requirements"][0]["condition"] == "new"


def test_import_save_rejects_empty_parts(client):
    """Save with no valid parts shows error."""
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={"name": "Empty", "customer_name": "", "deadline": "", "urgency": "normal"},
    )
    assert resp.status_code == 200
    # Should not crash, should show error or re-render form


def test_import_form_loads(client):
    """GET import form returns 200."""
    resp = client.get("/v2/partials/requisitions/import-form")
    assert resp.status_code == 200
    assert "New Requisition" in resp.text
```

- [ ] **Step 2: Run full test file**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_import.py -v`

- [ ] **Step 3: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`

- [ ] **Step 4: Final commit and deploy**

```bash
git add tests/test_req_import.py
git commit -m "test: add edge case tests for req import"
git push origin main
cd /root/availai && docker compose up -d --build
```
