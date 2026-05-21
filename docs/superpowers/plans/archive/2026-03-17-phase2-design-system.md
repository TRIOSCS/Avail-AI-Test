# Phase 2: Design System — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a unified design system with Inter font, compact spacing, status colors, and reusable component classes in Tailwind.

**Architecture:** Design system is implemented as: (1) updated `tailwind.config.js` theme, (2) Google Fonts link in base template, (3) component classes in `styles.css` using `@layer components`. No new files — just config and CSS changes.

**Tech Stack:** Tailwind CSS 3.4, PostCSS, Vite, Google Fonts

**Depends on:** Phase 1 complete (single template tree, clean codebase)

---

### Task 1: Update Tailwind Config (Typography + Spacing)

**Files:**
- Modify: `tailwind.config.js`

- [ ] **Step 1: Update fontFamily**

In `tailwind.config.js`, replace the fontFamily block:

```js
// OLD:
fontFamily: {
  display: ['Manrope', 'system-ui', 'sans-serif'],
  body:    ['DM Sans', 'system-ui', 'sans-serif'],
  mono:    ['IBM Plex Mono', 'Menlo', 'monospace'],
},

// NEW:
fontFamily: {
  sans: ['Inter', 'system-ui', 'sans-serif'],
  mono: ['IBM Plex Mono', 'Menlo', 'monospace'],
},
```

- [ ] **Step 2: Add compact fontSize overrides**

Add to `theme.extend` in `tailwind.config.js`:

```js
fontSize: {
  xs:   ['0.6875rem', { lineHeight: '1rem' }],     // 11px
  sm:   ['0.8125rem', { lineHeight: '1.125rem' }], // 13px
  base: ['0.875rem', { lineHeight: '1.25rem' }],   // 14px
  lg:   ['1rem', { lineHeight: '1.375rem' }],       // 16px
  xl:   ['1.25rem', { lineHeight: '1.625rem' }],   // 20px
},
```

- [ ] **Step 3: Verify build**

```bash
cd /root/availai && npm run build 2>&1 | tail -10
```

Expected: Build succeeds with no errors.

- [ ] **Step 4: Commit**

```bash
git add tailwind.config.js && git commit -m "design: update Tailwind config — Inter font, compact sizes"
```

---

### Task 2: Add Google Fonts Link

**Files:**
- Modify: `app/templates/htmx/base.html`
- Modify: `app/templates/htmx/login.html`

- [ ] **Step 1: Add Inter font link to base.html**

In `app/templates/htmx/base.html`, add inside `<head>` before the Vite CSS links:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
```

- [ ] **Step 2: Add same link to login.html**

Same `<link>` tags in `app/templates/htmx/login.html` `<head>`.

- [ ] **Step 3: Remove old font-display/font-body class usage**

Search for any remaining `font-display` or `font-body` classes in templates:

```bash
grep -rn "font-display\|font-body" app/templates/ --include="*.html"
```

Replace with `font-sans` or remove (Inter is the default sans now).

- [ ] **Step 4: Commit**

```bash
git add app/templates/ && git commit -m "design: add Inter font via Google Fonts, remove old font classes"
```

---

### Task 3: Rewrite Component Classes in styles.css

**Files:**
- Modify: `app/static/styles.css`

- [ ] **Step 1: Rewrite the entire styles.css**

Replace `app/static/styles.css` with the new design system. Keep the same `@tailwind` directives and `@layer` structure but update all component classes for compact, modern look.

Key component class updates:

**Buttons** — smaller, tighter:
```css
.btn-primary {
  @apply inline-flex items-center gap-1.5 rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white
         hover:bg-brand-700 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-1
         disabled:opacity-50 disabled:pointer-events-none transition-colors;
}
.btn-secondary {
  @apply inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700
         hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-1
         disabled:opacity-50 disabled:pointer-events-none transition-colors;
}
.btn-danger {
  @apply inline-flex items-center gap-1.5 rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white
         hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-1
         disabled:opacity-50 disabled:pointer-events-none transition-colors;
}
.btn-ghost {
  @apply inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium text-gray-600
         hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-1
         disabled:opacity-50 disabled:pointer-events-none transition-colors;
}
.btn-sm {
  @apply px-2 py-1 text-xs;
}
```

**Badges** — pill-shaped, solid colors:
```css
.badge-success { @apply inline-flex items-center rounded-full bg-green-600 px-2 py-0.5 text-xs font-medium text-white; }
.badge-warning { @apply inline-flex items-center rounded-full bg-amber-500 px-2 py-0.5 text-xs font-medium text-white; }
.badge-danger  { @apply inline-flex items-center rounded-full bg-red-600 px-2 py-0.5 text-xs font-medium text-white; }
.badge-info    { @apply inline-flex items-center rounded-full bg-blue-600 px-2 py-0.5 text-xs font-medium text-white; }
.badge-neutral { @apply inline-flex items-center rounded-full bg-gray-500 px-2 py-0.5 text-xs font-medium text-white; }
.badge-ai      { @apply inline-flex items-center rounded-full bg-purple-600 px-2 py-0.5 text-xs font-medium text-white; }
```

**Data table** — compact 36px rows:
```css
.data-table { @apply w-full text-sm; }
.data-table thead { @apply sticky top-0 z-10 bg-gray-50 border-b border-gray-200; }
.data-table th { @apply px-3 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500; }
.data-table td { @apply px-3 py-1.5 text-sm text-gray-700 border-b border-gray-100; }
.data-table tbody tr { @apply hover:bg-gray-50 transition-colors; }
```

**Cards** — minimal, no shadow:
```css
.card { @apply rounded-lg border border-gray-200 bg-white; }
.card-padded { @apply rounded-lg border border-gray-200 bg-white p-3; }
.stat-card { @apply rounded-lg border border-gray-200 bg-white p-3; }
```

**Form inputs** — compact:
```css
.input-field {
  @apply w-full rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-sm text-gray-900
         placeholder:text-gray-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500;
}
.input-search {
  @apply w-full rounded-md border border-gray-300 bg-white pl-9 pr-3 py-1.5 text-sm text-gray-900
         placeholder:text-gray-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500;
}
```

**Tabs** — underline style:
```css
.tab-bar { @apply flex gap-4 border-b border-gray-200; }
.tab-item { @apply px-1 pb-2 text-sm font-medium text-gray-500 hover:text-gray-700 border-b-2 border-transparent cursor-pointer transition-colors; }
.tab-item-active { @apply px-1 pb-2 text-sm font-medium text-brand-600 border-b-2 border-brand-600 cursor-pointer; }
```

**Nav** — compact 32px rows:
```css
.nav-item { @apply flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm font-medium text-brand-200 hover:bg-brand-800 hover:text-white transition-colors; }
.nav-item-active { @apply flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm font-medium bg-brand-900 text-white border-l-2 border-brand-400; }
```

**Page typography:**
```css
.page-title { @apply text-xl font-semibold text-gray-900; }
.page-subtitle { @apply text-sm text-gray-500; }
```

**Source badges** — keep existing connector-specific colors but make them pill-shaped:
```css
.source-badge { @apply inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium text-white; }
.source-badge-brokerbin { @apply source-badge bg-blue-700; }
.source-badge-nexar { @apply source-badge bg-emerald-600; }
.source-badge-digikey { @apply source-badge bg-purple-600; }
.source-badge-mouser { @apply source-badge bg-sky-600; }
.source-badge-oemsecrets { @apply source-badge bg-orange-500; }
.source-badge-element14 { @apply source-badge bg-teal-600; }
```

- [ ] **Step 2: Build and verify**

```bash
cd /root/availai && npm run build 2>&1 | tail -10
```

Expected: Build succeeds. CSS output should be ~45-55KB (similar to current).

- [ ] **Step 3: Commit**

```bash
git add app/static/styles.css && git commit -m "design: rewrite component classes — compact, modern, enterprise"
```

---

### Task 4: Verify & Deploy

- [ ] **Step 1: Full build**

```bash
cd /root/availai && npm run build 2>&1 | tail -10
```

- [ ] **Step 2: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All pass (CSS changes don't affect test assertions).

- [ ] **Step 3: Push and deploy**

```bash
git push origin main
docker compose up -d --build
docker compose logs app --tail 10
```

- [ ] **Step 4: Visual verification**

Load `/v2/requisitions` in browser. Verify:
- Inter font is rendering (check DevTools → Computed → font-family)
- Text is noticeably more compact
- Buttons, badges, tables use updated styles
- No broken layouts
