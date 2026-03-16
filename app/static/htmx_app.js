/**
 * HTMX + Alpine.js bootstrap — entry point for the AvailAI frontend.
 * Loaded when USE_HTMX=true. Replaces app.js + crm.js.
 *
 * What it does: Registers all Alpine.js plugins and HTMX extensions,
 *   sets up global Alpine stores (sidebar, toast, preferences), and
 *   configures HTMX defaults.
 * What calls it: Vite bundles this as the main entry point; loaded by base.html.
 * Depends on: htmx.org, alpinejs, all @alpinejs/* plugins, all htmx-ext-* packages.
 */

// ── Core ─────────────────────────────────────────────────────
import htmx from 'htmx.org';
import Alpine from 'alpinejs';

// ── Alpine.js Official Plugins (all 9) ───────────────────────
// Focus (replaces deprecated @alpinejs/trap) — focus management & trapping for modals/drawers
import focus from '@alpinejs/focus';
// Persist — saves Alpine state to localStorage across page loads
import persist from '@alpinejs/persist';
// Intersect — Intersection Observer wrapper for lazy-load & infinite scroll
import intersect from '@alpinejs/intersect';
// Collapse — smooth expand/collapse animations
import collapse from '@alpinejs/collapse';
// Morph — DOM morphing that preserves Alpine + browser state
import morph from '@alpinejs/morph';
// Mask — auto-format text inputs as user types (part numbers, phones)
import mask from '@alpinejs/mask';
// Sort — drag-and-drop reordering
import sort from '@alpinejs/sort';
// Anchor — position elements relative to other elements (dropdowns, tooltips)
import anchor from '@alpinejs/anchor';
// Resize — react to element resize events
import resize from '@alpinejs/resize';

// ── HTMX Extensions ─────────────────────────────────────────
// Alpine-morph: uses Alpine's morph plugin as HTMX swap strategy (preserves Alpine state)
import 'htmx-ext-alpine-morph';
// Preload: prefetch content on mouseover for faster navigation
import 'htmx-ext-preload';
// Response-targets: route different HTTP status codes to different target elements
import 'htmx-ext-response-targets';
// Loading-states: add CSS classes/attributes during HTMX requests (spinners, disabled)
import 'htmx-ext-loading-states';
// Class-tools: timed addition/removal of CSS classes (flash highlights, temp notifications)
import 'htmx-ext-class-tools';
// Head-support: merge <head> content (title, meta, css) on HTMX page navigations
import 'htmx-ext-head-support';
// Multi-swap: swap multiple elements from a single HTMX response
import 'htmx-ext-multi-swap';
// SSE: Server-Sent Events for real-time updates (sourcing progress, RFQ status)
import 'htmx-ext-sse';
// WS: WebSocket support with auto-reconnect (real-time notifications)
import 'htmx-ext-ws';
// JSON-enc: encode request body as JSON instead of form-encoded
import 'htmx-ext-json-enc';
// Path-params: use path parameters in hx-get/hx-post URLs from element data
import 'htmx-ext-path-params';
// Remove-me: auto-remove elements after a timeout (flash messages, temp alerts)
import 'htmx-ext-remove-me';
// Restored: trigger events when back-button restores a page from cache
import 'htmx-ext-restored';
// Debug: logs all HTMX events to console (dev only — enabled per-element with hx-ext="debug")
import 'htmx-ext-debug';
// Idiomorph: smart DOM morphing algorithm by HTMX team (alternative swap strategy)
import 'idiomorph';
import 'idiomorph/dist/idiomorph-ext.esm.js';

// ── Styles ───────────────────────────────────────────────────
import './styles.css';
import './htmx_mobile.css';

// ── Register all Alpine plugins ──────────────────────────────
// Order matters: register plugins BEFORE Alpine.start()
Alpine.plugin(focus);      // x-trap (backwards compat) + x-focus
Alpine.plugin(persist);    // $persist
Alpine.plugin(intersect);  // x-intersect
Alpine.plugin(collapse);   // x-collapse
Alpine.plugin(morph);      // Alpine.morph()
Alpine.plugin(mask);       // x-mask
Alpine.plugin(sort);       // x-sort
Alpine.plugin(anchor);     // x-anchor
Alpine.plugin(resize);     // x-resize

// ── Expose globals ───────────────────────────────────────────
window.htmx = htmx;
window.Alpine = Alpine;

// ── Global Alpine stores ─────────────────────────────────────
Alpine.store('sidebar', {
    open: true,
    collapsed: Alpine.$persist(false).as('avail_sidebar_collapsed'),
});

Alpine.store('toast', { message: '', type: 'info', show: false });

Alpine.store('preferences', Alpine.$persist({
    resultsPerPage: 25,
    defaultView: 'requisitions',
    compactTables: false,
}).as('avail_preferences'));

// ── HTMX config ─────────────────────────────────────────────
htmx.config.defaultSwapStyle = 'innerHTML';
htmx.config.historyCacheSize = 10;
htmx.config.selfRequestsOnly = true;
htmx.config.timeout = 15000;  // 15s timeout — prevents requests from hanging forever

// ── Derive currentView from URL path ────────────────────────
function _viewFromPath(path) {
    if (/\/buy-plans(\/|$)/.test(path)) return 'buy-plans';
    if (/\/quotes(\/|$)/.test(path)) return 'quotes';
    if (/\/prospecting(\/|$)/.test(path)) return 'prospecting';
    if (/\/proactive(\/|$)/.test(path)) return 'proactive';
    if (/\/strategic(\/|$)/.test(path)) return 'strategic';
    if (/\/settings(\/|$)/.test(path)) return 'settings';
    if (/\/my-vendors(\/|$)/.test(path)) return 'my-vendors';
    if (/\/vendors(\/|$)/.test(path)) return 'vendors';
    if (/\/companies(\/|$)/.test(path)) return 'companies';
    if (/\/search(\/|$)/.test(path)) return 'search';
    if (/\/tasks(\/|$)/.test(path)) return 'tasks';
    if (/\/requisitions(\/|$)/.test(path)) return 'requisitions';
    return 'requisitions';
}

function _syncSidebarToUrl() {
    var body = document.body;
    if (body && body._x_dataStack) {
        body._x_dataStack[0].currentView = _viewFromPath(window.location.pathname);
    }
}

// Sync sidebar on browser back/forward
window.addEventListener('popstate', function () {
    _syncSidebarToUrl();
});

// Sync sidebar after HTMX pushes a new URL (covers all HTMX navigations)
document.body.addEventListener('htmx:pushedIntoHistory', function () {
    _syncSidebarToUrl();
});

// After HTMX restores a cached page on back/forward, re-sync sidebar
document.body.addEventListener('htmx:historyRestore', function () {
    _syncSidebarToUrl();
});

// ── HTMX error handler — show toast on failed requests ──────
htmx.on('htmx:responseError', (evt) => {
    Alpine.store('toast').message = 'Request failed. Please try again.';
    Alpine.store('toast').type = 'error';
    Alpine.store('toast').show = true;
});

// ── Clear stuck loading/swapping states after errors or timeouts ──
htmx.on('htmx:timeout', (evt) => {
    Alpine.store('toast').message = 'Request timed out. Please try again.';
    Alpine.store('toast').type = 'error';
    Alpine.store('toast').show = true;
});

// Safety net: after ANY request ends (success, error, or abort), force-clear
// stuck CSS classes that can freeze the UI (pointer-events:none, opacity:0).
htmx.on('htmx:afterRequest', function(evt) {
    var elt = evt.detail.elt;
    if (elt) elt.classList.remove('htmx-request', 'htmx-swapping');
});
htmx.on('htmx:sendError', function(evt) {
    var elt = evt.detail.elt;
    if (elt) elt.classList.remove('htmx-request', 'htmx-swapping');
    Alpine.store('toast').message = 'Network error. Check your connection.';
    Alpine.store('toast').type = 'error';
    Alpine.store('toast').show = true;
});

// ── 401 → redirect to login ─────────────────────────────────
document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.detail.xhr.status === 401) {
        window.location.href = '/auth/login';
    }
});

/**
 * sourcingProgress — Alpine component for SSE sourcing search progress.
 * Listens for SSE events from the HTMX SSE extension and updates the
 * per-source progress UI (count, elapsed time, status icon, progress bar).
 * On search-complete, loads the full sourcing results via HTMX ajax.
 *
 * Called by: partials/sourcing/search_progress.html
 * Depends on: htmx SSE extension, tpl-icon-check/tpl-icon-fail templates in base.html
 */
Alpine.data('sourcingProgress', (requirementId, totalSources) => ({
    completed: 0,
    init() {
        document.body.addEventListener('htmx:sseMessage', (evt) => {
            if (evt.detail.type === 'source-complete') {
                this.handleSourceComplete(JSON.parse(evt.detail.data));
            }
            if (evt.detail.type === 'search-complete') {
                this.handleSearchComplete(JSON.parse(evt.detail.data));
            }
        });
    },
    handleSourceComplete(data) {
        this.completed++;
        var source = data.source.toLowerCase();
        // Update count text
        var countEl = document.getElementById('source-count-' + source + '-' + requirementId);
        if (countEl) countEl.textContent = data.count + ' results';
        // Update elapsed time text
        var timeEl = document.getElementById('source-time-' + source + '-' + requirementId);
        if (timeEl) timeEl.textContent = (data.elapsed_ms / 1000).toFixed(1) + 's';
        // Update status icon — clone from template SVGs
        var statusEl = document.getElementById('source-status-' + source + '-' + requirementId);
        if (statusEl) {
            var tplId = data.status === 'done' ? 'tpl-icon-check' : 'tpl-icon-fail';
            var tpl = document.getElementById(tplId);
            if (tpl) {
                statusEl.replaceChildren(tpl.content.cloneNode(true));
            }
        }
        // Update progress bar width
        var pct = Math.round((this.completed / totalSources) * 100);
        var bar = document.getElementById('progress-bar-' + requirementId);
        if (bar) bar.style.width = pct + '%';
        var counter = document.getElementById('progress-count-' + requirementId);
        if (counter) counter.textContent = this.completed + ' / ' + totalSources + ' complete';
    },
    handleSearchComplete(data) {
        setTimeout(function() {
            htmx.ajax('GET', '/v2/partials/sourcing/' + requirementId, '#main-content');
        }, 500);
    }
}));

// ── Page-level loading bar for navigation ──────────────────
// Shows a slim progress bar at the top when navigating between pages.
htmx.on('htmx:beforeRequest', function(evt) {
    // Only show for #main-content targeted requests (page navigation)
    var target = evt.detail.target || evt.detail.elt;
    if (target && target.id === 'main-content' || (evt.detail.elt && evt.detail.elt.getAttribute('hx-target') === '#main-content')) {
        var bar = document.getElementById('page-loading-bar');
        if (bar) {
            bar.style.display = 'block';
            // Force reflow then animate
            bar.offsetHeight;
            bar.style.transform = 'scaleX(0.7)';
        }
    }
});
htmx.on('htmx:afterSwap', function(evt) {
    var bar = document.getElementById('page-loading-bar');
    if (bar) {
        bar.style.transform = 'scaleX(1)';
        setTimeout(function() {
            bar.style.display = 'none';
            bar.style.transform = 'scaleX(0)';
        }, 200);
    }
    // Safety: always reset body overflow after page navigation
    // (prevents stuck overflow:hidden from lead-drawer or modal)
    document.body.style.overflow = '';
});

// ── Keyboard shortcuts ─────────────────────────────────────
// Cmd+K / Ctrl+K → focus global search
document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        var searchInput = document.querySelector('#global-search-results')?.previousElementSibling?.querySelector('input[type="search"]')
            || document.querySelector('input[name="q"]');
        if (searchInput) searchInput.focus();
    }
    // Escape → close modal or drawer
    if (e.key === 'Escape') {
        var drawer = document.getElementById('lead-drawer');
        if (drawer && drawer.dataset.open === 'true') {
            drawer.dataset.open = 'false';
        }
    }
});

Alpine.start();
