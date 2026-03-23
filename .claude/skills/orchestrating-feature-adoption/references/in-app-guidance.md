# In-App Guidance Reference

## Contents
- Modal-Based Guidance
- Inline Help Text Patterns
- Tooltip Gaps & Workarounds
- Activity Timeline as Guidance
- DO / DON'T Pairs

---

## Modal-Based Guidance

The existing modal system (`shared/modal.html`) is the right vehicle for step-by-step guidance
that requires user attention. It supports HTMX-loaded content and focus trapping.

**Triggering a guidance modal from a button:**

```html
<!-- In any template — fires @open-modal.window event -->
<button
  @click="$dispatch('open-modal', { url: '/v2/partials/help/rfq-guide', title: 'How RFQ Works' })"
  class="text-sm text-brand-600 hover:underline"
>
  How does this work?
</button>
```

**The modal partial receives an HTMX GET and returns HTML:**

```python
# app/routers/htmx_views.py
@router.get("/v2/partials/help/rfq-guide")
async def rfq_guide_modal(request: Request):
    return templates.TemplateResponse("htmx/partials/help/rfq_guide.html", {"request": request})
```

```html
<!-- app/templates/htmx/partials/help/rfq_guide.html -->
<div class="prose prose-sm max-w-none">
  <ol class="space-y-3">
    <li><strong>Select vendors</strong> from search results using the checkboxes.</li>
    <li><strong>Click "Send RFQ"</strong> — emails go via your connected Microsoft account.</li>
    <li><strong>Wait for replies</strong> — AvailAI checks your inbox every 30 minutes automatically.</li>
    <li><strong>Review parsed offers</strong> — AI extracts price, quantity, and lead time.</li>
  </ol>
</div>
```

## Inline Help Text Patterns

For single-field or short contextual help, use `<p class="text-xs text-gray-500 mt-1">` adjacent
to the element. This is already used throughout the settings and form templates.

```html
<!-- In a form field -->
<label class="block text-sm font-medium text-gray-700">Target Quantity</label>
<input type="number" name="target_qty" class="input-field" />
<p class="text-xs text-gray-500 mt-1">
  Used to filter vendor results below MOQ and flag quantity mismatches.
</p>
```

## Tooltip Gaps & Workarounds

AvailAI has no dedicated tooltip component. The existing pattern uses native HTML `title`
attributes — these are accessible but visually inconsistent on mobile.

**Current approach (acceptable for single data points):**

```html
<!-- Used in insights_panel.html and line_item_row.html -->
<span class="text-xs text-gray-500 cursor-help" title="Confidence based on reply completeness">
  87% confidence
</span>
```

**Alpine-powered tooltip (use for anything needing rich text or mobile support):**

```html
<div class="relative inline-block" x-data="{ show: false }">
  <span
    class="text-xs text-gray-500 cursor-help underline decoration-dotted"
    @mouseenter="show = true"
    @mouseleave="show = false"
  >
    87% confidence
  </span>
  <div
    x-show="show"
    x-transition
    class="absolute bottom-full left-0 mb-1 w-56 bg-gray-800 text-white text-xs rounded p-2 z-50 pointer-events-none"
  >
    Confidence scored on: price presence, quantity match, lead time clarity, date code.
  </div>
</div>
```

## Activity Timeline as Guidance

The `shared/activity_timeline.html` partial is already a guidance surface — it shows users what
actions have been taken and implicitly reveals what actions are possible.

```python
# Include activity context in detail routes to show users the workflow history
activities = (
    db.query(ActivityLog)
    .filter(ActivityLog.requisition_id == req_id)
    .order_by(ActivityLog.occurred_at.desc())
    .limit(10)
    .all()
)
context["activities"] = activities
context["show_activity"] = True
```

```html
<!-- Conditionally include the timeline to guide users on workflow status -->
{% if show_activity %}
  {% include "htmx/partials/shared/activity_timeline.html" %}
{% endif %}
```

## DO / DON'T Pairs

**DO: Load guidance content via HTMX into the modal**
```python
@router.get("/v2/partials/help/{topic}")
async def help_topic(topic: str, request: Request):
    return templates.TemplateResponse(f"htmx/partials/help/{topic}.html", {"request": request})
```
This keeps guidance templates versioned and avoids hardcoding help text in JS strings.

**DON'T: Put guidance content in Alpine `x-data` strings**
```html
<!-- BAD — hardcoded, not translatable, not reviewable -->
<div x-data="{ tip: 'Click Send RFQ to email vendors. Results appear in 30 min.' }">
  <p x-text="tip"></p>
</div>
```
Why: Content in JS strings can't be spell-checked, reviewed, or updated without a code change.

**DO: Group related guidance under a `help/` partial directory**
```
app/templates/htmx/partials/help/
  rfq_guide.html
  search_tips.html
  proactive_intro.html
```

**DON'T: Scatter guidance HTML across feature partials**
One-off help text embedded in a requisition partial becomes invisible to future content updates.

See the **frontend-design** skill for Alpine tooltip component patterns.
See the **jinja2** skill for template includes and macro patterns.
See the **htmx** skill for modal content loading via HTMX GET.
