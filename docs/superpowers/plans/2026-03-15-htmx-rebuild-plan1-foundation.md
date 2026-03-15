# Plan 1: Foundation — Brand, Build Pipeline, App Shell

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CDN-loaded Tailwind/HTMX/Alpine with Vite-bundled assets, apply brand color palette from AVAIL logo, rebuild base template with collapsible sidebar, topbar, and shared components.

**Architecture:** Tailwind CSS built locally via PostCSS + Vite. Brand palette defined in `tailwind.config.js`. Base template updated to use brand colors, AVAIL logo, collapsible sidebar, and OOB breadcrumb topbar. All shared partials updated to use brand tokens.

**Tech Stack:** Tailwind CSS 3.x, PostCSS, Vite, HTMX 2.x, Alpine.js 3.x, @alpinejs/trap, Jinja2

**Spec:** `docs/superpowers/specs/2026-03-15-htmx-frontend-rebuild-design.md` (Sections 1, 2)

---

## Task 1: Install Tailwind + PostCSS + Alpine Trap

**Files:**
- Modify: `package.json`
- Create: `tailwind.config.js`
- Create: `postcss.config.js`
- Create: `app/static/styles.css`

- [ ] **Step 1: Install npm dependencies**

```bash
cd /root/availai && npm install tailwindcss@^3 postcss autoprefixer @alpinejs/trap@^3
```

- [ ] **Step 2: Create `tailwind.config.js`**

```js
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./app/templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f0f4f8',
          100: '#dce4ed',
          200: '#b7c7d8',
          300: '#8b9daf',
          400: '#6a8bad',
          500: '#3d6895',
          600: '#345a82',
          700: '#2b4c6e',
          800: '#1e3a56',
          900: '#142a40',
        }
      }
    }
  },
  plugins: [],
}
```

- [ ] **Step 3: Create `postcss.config.js`**

```js
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
```

- [ ] **Step 4: Create `app/static/styles.css`**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

- [ ] **Step 5: Update `vite.config.js` — add styles.css as input**

In `rollupOptions.input`, add:
```js
styles: resolve(__dirname, "app/static/styles.css"),
```

- [ ] **Step 6: Verify Vite build works**

```bash
cd /root/availai && npm run build
```

Expected: Build succeeds, `app/static/dist/` contains hashed CSS file with Tailwind utilities.

- [ ] **Step 7: Commit**

```bash
git add tailwind.config.js postcss.config.js app/static/styles.css vite.config.js package.json package-lock.json
git commit -m "feat: add Tailwind local build with brand color palette"
```

---

## Task 2: Update htmx_app.js — Add Alpine Trap Plugin

**Files:**
- Modify: `app/static/htmx_app.js`

- [ ] **Step 1: Update htmx_app.js**

Add Alpine trap import and `collapsed` to sidebar store. Add 401 redirect handler. The file should become:

```js
import htmx from 'htmx.org';
import Alpine from 'alpinejs';
import trap from '@alpinejs/trap';
import './styles.css';
import './htmx_mobile.css';

Alpine.plugin(trap);

window.htmx = htmx;
window.Alpine = Alpine;

// Global Alpine stores
Alpine.store('sidebar', { open: true, collapsed: false, active: '' });
Alpine.store('toast', { message: '', type: 'info', show: false });

// HTMX config
htmx.config.defaultSwapStyle = 'innerHTML';
htmx.config.historyCacheSize = 0;
htmx.config.selfRequestsOnly = true;

// HTMX error handler — show toast on failed requests
htmx.on('htmx:responseError', (evt) => {
    Alpine.store('toast').message = 'Request failed. Please try again.';
    Alpine.store('toast').type = 'error';
    Alpine.store('toast').show = true;
});

// 401 → redirect to login
document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.detail.xhr.status === 401) {
        window.location.href = '/auth/login';
    }
});

Alpine.start();
```

- [ ] **Step 2: Rebuild and verify**

```bash
cd /root/availai && npm run build
```

Expected: Build succeeds. JS bundle includes Alpine trap and CSS imports.

- [ ] **Step 3: Commit**

```bash
git add app/static/htmx_app.js
git commit -m "feat: add Alpine trap plugin, CSS imports, 401 handler to htmx_app.js"
```

---

## Task 3: Update Base Template — Remove CDN, Use Vite Assets

**Files:**
- Modify: `app/templates/htmx/base.html`

- [ ] **Step 1: Read current base.html**

Read `app/templates/htmx/base.html` to understand current structure.

- [ ] **Step 2: Replace CDN script tags with Vite assets**

Remove these three lines from `<head>`:
```html
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
```

Replace with Vite manifest-based asset loading. Add to `<head>`:
```html
{% set manifest = {} %}
{% set manifest_path = 'app/static/dist/.vite/manifest.json' %}
<link rel="stylesheet" href="/static/dist/{{ manifest.get('app/static/styles.css', {}).get('file', 'styles.css') }}">
<script type="module" src="/static/dist/{{ manifest.get('app/static/htmx_app.js', {}).get('file', 'htmx_app.js') }}"></script>
```

Note: The exact approach depends on how the existing Vite manifest is loaded in Jinja2. Check if there's already a manifest loader in the template context. If not, the simplest approach is to use the `url_for('static', path='dist/...')` pattern with known filenames from the build output.

- [ ] **Step 3: Keep inline critical CSS**

Keep the existing inline `<style>` block for HTMX indicators, transitions, and spinner animation — these are needed before the CSS bundle loads.

- [ ] **Step 4: Build and verify base template loads**

```bash
cd /root/availai && npm run build
```

Then verify the app still loads by checking Docker or local dev server.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/base.html
git commit -m "feat: replace CDN script tags with Vite-bundled assets in base template"
```

---

## Task 4: Apply Brand Colors to Sidebar

**Files:**
- Modify: `app/templates/partials/shared/sidebar.html`
- Modify: `app/templates/htmx/base.html` (sidebar section if inline)

- [ ] **Step 1: Read current sidebar**

Read `app/templates/partials/shared/sidebar.html` (or the sidebar section in `base.html` if it's inline).

- [ ] **Step 2: Replace gray-900 with brand-700**

Replace all sidebar background colors:
- `bg-gray-900` → `bg-brand-700`
- `border-gray-800` → `border-brand-800`
- `hover:bg-gray-800` → `hover:bg-brand-800`
- `bg-gray-800 text-white` (active state) → `bg-brand-900 text-white`
- `text-gray-300` → `text-brand-200`
- `text-gray-400` → `text-brand-300`

- [ ] **Step 3: Replace text "AvailAI" with logo image**

Replace the text logo in the sidebar header:
```html
<!-- OLD -->
<span class="text-xl font-bold text-white tracking-tight">Avail<span class="text-blue-400">AI</span></span>

<!-- NEW -->
<img src="/static/public/avail_logo.png" alt="AVAIL" class="h-10 w-auto" x-show="!$store.sidebar.collapsed">
```

- [ ] **Step 4: Add collapsible sidebar behavior**

Wrap the sidebar in Alpine reactive state using `$store.sidebar.collapsed`:
- Add toggle button at top or bottom of sidebar
- When collapsed: sidebar width shrinks to icon-only (~64px), labels hidden via `x-show="!$store.sidebar.collapsed"`, logo hidden
- When expanded: full width (~256px), labels visible, logo visible
- Transition: `transition-all duration-200`

- [ ] **Step 5: Add all 9 nav items**

Ensure sidebar has all nav items from spec:
1. Requisitions (clipboard icon)
2. Part Search (magnifying glass)
3. Buy Plans (shopping cart)
4. Section label: "Relationships"
5. Vendors (building icon)
6. Companies (people icon)
7. Quotes (document icon)
8. Prospecting (compass icon)
9. Settings (gear icon)

Each with `hx-get`, `hx-target="#main-content"`, `hx-push-url`, and `@click` to set `$store.sidebar.active`.

- [ ] **Step 6: Verify sidebar renders with brand colors**

Build and check the app loads with correct colors.

- [ ] **Step 7: Commit**

```bash
git add app/templates/partials/shared/sidebar.html app/templates/htmx/base.html
git commit -m "feat: apply brand colors to sidebar, add logo, collapsible nav, all 9 items"
```

---

## Task 5: Apply Brand Colors to Topbar + OOB Breadcrumb

**Files:**
- Modify: `app/templates/partials/shared/topbar.html`

- [ ] **Step 1: Read current topbar**

Read `app/templates/partials/shared/topbar.html`.

- [ ] **Step 2: Apply brand colors**

- Background: `bg-white`
- Border: `border-b border-brand-200`
- Search input ring: `focus:ring-brand-500 focus:border-brand-500`
- Replace any `blue-600` or `blue-500` references with `brand-500` / `brand-600`

- [ ] **Step 3: Add breadcrumb container with OOB target**

Ensure topbar has a breadcrumb div that partials can update via OOB swap:
```html
<div id="breadcrumb" class="text-sm text-gray-500">
  <span>Dashboard</span>
</div>
```

Each page partial will include:
```html
<div id="breadcrumb" hx-swap-oob="true" class="text-sm text-gray-500">
  <a href="/v2/requisitions" class="text-brand-500 hover:text-brand-600">Requisitions</a>
  <span class="mx-1">/</span>
  <span>{{ req.name }}</span>
</div>
```

- [ ] **Step 4: Add global search endpoint to router**

In `app/routers/htmx_views.py`, add:
```python
@router.get("/v2/partials/search/global", response_class=HTMLResponse)
async def global_search(request: Request, q: str = "", user: User = Depends(require_user)):
    """Global search across requisitions, companies, vendors."""
    results = {"requisitions": [], "companies": [], "vendors": []}
    if q and len(q) >= 2:
        # Search requisitions
        # Search companies
        # Search vendors
        # Limit to 5 per type
        pass
    return templates.TemplateResponse("partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q})
```

- [ ] **Step 5: Update search_results.html**

Read and update `app/templates/partials/shared/search_results.html` to:
- Group results by type (Requisitions, Companies, Vendors)
- Each result row: `hx-get` to detail partial, `hx-target="#main-content"`, `hx-push-url`
- Clicking a result closes the dropdown
- Use brand colors for links

- [ ] **Step 6: Commit**

```bash
git add app/templates/partials/shared/topbar.html app/templates/partials/shared/search_results.html app/routers/htmx_views.py
git commit -m "feat: brand colors on topbar, OOB breadcrumb, global search endpoint"
```

---

## Task 6: Update Mobile Nav + Login Page

**Files:**
- Modify: `app/templates/partials/shared/mobile_nav.html`
- Modify: `app/templates/htmx/base.html` (mobile header section)
- Modify: `app/templates/htmx/login.html`

- [ ] **Step 1: Update mobile header brand colors**

In `base.html` mobile header section:
- `bg-gray-900` → `bg-brand-700`
- `border-gray-800` → `border-brand-800`
- Replace text "AvailAI" with compact logo: `<img src="/static/public/avail_logo.png" alt="AVAIL" class="h-6 w-auto">`

- [ ] **Step 2: Update mobile sidebar overlay brand colors**

Same color replacements as desktop sidebar (Task 4 Step 2).

- [ ] **Step 3: Update mobile_nav.html**

Read and update `app/templates/partials/shared/mobile_nav.html`:
- Active item: `text-brand-500` / `border-brand-500`
- Add all 5 bottom nav items: Requisitions, Search, Buy Plans, Vendors, Companies
- Use brand colors for active state

- [ ] **Step 4: Update login page with logo**

Update `app/templates/htmx/login.html`:
- Replace text "AvailAI" heading with: `<img src="/static/public/avail_logo_white_bg.png" alt="AVAIL Opportunity Management" class="h-16 w-auto mx-auto">`
- Update button color: `bg-white` stays (Microsoft sign-in button)
- Update background: `bg-brand-900` (replaces `bg-gray-900`)
- Update card: `bg-brand-800` (replaces `bg-gray-800`), `border-brand-700`
- Remove CDN Tailwind script tag, add Vite CSS link

- [ ] **Step 5: Commit**

```bash
git add app/templates/partials/shared/mobile_nav.html app/templates/htmx/base.html app/templates/htmx/login.html
git commit -m "feat: brand colors on mobile nav and login page, AVAIL logo"
```

---

## Task 7: Update Shared Components — Modal, Toast, Pagination, Empty State, Enrich Button

**Files:**
- Modify: `app/templates/partials/shared/modal.html`
- Modify: `app/templates/partials/shared/toast.html`
- Modify: `app/templates/partials/shared/pagination.html`
- Modify: `app/templates/partials/shared/empty_state.html`
- Modify: `app/templates/partials/shared/enrich_button.html`

- [ ] **Step 1: Update modal.html**

Read current `modal.html`. Ensure it has:
- `x-trap.noscroll` for focus trapping (requires @alpinejs/trap)
- `@open-modal.window` / `@close-modal.window` event listeners
- `@keydown.escape.window` closes
- Backdrop: `bg-black/50`
- Modal box: `bg-white rounded-lg shadow-xl`
- `#modal-content` as HTMX swap target
- Full-screen on mobile: add responsive classes

- [ ] **Step 2: Update toast.html**

Read current `toast.html`. Ensure it has:
- Color-coded backgrounds: success=`bg-emerald-50 text-emerald-700`, error=`bg-rose-50 text-rose-700`, info=`bg-brand-100 text-brand-600`
- Auto-dismiss via `$watch('$store.toast.show', v => v && setTimeout(() => $store.toast.show = false, 4000))`
- `x-transition` for smooth enter/exit
- Fixed position: `fixed top-4 right-4 z-50` (desktop), `fixed top-4 left-4 right-4 z-50` (mobile)

- [ ] **Step 3: Update pagination.html**

Read current `pagination.html`. Ensure buttons use brand colors:
- Active/hover: `bg-brand-500 text-white` / `hover:bg-brand-600`
- Disabled: `bg-gray-100 text-gray-500 cursor-not-allowed`

- [ ] **Step 4: Update empty_state.html**

Read current. Replace any `blue-600` with `brand-500` on CTA button.

- [ ] **Step 5: Update enrich_button.html**

Read current. Replace any `blue-600` with `brand-500`. Ensure it uses `htmx-indicator` for spinner.

- [ ] **Step 6: Commit**

```bash
git add app/templates/partials/shared/
git commit -m "feat: apply brand colors to all shared components (modal, toast, pagination, etc.)"
```

---

## Task 8: Update htmx_mobile.css with Brand Colors

**Files:**
- Modify: `app/static/htmx_mobile.css`

- [ ] **Step 1: Read current htmx_mobile.css**

Read `app/static/htmx_mobile.css`.

- [ ] **Step 2: Replace any hardcoded colors with brand equivalents**

Search for any hex colors or Tailwind color references and update to brand palette. Since this is raw CSS (not Tailwind utilities), use the hex values directly:
- Primary blue → `#3d6895` (brand-500)
- Dark backgrounds → `#2b4c6e` (brand-700)
- Borders → `#b7c7d8` (brand-200)

- [ ] **Step 3: Rebuild**

```bash
cd /root/availai && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_mobile.css
git commit -m "feat: update mobile CSS with brand color palette"
```

---

## Task 9: Write Tests for Foundation

**Files:**
- Modify: `tests/test_htmx_foundation.py`

- [ ] **Step 1: Read current test file**

Read `tests/test_htmx_foundation.py`.

- [ ] **Step 2: Add/update tests**

Ensure tests cover:
- `test_base_template_no_cdn` — GET `/v2/requisitions` response does NOT contain `cdn.tailwindcss.com` or `unpkg.com`
- `test_base_template_has_vite_assets` — response contains `/static/dist/` asset references
- `test_sidebar_has_brand_colors` — response contains `brand-700` or equivalent classes
- `test_sidebar_has_logo` — response contains `avail_logo.png`
- `test_sidebar_has_all_nav_items` — response contains all 9 nav labels
- `test_login_page_has_logo` — GET `/auth/htmx-login` contains `avail_logo`
- `test_topbar_has_breadcrumb` — response contains `id="breadcrumb"`
- `test_global_search_endpoint` — GET `/v2/partials/search/global?q=test` returns 200

- [ ] **Step 3: Run tests**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_foundation.py -v
```

Expected: All tests pass.

- [ ] **Step 4: Run full test suite to check for regressions**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: No new failures.

- [ ] **Step 5: Commit**

```bash
git add tests/test_htmx_foundation.py
git commit -m "test: foundation tests — no CDN, brand colors, logo, nav items, breadcrumb"
```

---

## Task 10: Final Verification + Deploy

- [ ] **Step 1: Full Vite build**

```bash
cd /root/availai && npm run build
```

- [ ] **Step 2: Run full test suite**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

- [ ] **Step 3: Verify coverage hasn't dropped**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

- [ ] **Step 4: Deploy**

```bash
cd /root/availai && git push origin main && docker compose up -d --build && sleep 5 && docker compose logs --tail=20 app
```

Check logs for errors. Hard refresh browser to verify brand colors and logo appear.
