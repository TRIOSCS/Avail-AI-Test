# In-App Guidance Reference

## Contents
- Tooltip Patterns with Alpine.js
- Contextual Help in Partials
- Progress Indicators
- Task Checklist Pattern
- Anti-Patterns

## Tooltip Patterns with Alpine.js

AvailAI uses Alpine.js for all UI state. Tooltips use `x-show` + `@mouseenter`/`@mouseleave`:

```jinja2
{# Inline tooltip on any element #}
<div class="relative inline-block"
     x-data="{ show: false }"
     @mouseenter="show = true"
     @mouseleave="show = false">
  <button class="icon-btn">
    <svg><!-- info icon --></svg>
  </button>
  <div x-show="show"
       x-transition
       class="absolute z-10 bottom-full left-1/2 -translate-x-1/2 mb-2
              bg-gray-800 text-white text-xs rounded px-2 py-1 w-48 text-center">
    Send RFQ to selected vendors via Microsoft Graph API.
  </div>
</div>
```

## Contextual Help in Partials

For complex workflows (RFQ compose, buy plan creation), add a collapsible help section using the `@alpinejs/collapse` plugin:

```jinja2
{# Collapsible how-it-works panel #}
<div x-data="{ open: false }">
  <button @click="open = !open"
          class="text-sm text-gray-500 flex items-center gap-1">
    <svg x-bind:class="open && 'rotate-90'" class="w-3 h-3 transition-transform">
      <!-- chevron icon -->
    </svg>
    How does RFQ sending work?
  </button>
  <div x-collapse x-show="open" class="mt-2 text-sm text-gray-600 bg-gray-50 rounded p-3">
    <ol class="list-decimal list-inside space-y-1">
      <li>Select vendors from the sightings list</li>
      <li>Click "Send RFQ" — emails go out via your connected Microsoft account</li>
      <li>Replies are auto-parsed by AI within 30 minutes</li>
    </ol>
  </div>
</div>
```

## Progress Indicators

Use the SSE streaming pattern for long-running operations. For shorter operations, use HTMX `htmx-ext-loading-states`:

```jinja2
{# Loading state on form submit — disables button, shows spinner #}
<form hx-post="/api/requisitions/{{ id }}/search"
      hx-target="#search-results"
      hx-ext="loading-states">
  <button type="submit"
          data-loading-disable
          data-loading-class="opacity-50 cursor-not-allowed"
          class="btn-primary">
    <span data-loading-class="hidden">Search All Sources</span>
    <span data-loading-class-remove="hidden" class="hidden">Searching...</span>
  </button>
</form>
```

For multi-step workflows, show a step indicator in the partial header:

```jinja2
{# Step indicator for buy plan creation #}
<div class="flex items-center gap-2 mb-6">
  {% for step in ["Details", "Vendors", "Review"] %}
    <div class="flex items-center gap-1">
      <span class="step-dot {% if loop.index <= current_step %}bg-indigo-600{% else %}bg-gray-300{% endif %}">
        {{ loop.index }}
      </span>
      <span class="text-sm {% if loop.index == current_step %}font-medium{% else %}text-gray-400{% endif %}">
        {{ step }}
      </span>
    </div>
    {% if not loop.last %}<div class="h-px flex-1 bg-gray-200"></div>{% endif %}
  {% endfor %}
</div>
```

## Task Checklist Pattern

The tasks tab in `app/templates/htmx/partials/requisitions/tabs/tasks.html` shows a filterable checklist. Replicate this pattern for onboarding checklists:

```jinja2
{# Onboarding checklist using Alpine.js #}
<div x-data="{
  steps: [
    { id: 1, label: 'Create your first requisition', done: {{ has_requisition|lower }} },
    { id: 2, label: 'Run a search to find vendors', done: {{ has_sightings|lower }} },
    { id: 3, label: 'Send your first RFQ', done: {{ has_rfq|lower }} },
  ],
  get progress() { return this.steps.filter(s => s.done).length }
}">
  <div class="mb-3">
    <div class="text-sm text-gray-500">
      <span x-text="progress"></span> of <span x-text="steps.length"></span> complete
    </div>
    <div class="h-1.5 bg-gray-200 rounded-full mt-1">
      <div class="h-full bg-indigo-500 rounded-full transition-all"
           :style="`width: ${(progress/steps.length)*100}%`"></div>
    </div>
  </div>
  <template x-for="step in steps" :key="step.id">
    <div class="flex items-center gap-2 py-2 border-b border-gray-100">
      <svg x-show="step.done" class="w-4 h-4 text-green-500"><!-- check --></svg>
      <svg x-show="!step.done" class="w-4 h-4 text-gray-300"><!-- circle --></svg>
      <span :class="step.done && 'line-through text-gray-400'" x-text="step.label"></span>
    </div>
  </template>
</div>
```

## Anti-Patterns

### WARNING: Hardcoded Guidance Copy in Python

```python
# BAD — copy buried in router, can't be updated without code change
return {"help_text": "Create a requisition to start sourcing parts."}
```

**The Fix:** Keep all copy in Jinja2 templates. Pass booleans (`has_data`, `is_first_run`) from the router, not copy strings.

### WARNING: Guidance Shown After First Action

Don't show "how to create a requisition" tooltip on the requisitions list after the user already has 10 requisitions. Gate guidance on `{{ items|length == 0 }}` or pass a `first_visit` flag from the router.

See the **htmx** skill for loading state extensions and the **frontend-design** skill for styling conventions.
