# Phase 3: App Shell Redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. After each task, run the `simplify` skill to review changed code for reuse, quality, and efficiency.

**Goal:** Rebuild the app shell (sidebar, topbar, mobile nav, drawer/modal system) using the Phase 2 design system.

**Architecture:** The shell is defined in `app/templates/htmx/base.html` (full page wrapper) and `app/templates/htmx/base_page.html` (content wrapper). These templates control sidebar, topbar, mobile nav, global modal, and toast. We rebuild them for the new compact, dense design while keeping all HTMX/Alpine.js functionality intact.

**Tech Stack:** Jinja2, HTMX 2.0, Alpine.js 3.15, Tailwind CSS

**Depends on:** Phase 2 complete (design system in place)

---

### Task 1: Rebuild Sidebar

**Files:**
- Modify: `app/templates/htmx/base.html` — sidebar section

- [ ] **Step 1: Identify current sidebar code**

In `app/templates/htmx/base.html`, find the `<aside>` element (the sidebar). Note the Alpine.js state it uses (`sidebarCollapsed`, `$store.sidebar`).

- [ ] **Step 2: Rewrite sidebar HTML**

Replace the sidebar `<aside>` with new compact design:
- Width: 220px expanded, 56px collapsed (was 256/64)
- Row height: 32px per nav item (was ~40-48px)
- Three nav sections: OPPORTUNITY, RELATIONSHIPS, SYSTEM
- Active item: brand-900 bg + 2px left brand-400 accent bar
- Use `.nav-item` and `.nav-item-active` classes from Phase 2
- Section labels: `text-xs uppercase tracking-wider text-brand-400`
- Logo: 40px height at top
- User info: avatar + name at bottom

Nav items with sections:
```
OPPORTUNITY:
  - Requisitions → /v2/requisitions
  - Part Search  → /v2/search
  - Proactive    → /v2/proactive

RELATIONSHIPS:
  - Vendors   → /v2/vendors
  - Customers → /v2/customers
  - Quotes    → /v2/quotes

SYSTEM:
  - Prospecting → /v2/prospecting
  - Strategic   → /v2/strategic
  - Settings    → /v2/settings
```

Secondary pages need a "More" section at the bottom of the SYSTEM nav group:

```
SYSTEM:
  - Prospecting → /v2/prospecting
  - Strategic   → /v2/strategic
  - Settings    → /v2/settings
  - More ▾      (Alpine.js collapsible)
    - Buy Plans   → /v2/buy-plans
    - Materials   → /v2/materials
    - Follow-ups  → /v2/follow-ups
    - Knowledge   → /v2/knowledge
    - Emails      → /v2/emails
    - Admin       → /v2/admin (if is_admin)
```

Use Alpine.js `x-show` with `$persist` to remember collapsed/expanded state.

- [ ] **Step 3: Preserve Alpine.js sidebar state**

Keep the existing Alpine.js pattern:
```html
<aside x-data :class="$store.sidebar.collapsed ? 'w-14' : 'w-[220px]'" ...>
```

Collapsed state must still work: icons only, tooltips on hover.

- [ ] **Step 4: Test sidebar in browser**

Build and deploy, verify:
- Sidebar renders with correct sections
- Collapse/expand works
- Active nav item highlights correctly
- All links navigate to correct pages

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/base.html && git commit -m "shell: rebuild sidebar — compact, sectioned, 220px/56px"
```

---

### Task 2: Rebuild Topbar

**Files:**
- Modify: `app/templates/htmx/base.html` — header/topbar section

- [ ] **Step 1: Rewrite topbar HTML**

Replace the topbar `<header>` with new 48px-height design:
- Height: 48px (was 56px or variable)
- Left: breadcrumb trail — `text-sm text-gray-500`, separator `/`
- Center: global search input with `Cmd+K` badge, compact
- Right: user dropdown (name + chevron)
- Bottom border: 1px gray-200
- Sticky: `sticky top-0 z-20`

```html
<header class="sticky top-0 z-20 flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4">
  <!-- Breadcrumb -->
  <nav class="flex items-center gap-1.5 text-sm">
    <a href="/v2" class="text-gray-400 hover:text-gray-600">Home</a>
    <span class="text-gray-300">/</span>
    <span id="breadcrumb-current" class="font-medium text-gray-700">{{ current_view|title }}</span>
  </nav>

  <!-- Global search -->
  <div class="relative mx-4 flex-1 max-w-md">
    <input type="search" placeholder="Search... (⌘K)" ... class="input-search" />
  </div>

  <!-- User -->
  <div class="flex items-center gap-2 text-sm text-gray-600">
    <span>{{ user_name }}</span>
  </div>
</header>
```

- [ ] **Step 2: Preserve Cmd+K keyboard shortcut**

Ensure the global search keyboard shortcut (`Cmd+K` / `Ctrl+K`) from `htmx_app.js` still works — it targets the search input by ID.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/base.html && git commit -m "shell: rebuild topbar — 48px, breadcrumb + search + user"
```

---

### Task 3: Rebuild Main Content Area

**Files:**
- Modify: `app/templates/htmx/base.html` — main content wrapper
- Modify: `app/templates/htmx/base_page.html` — if applicable

- [ ] **Step 1: Update main content wrapper**

Remove `max-w-7xl` constraint. Use full available width:

```html
<main id="main-content" class="px-4 py-3">
  {% block content %}{% endblock %}
</main>
```

- Padding: `px-4 py-3` (compact, not `px-6 py-6`)
- Full width: no `max-w-7xl` or `container`
- Left margin matches sidebar width: `:class="$store.sidebar.collapsed ? 'ml-14' : 'ml-[220px]'"`

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/ && git commit -m "shell: full-width main content area with compact padding"
```

---

### Task 4: Rebuild Mobile Navigation

**Files:**
- Modify: `app/templates/htmx/base.html` — mobile nav section

- [ ] **Step 1: Rewrite mobile hamburger + bottom nav**

For screens < 1024px:
- Sidebar hidden, replaced by hamburger button in topbar
- Slide-over drawer for full nav (same sections as desktop sidebar)
- Bottom nav bar: 5 items — Requisitions, Search, Vendors, Customers, Settings
- Bottom nav: fixed, 48px height, icon + tiny label per item

```html
<!-- Mobile bottom nav (lg:hidden) -->
<nav class="fixed bottom-0 inset-x-0 z-30 flex h-12 items-center justify-around border-t border-gray-200 bg-white lg:hidden">
  {% set mobile_nav = [
    ('Reqs', '/v2/requisitions', 'clipboard-icon-svg'),
    ('Search', '/v2/search', 'search-icon-svg'),
    ('Vendors', '/v2/vendors', 'building-icon-svg'),
    ('Customers', '/v2/customers', 'users-icon-svg'),
    ('Settings', '/v2/settings', 'cog-icon-svg'),
  ] %}
  {% for label, href, icon in mobile_nav %}
  <a href="{{ href }}" class="flex flex-col items-center gap-0.5 text-xs text-gray-500 hover:text-brand-600"
     hx-get="{{ href }}" hx-target="#main-content" hx-push-url="{{ href }}">
    <svg class="h-5 w-5">...</svg>
    <span>{{ label }}</span>
  </a>
  {% endfor %}
</nav>
```

- [ ] **Step 2: Ensure mobile drawer works**

The hamburger → slide-over drawer should use Alpine.js `x-show` with transition:
```html
<div x-show="sidebarOpen" x-transition:enter="..." class="fixed inset-0 z-40 lg:hidden">
  <!-- overlay + sidebar content -->
</div>
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/base.html && git commit -m "shell: rebuild mobile nav — bottom bar + slide-over drawer"
```

---

### Task 5: Rebuild Modal/Drawer System

**Files:**
- Modify: `app/templates/htmx/base.html` — global modal section

- [ ] **Step 1: Implement right-side drawer for detail views**

Replace centered modal with a slide-in drawer from right:

```html
<!-- Global drawer (detail views) -->
<div x-data="{ open: false }" x-show="open"
     @open-modal.window="open = true" @close-modal.window="open = false"
     @keydown.escape.window="open = false"
     class="fixed inset-0 z-50">
  <!-- Overlay -->
  <div x-show="open" x-transition:enter="transition ease-out duration-200"
       x-transition:enter-start="opacity-0" x-transition:enter-end="opacity-100"
       @click="open = false"
       class="fixed inset-0 bg-black/30"></div>
  <!-- Drawer panel -->
  <div x-show="open"
       x-transition:enter="transition ease-out duration-200"
       x-transition:enter-start="translate-x-full" x-transition:enter-end="translate-x-0"
       x-transition:leave="transition ease-in duration-150"
       x-transition:leave-start="translate-x-0" x-transition:leave-end="translate-x-full"
       class="fixed inset-y-0 right-0 w-full max-w-2xl bg-white shadow-xl border-l border-gray-200">
    <div id="modal-content" class="h-full overflow-y-auto p-4">
      <!-- HTMX swaps content here -->
    </div>
  </div>
</div>
```

- [ ] **Step 2: Keep small centered modal for confirmations**

Add a separate confirmation modal:

```html
<!-- Confirmation modal (small, centered) -->
<div x-data="{ open: false, title: '', message: '', onConfirm: null }"
     @open-confirm.window="open = true; title = $event.detail.title; message = $event.detail.message; onConfirm = $event.detail.onConfirm"
     x-show="open" class="fixed inset-0 z-50 flex items-center justify-center">
  <div @click="open = false" class="fixed inset-0 bg-black/30"></div>
  <div class="relative w-full max-w-sm rounded-lg bg-white p-4 shadow-xl">
    <h3 class="text-lg font-semibold" x-text="title"></h3>
    <p class="mt-1 text-sm text-gray-600" x-text="message"></p>
    <div class="mt-4 flex justify-end gap-2">
      <button @click="open = false" class="btn-secondary">Cancel</button>
      <button @click="onConfirm?.(); open = false" class="btn-danger">Confirm</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/base.html && git commit -m "shell: right-side drawer for details, centered modal for confirms"
```

---

### Task 6: Update Global Toast

**Files:**
- Modify: `app/templates/htmx/base.html` — toast section

- [ ] **Step 1: Compact toast notification**

Update toast to match design system:

```html
<div x-show="$store.toast.show"
     x-transition:enter="transition ease-out duration-200"
     x-transition:enter-start="opacity-0 translate-y-2"
     x-transition:enter-end="opacity-100 translate-y-0"
     class="fixed bottom-4 right-4 z-50 rounded-lg px-4 py-2.5 text-sm font-medium shadow-lg"
     :class="{
       'bg-green-600 text-white': $store.toast.type === 'success',
       'bg-red-600 text-white': $store.toast.type === 'error',
       'bg-gray-800 text-white': $store.toast.type === 'info'
     }"
     x-text="$store.toast.message">
</div>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/base.html && git commit -m "shell: compact toast notifications with status colors"
```

---

### Task 7: Final Verification & Deploy

- [ ] **Step 1: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -20
```

- [ ] **Step 2: Build frontend**

```bash
cd /root/availai && npm run build 2>&1 | tail -10
```

- [ ] **Step 3: Deploy and visual test**

```bash
git push origin main
docker compose up -d --build
docker compose logs app --tail 10
```

Verify in browser:
- Sidebar: compact, sectioned, collapse works
- Topbar: 48px, breadcrumb + search + user
- Full-width content area
- Drawer opens from right on detail view click
- Mobile: hamburger + bottom nav (resize browser to test)
- Toast: appears on actions with correct colors
