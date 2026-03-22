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

Alpine.store('errorLog', { entries: [] });
window.onerror = function(msg, src, line, col) {
    var log = Alpine.store('errorLog').entries;
    log.push({ msg: String(msg), src: src, line: line, col: col, ts: new Date().toISOString() });
    if (log.length > 10) log.shift();
};
window.onunhandledrejection = function(e) {
    var log = Alpine.store('errorLog').entries;
    log.push({ msg: String(e.reason), ts: new Date().toISOString() });
    if (log.length > 10) log.shift();
};

// ── Network log capture for trouble tickets ──────────────────
Alpine.store('networkLog', { entries: [] });

htmx.on('htmx:afterRequest', function(evt) {
    var log = Alpine.store('networkLog').entries;
    log.push({
        url: evt.detail.pathInfo.requestPath,
        method: evt.detail.requestConfig.verb.toUpperCase(),
        status: evt.detail.xhr.status,
        ts: new Date().toISOString()
    });
    if (log.length > 10) log.shift();
});

Alpine.store('shortlist', {
    items: [],
    toggle(item) {
        const key = item.vendor_name + ':' + item.mpn;
        const idx = this.items.findIndex(i => (i.vendor_name + ':' + i.mpn) === key);
        if (idx >= 0) {
            this.items.splice(idx, 1);
        } else {
            this.items.push(item);
        }
    },
    has(vendorName, mpn) {
        const key = vendorName + ':' + mpn;
        return this.items.some(i => (i.vendor_name + ':' + i.mpn) === key);
    },
    clear() { this.items = []; },
    get count() { return this.items.length; },
});

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
        evt.detail.shouldSwap = false;
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

/* Faceted materials search — Alpine.js component.
 * Manages commodity, sub-filters, search query, pagination.
 * URL is the canonical source of truth (back button, deep links work).
 */
Alpine.data('materialsFilter', () => ({
  commodity: '',
  subFilters: {},
  q: '',
  page: 0,
  drawerOpen: false,
  _onPopstate: null,

  get commodityDisplayName() {
    return this.commodity ? this.commodity.replace(/_/g, ' ').replace(/(^|\s)\S/g, l => l.toUpperCase()) : '';
  },

  get activeFilterCount() {
    let count = 0;
    for (const [key, val] of Object.entries(this.subFilters)) {
      if (Array.isArray(val)) count += val.length;
      else if (val !== '' && val !== null) count += 1;
    }
    return count;
  },

  init() {
    this.syncFromURL();
    this._onPopstate = () => this.syncFromURL();
    window.addEventListener('popstate', this._onPopstate);
  },

  destroy() {
    if (this._onPopstate) {
      window.removeEventListener('popstate', this._onPopstate);
    }
  },

  syncFromURL() {
    try {
      const params = new URLSearchParams(window.location.search);
      this.commodity = params.get('commodity') || '';
      this.q = params.get('q') || '';
      const pageVal = parseInt(params.get('page') || '0', 10);
      this.page = isNaN(pageVal) ? 0 : pageVal;
      this.subFilters = {};
      for (const [key, val] of params.entries()) {
        if (key.startsWith('sf_')) {
          const specKey = key.slice(3);
          try {
            if (specKey.endsWith('_min') || specKey.endsWith('_max')) {
              const num = parseFloat(val);
              if (!isNaN(num)) {
                this.subFilters[specKey] = num;
              }
            } else {
              const items = val.split(',').filter(s => s !== '');
              if (items.length > 0) {
                this.subFilters[specKey] = items;
              }
            }
          } catch (e) {
            // Ignore unparseable sf_ param
          }
        }
      }
    } catch (e) {
      // Broken URL — reset to defaults
      this.commodity = '';
      this.q = '';
      this.page = 0;
      this.subFilters = {};
    }
  },

  pushURL(push = false) {
    const params = new URLSearchParams();
    if (this.commodity) params.set('commodity', this.commodity);
    if (this.q) params.set('q', this.q);
    if (this.page > 0) params.set('page', this.page);
    for (const [key, val] of Object.entries(this.subFilters)) {
      if (Array.isArray(val) && val.length > 0) {
        params.set('sf_' + key, val.join(','));
      } else if (typeof val === 'number' && !isNaN(val)) {
        params.set('sf_' + key, val);
      }
    }
    const search = params.toString();
    const url = window.location.pathname + (search ? '?' + search : '');
    const method = push ? 'pushState' : 'replaceState';
    history[method]({}, '', url);
  },

  selectCommodity(commodity) {
    this.commodity = commodity || '';
    this.subFilters = {};
    document.body.dispatchEvent(new CustomEvent('commodity-changed'));
    this.applyFilters();
  },

  toggleFilter(specKey, value) {
    if (!this.subFilters[specKey]) {
      this.subFilters[specKey] = [value];
    } else {
      const idx = this.subFilters[specKey].indexOf(value);
      if (idx >= 0) {
        this.subFilters[specKey].splice(idx, 1);
        if (this.subFilters[specKey].length === 0) {
          delete this.subFilters[specKey];
        }
      } else {
        this.subFilters[specKey].push(value);
      }
    }
    if (window.innerWidth >= 1024) {
      this.applyFilters();
    }
  },

  setRange(specKey, bound, value) {
    const key = specKey + '_' + bound;
    if (value === '' || value === null) {
      delete this.subFilters[key];
    } else {
      this.subFilters[key] = parseFloat(value);
    }
    if (window.innerWidth >= 1024) {
      this.applyFilters();
    }
  },

  removeFilter(key, val) {
    if (Array.isArray(this.subFilters[key])) {
      this.subFilters[key] = this.subFilters[key].filter(v => v !== val);
      if (this.subFilters[key].length === 0) delete this.subFilters[key];
    } else {
      delete this.subFilters[key];
    }
    this.applyFilters();
  },

  clearSubFilters() {
    this.subFilters = {};
    this.applyFilters();
  },

  applyFilters() {
    this.page = 0;
    this.pushURL();
    document.body.dispatchEvent(new CustomEvent('filters-changed'));
  },

  goToPage(newPage) {
    this.page = newPage;
    this.pushURL(true);
    document.body.dispatchEvent(new CustomEvent('filters-changed'));
  },
}));

/**
 * customerPicker — Alpine.js component for customer/company typeahead selection.
 * Supports searching existing customers, selecting a site, and quick-creating
 * a new customer via the company lookup endpoint.
 *
 * Usage: x-data="customerPicker()" on a container div.
 * The container must include a <div data-lookup-result></div> for lookup results.
 *
 * Called by: requisitions/unified_modal.html
 * Depends on: /api/companies/typeahead, /v2/partials/companies/lookup
 */
Alpine.data('customerPicker', () => ({
    companies: [],
    query: '',
    open: false,
    selectedSiteId: '',
    selectedName: '',
    addNew: false,
    newName: '',
    newLocation: '',
    lookingUp: false,
    init() {
        fetch('/api/companies/typeahead')
            .then(r => r.json())
            .then(data => { this.companies = data; })
            .catch(() => {});
        // Listen for customer-created event from quick-create
        document.addEventListener('customer-created', (e) => {
            this.selectById(e.detail.siteId, e.detail.displayName);
            // Refresh typeahead so new company appears in future searches
            fetch('/api/companies/typeahead')
                .then(r => r.json())
                .then(data => { this.companies = data; })
                .catch(() => {});
        });
    },
    get filtered() {
        if (!this.query.trim()) return this.companies.slice(0, 20);
        const q = this.query.toLowerCase();
        return this.companies.filter(c => c.name.toLowerCase().includes(q)).slice(0, 20);
    },
    select(company, site) {
        this.selectedSiteId = site.id;
        this.selectedName = company.name + ' \u2014 ' + site.site_name;
        this.open = false;
        this.query = '';
        this.addNew = false;
    },
    selectById(siteId, displayName) {
        this.selectedSiteId = siteId;
        this.selectedName = displayName;
        this.addNew = false;
    },
    clear() {
        this.selectedSiteId = '';
        this.selectedName = '';
        this.query = '';
    },
    async lookupCompany() {
        this.lookingUp = true;
        // Use data-lookup-result within this component's root element to avoid
        // global ID collisions when multiple pickers exist on the same page.
        const resultEl = this.$el.querySelector('[data-lookup-result]');
        try {
            const formData = new FormData();
            formData.append('company_name', this.newName);
            formData.append('location', this.newLocation);
            const resp = await fetch('/v2/partials/companies/lookup', { method: 'POST', body: formData });
            // Server HTML is trusted (same-origin, auth-protected endpoint)
            resultEl.replaceChildren();
            resultEl.insertAdjacentHTML('afterbegin', await resp.text());
            htmx.process(resultEl);
        } catch (e) {
            resultEl.textContent = 'Lookup failed. Try again.';
            resultEl.classList.add('text-xs', 'text-rose-500');
        }
        this.lookingUp = false;
    }
}));

Alpine.data('unifiedReqModal', () => ({
    // Metadata
    reqName: '',
    customerSiteId: '',
    customerName: '',
    deadline: '',
    urgency: 'normal',
    // Input mode
    inputMode: 'paste',
    rawText: '',
    // State
    parsed: false,
    parsing: false,
    saving: false,
    parseError: '',
    parts: [],
    showAllColumns: false,
    init() {
        // No-op: modal opens fresh each time
    },
    get errorCount() {
        return this.parts.filter(p => p.primary_mpn && !p.manufacturer).length;
    },
    get validCount() {
        return this.parts.filter(p => p.primary_mpn && p.manufacturer).length;
    },
    get hasErrors() {
        return this.errorCount > 0;
    },
    addBlankPart() {
        this.parts.push({
            _id: Date.now() + Math.random(),
            primary_mpn: '',
            manufacturer: '',
            target_qty: 1,
            brand: '',
            condition: 'new',
            target_price: '',
            customer_pn: '',
            date_codes: '',
            packaging: '',
            firmware: '',
            hardware_codes: '',
            need_by_date: '',
            sale_notes: '',
            substitutes: [],
        });
    },
    removePart(idx) {
        this.parts.splice(idx, 1);
    },
    resetParse() {
        this.parsed = false;
        this.parts = [];
        this.parseError = '';
    },
    async parseWithAI() {
        this.parsing = true;
        this.parseError = '';
        try {
            const formData = new FormData();
            formData.append('name', this.reqName || 'Untitled');
            formData.append('raw_text', this.rawText);
            formData.append('customer_name', this.customerName || '');
            formData.append('customer_site_id', this.customerSiteId || '');
            formData.append('deadline', this.deadline || '');
            formData.append('urgency', this.urgency || 'normal');
            if (this.inputMode === 'upload' && this.$refs.fileInput?.files?.[0]) {
                formData.append('file', this.$refs.fileInput.files[0]);
            }
            const resp = await fetch('/v2/partials/requisitions/import-parse?format=json', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json();
            if (data.error) {
                this.parseError = data.error;
            } else {
                this.parts = (data.requirements || []).map((r, i) => ({
                    _id: i + Date.now(),
                    primary_mpn: r.primary_mpn || '',
                    manufacturer: r.manufacturer || '',
                    target_qty: r.target_qty || 1,
                    brand: r.brand || '',
                    condition: r.condition || 'new',
                    target_price: r.target_price || '',
                    customer_pn: r.customer_pn || '',
                    date_codes: r.date_codes || '',
                    packaging: r.packaging || '',
                    firmware: r.firmware || '',
                    hardware_codes: r.hardware_codes || '',
                    need_by_date: r.need_by_date || '',
                    sale_notes: r.notes || r.sale_notes || '',
                    substitutes: r.substitutes || [],
                }));
                if (data.inferred_name && !this.reqName.trim()) {
                    this.reqName = data.inferred_name;
                }
                if (data.inferred_customer && !this.customerName.trim()) {
                    this.customerName = data.inferred_customer;
                }
                this.parsed = true;
                if (this.parts.length === 0) {
                    this.parseError = 'No parts could be extracted. Try a different format.';
                    this.parsed = false;
                }
            }
        } catch (e) {
            this.parseError = 'Parse failed. Please try again.';
        }
        this.parsing = false;
    },
}));

Alpine.start();
