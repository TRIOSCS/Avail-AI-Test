# Motion Reference

## Contents
- Alpine.js Transitions
- HTMX Loading States
- Hover & Focus States
- Spinner Pattern
- CSS Transitions

---

## Alpine.js Transitions

No animation library (Framer Motion, GSAP) — all motion uses Alpine.js `x-transition` directives and Tailwind CSS transitions. This is intentional: HTMX's server-driven model doesn't benefit from client-side animation libraries.

**Standard enter/leave for dropdowns and search results:**

```html
<div x-show="open" x-cloak
     x-transition:enter="transition ease-out duration-100"
     x-transition:enter-start="opacity-0 -translate-y-1"
     x-transition:enter-end="opacity-100 translate-y-0"
     x-transition:leave="transition ease-in duration-75"
     x-transition:leave-start="opacity-100 translate-y-0"
     x-transition:leave-end="opacity-0 -translate-y-1"
     class="absolute top-full mt-1 rounded-lg border border-brand-200 bg-white shadow-lg">
</div>
```

**Simple fade for expandable panels (no translate — avoids layout shift):**

```html
<div x-show="expanded" x-cloak x-transition
     class="mt-3 pt-3 border-t border-gray-100">
  <!-- expanded content -->
</div>
```

`x-cloak` is required on any `x-show` element. Without it, the element flashes visible before Alpine initializes. The `[x-cloak] { display: none }` rule is in `styles.css`.

---

## HTMX Loading States

Use the `htmx-ext-loading-states` extension (already loaded in `htmx_app.js`). Add `data-loading` attributes instead of writing Alpine booleans for every button.

```html
<!-- Button shows spinner text while request is in-flight -->
<button class="btn-primary"
        hx-post="/v2/partials/rfq/send"
        hx-target="#rfq-result"
        data-loading-class="opacity-50 cursor-not-allowed"
        data-loading-aria-label="Sending...">
  Send RFQ
</button>

<!-- Show a spinner div only during load -->
<div class="hidden" data-loading-class-remove="hidden">
  <svg class="h-4 w-4 text-brand-500 spinner" viewBox="0 0 24 24" fill="none">
    <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3"
            stroke-dasharray="31.4" stroke-dashoffset="10"/>
  </svg>
</div>
```

**CSS spinner** (from `base_page.html`) — uses a CSS animation, not a JS timer:

```css
/* In styles.css — already defined */
.spinner {
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
```

---

## Hover & Focus States

Card hover: `hover:border-brand-400 hover:shadow-md transition-all` — border darkens and shadow appears. Always pair with `transition-all` or `transition-colors`.

Input focus: `focus:border-brand-500 focus:ring-1 focus:ring-brand-500` — matches the brand color, replaces browser default outline.

Row hover in tables: `transition: background-color 0.15s` applied globally in `styles.css`. No need to add per-table.

Chevron rotation for expand/collapse — always use `:class` binding, not CSS:

```html
<svg class="h-5 w-5 text-gray-400 transition-transform duration-200"
     :class="{ 'rotate-180': expanded }">
  <path d="M19 9l-7 7-7-7"/>
</svg>
```

---

## CSS Transitions

Tailwind `transition-colors` (150ms ease) for button/badge state changes.
Tailwind `transition-all` (150ms ease) for card hover (border + shadow together).
`transition-transform` for chevrons and icons that rotate.

**NEVER** use `transition: all 0.3s` on table rows — it makes large tables sluggish as it re-transitions every property on every row rerender.

Score bars use `transition-all` on the width — this creates a satisfying fill animation when the partial first renders. Keep it.

```html
<div class="h-1.5 rounded-full transition-all duration-500"
     style="width: {{ score }}%"></div>
