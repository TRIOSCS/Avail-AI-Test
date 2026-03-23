---
name: frontend-design
description: |
  Designs UI with HTMX, Alpine.js, Jinja2, and Tailwind CSS for the AvailAI sourcing platform.
  Use when: building new partials, adding HTMX interactions, styling components, creating page layouts,
  implementing inline editing, adding loading states, or matching existing visual identity.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_close, mcp__playwright__browser_resize, mcp__playwright__browser_console_messages, mcp__playwright__browser_handle_dialog, mcp__playwright__browser_evaluate, mcp__playwright__browser_file_upload, mcp__playwright__browser_fill_form, mcp__playwright__browser_install, mcp__playwright__browser_press_key, mcp__playwright__browser_type, mcp__playwright__browser_navigate, mcp__playwright__browser_navigate_back, mcp__playwright__browser_network_requests, mcp__playwright__browser_run_code, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_snapshot, mcp__playwright__browser_click, mcp__playwright__browser_drag, mcp__playwright__browser_hover, mcp__playwright__browser_select_option, mcp__playwright__browser_tabs, mcp__playwright__browser_wait_for
---

# Frontend-Design Skill

AvailAI uses HTMX-driven navigation (no SPA), Alpine.js for component state, Jinja2 for
server-side rendering, and Tailwind CSS with a custom steel-blue brand palette. All UI lives in
`app/templates/htmx/partials/`. Server returns HTML fragments; HTMX swaps into `#main-content`.
Never build client-side routing or fetch JSON in templates.

## Quick Start

### HTMX partial with lazy load

```html
{# base_page.html pattern — spinner until partial loads #}
<div hx-get="/v2/partials/vendors/list"
     hx-target="#main-content"
     hx-trigger="load"
     hx-swap="innerHTML">
  <div class="flex items-center justify-center py-20">
    <svg class="h-8 w-8 text-gray-400 spinner" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3"
              stroke-dasharray="31.4" stroke-dashoffset="10"/>
    </svg>
  </div>
</div>
```

### Alpine.js expandable panel

```html
<div x-data="{ open: false }" class="card-padded">
  <button @click="open = !open" class="flex items-center justify-between w-full">
    <span class="font-medium text-gray-900">Details</span>
    <svg class="h-5 w-5 text-gray-400 transition-transform" :class="{ 'rotate-180': open }"
         fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
    </svg>
  </button>
  <div x-show="open" x-cloak x-transition class="mt-3 pt-3 border-t border-gray-100">
    <!-- expanded content -->
  </div>
</div>
```

### Status badge

```html
<span class="badge-success">Active</span>
<span class="badge-warning">Pending Review</span>
<span class="badge-danger">Rejected</span>
<span class="badge-info">In Progress</span>
<span class="badge-neutral">Draft</span>
```

### Score progress bar

```html
<div class="w-full bg-gray-200 rounded-full h-1.5">
  <div class="h-1.5 rounded-full transition-all
    {% if score >= 66 %}bg-emerald-500
    {% elif score >= 33 %}bg-brand-500
    {% else %}bg-amber-500{% endif %}"
       style="width: {{ [score, 100]|min }}%"></div>
</div>
```

## Key Concepts

| Concept | Class / Pattern | Notes |
|---------|----------------|-------|
| Primary button | `btn-primary` | brand-500 bg, focus ring |
| Card container | `card` / `card-padded` | 2px border, rounded-xl |
| Data table wrapper | `table-wrapper` | brand scrollbar, overflow-x |
| Monospace data | `font-data` | JetBrains Mono 12px |
| Brand blue mid | `brand-500` = `#5B8FB8` | scrollbars, borders, links |
| Topbar height | `h-12` sticky | 52px bottom padding on main |
| Content swap target | `#main-content` | defined in `base.html` |

## New Partial Checklist

Copy and track progress:
- [ ] Add header comment: what it does, called by, depends on, context vars
- [ ] Use `card` or `table-wrapper` as outermost container
- [ ] Status values rendered via badge classes, not raw inline colors
- [ ] Data cells use `font-data` for MPN/price/qty columns
- [ ] HTMX actions target `#main-content` or a named swap target
- [ ] Alpine transitions use `x-transition` + `x-cloak`
- [ ] Mobile: verify layout at `sm:` breakpoint

## See Also

- [aesthetics](references/aesthetics.md) — brand palette, typography, color usage
- [components](references/components.md) — buttons, badges, cards, tables, forms
- [layouts](references/layouts.md) — page structure, grid, spacing, responsive
- [motion](references/motion.md) — Alpine transitions, HTMX loading states, hover
- [patterns](references/patterns.md) — DO/DON'T decisions, anti-patterns

## Related Skills

- See the **htmx** skill for HTMX attributes, partial routing, and swap patterns
- See the **alpine-js** skill for Alpine stores, directives, and plugin usage
- See the **tailwind-css** skill for utility class conventions and custom config
- See the **fastapi** skill for route handlers that serve these partials
- See the **jinja2** skill for template inheritance and macro patterns
