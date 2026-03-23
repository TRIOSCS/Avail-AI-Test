# In-App Guidance Reference

## Contents
- Toast Notifications
- Inline Help Text
- Confirmation Modals
- Loading States
- Anti-Patterns

---

## Toast Notifications

The global toast system lives in `app/templates/base.html` and is controlled via Alpine.js store. Trigger toasts from HTMX responses using `HX-Trigger` response headers.

```python
# app/routers/htmx_views.py — trigger a toast from a route
from fastapi.responses import HTMLResponse

@router.post("/v2/requisitions/{id}/send-rfq")
async def send_rfq(id: int, user=Depends(require_buyer), db: Session = Depends(get_db)):
    # ... business logic ...
    response = HTMLResponse(content="")
    response.headers["HX-Trigger"] = json.dumps({
        "showToast": {"message": "RFQ sent to 3 vendors", "type": "success"}
    })
    return response
```

```javascript
// app/static/htmx_app.js — toast store listener
document.addEventListener("showToast", (e) => {
  Alpine.store("toast").show(e.detail.message, e.detail.type);
});
```

```html
<!-- app/templates/base.html — toast container -->
<div x-data x-on:showtoast.window="$store.toast.show($event.detail.message, $event.detail.type)"
     class="toast-container" role="alert" aria-live="polite">
  <template x-if="$store.toast.visible">
    <div :class="`toast toast--${$store.toast.type}`" x-text="$store.toast.message"></div>
  </template>
</div>
```

---

## Inline Help Text

Contextual help belongs in the Jinja2 partial, adjacent to the field it describes. Do NOT use tooltips that require JS — use `title` attributes or `<details>` for disclosures.

```html
<!-- app/templates/htmx/partials/requisitions/new.html -->
<div class="field-group">
  <label for="target_qty">Target Quantity</label>
  <input id="target_qty" name="target_qty" type="number" min="1" required>
  <p class="field-hint">
    Enter the total quantity needed. AvailAI searches all 10 sources for this exact quantity.
  </p>
</div>

<!-- For complex fields, use a disclosure -->
<details class="field-help">
  <summary>What is a condition code?</summary>
  <p>NS = New/Sealed. R = Refurbished. U = Used. Leaving blank searches all conditions.</p>
</details>
```

---

## Confirmation Modals

Use the global modal in `base.html` for destructive actions. Trigger via HTMX + Alpine.

```html
<!-- app/templates/htmx/partials/vendors/detail.html -->
<button
  hx-get="/v2/vendors/{{ vendor.id }}/confirm-delete"
  hx-target="#modal-content"
  hx-swap="innerHTML"
  @click="$store.modal.open()">
  Delete Vendor
</button>
```

```html
<!-- app/templates/htmx/partials/vendors/confirm-delete.html -->
<div class="modal-body">
  <h3>Delete {{ vendor.name }}?</h3>
  <p>This removes all associated contacts and sightings. This cannot be undone.</p>
  <div class="modal-actions">
    <button @click="$store.modal.close()">Cancel</button>
    <button
      hx-delete="/api/vendors/{{ vendor.id }}"
      hx-target="#main-content"
      hx-swap="innerHTML"
      @click="$store.modal.close()"
      class="btn-danger">
      Delete
    </button>
  </div>
</div>
```

---

## Loading States

Use `htmx-ext-loading-states` for request-scoped indicators. NEVER use a manual `x-show` toggle for HTMX loading — it causes race conditions when requests complete faster than Alpine updates.

```html
<!-- DO: htmx-ext-loading-states handles timing correctly -->
<button
  hx-post="/api/search"
  hx-target="#results"
  data-loading-disable
  data-loading-class="btn--loading">
  Search
</button>
<div id="results" data-loading-class-remove="hidden">
  <!-- results appear here -->
</div>
```

```html
<!-- DON'T: manual Alpine toggle races with HTMX -->
<button @click="loading = true" hx-post="/api/search" hx-target="#results">
  <span x-show="!loading">Search</span>
  <span x-show="loading">Searching...</span>
</button>
```

**Why the DON'T breaks:** Alpine's `loading = true` runs on click, but `loading = false` has no trigger — it never resets unless you add an `htmx:afterRequest` listener. The button stays in loading state permanently if the request errors.

See the **htmx** skill for full `loading-states` extension patterns.

---

## Anti-Patterns

### WARNING: Guidance Text Hardcoded in Routes

**The Problem:**
```python
# BAD — UX copy in Python, impossible to update without redeploy
return {"message": "You need to add at least one vendor before sending an RFQ."}
```

**The Fix:** Put guidance text in templates. Pass only data (counts, booleans) from routes.

```python
# GOOD — route passes data, template owns copy
return templates.TemplateResponse("htmx/partials/rfq/send.html", {
    "request": request,
    "vendor_count": vendor_count,  # template decides what to say
})
