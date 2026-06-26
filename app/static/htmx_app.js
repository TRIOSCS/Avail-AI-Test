/**
 * HTMX + Alpine.js bootstrap — entry point for the AvailAI frontend.
 * Loaded when USE_HTMX=true. Replaces app.js + crm.js.
 *
 * What it does: Registers all Alpine.js plugins and HTMX extensions,
 *   sets up global Alpine stores (toast, errorLog, networkLog), and
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

// ── Local modules ────────────────────────────────────────────
// Pure geometry math for the resizable/movable modal wrapper (see base.html).
import { resizeGeometry, moveGeometry, clampToViewport } from './modal_geometry.js';

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

// ── Shared helpers ───────────────────────────────────────────
// starlette_csrf sets the csrftoken cookie on every response and requires the
// matching x-csrftoken header on POST/PUT/PATCH/DELETE.
function csrfToken() {
    return document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
}

// Set the global toast store ({ message, type, show }) in one call. `show` is a
// boolean field, not a method.
function showToast(message, type = 'info') {
    const toast = Alpine.store('toast');
    toast.message = message;
    toast.type = type;
    toast.show = true;
}

// Append to a capped (last-10) Alpine store log used by trouble tickets.
function pushCappedLog(storeName, entry) {
    const log = Alpine.store(storeName).entries;
    log.push({ ...entry, ts: new Date().toISOString() });
    if (log.length > 10) log.shift();
}

// ── Global Alpine stores ─────────────────────────────────────
Alpine.store('toast', { message: '', type: 'info', show: false });

Alpine.store('errorLog', { entries: [] });
window.onerror = function(msg, src, line, col) {
    pushCappedLog('errorLog', { msg: String(msg), src: src, line: line, col: col });
};
window.onunhandledrejection = function(e) {
    pushCappedLog('errorLog', { msg: String(e.reason) });
};

// Tee console.error/console.warn into the capped errorLog store so a trouble
// report carries the app's own logged diagnostics (e.g. '[outreach-log] failed'),
// not just uncaught errors. Originals still fire — logging never breaks logging.
['error', 'warn'].forEach(function(level) {
    const orig = console[level].bind(console);
    console[level] = function(...args) {
        try {
            pushCappedLog('errorLog', {
                level: level,
                msg: args.map(function(a) {
                    return (a instanceof Error) ? (a.stack || a.message) : String(a);
                }).join(' ').slice(0, 1000),
            });
        } catch (_) { /* never let logging break logging */ }
        orig(...args);
    };
});

// ── Network log capture for trouble tickets ──────────────────
Alpine.store('networkLog', { entries: [] });

htmx.on('htmx:afterRequest', function(evt) {
    pushCappedLog('networkLog', {
        url: evt.detail.pathInfo.requestPath,
        method: evt.detail.requestConfig.verb.toUpperCase(),
        status: evt.detail.xhr.status,
    });
});

// ── Trouble-ticket capture & reporting ───────────────────────
// Recent HTMX-pushed URLs — a breadcrumb trail for bug repro. Capped at 8.
window._ttNavHistory = [];
document.body.addEventListener('htmx:pushedIntoHistory', function(e) {
    const path = e && e.detail && e.detail.path;
    if (!path) return;
    window._ttNavHistory.push({ path: path, ts: new Date().toISOString() });
    if (window._ttNavHistory.length > 8) window._ttNavHistory.shift();
});

const TT_MAX_B64 = 1950000; // margin under the server's 2MB screenshot limit

// Reject if `p` doesn't settle within `ms`; clears the timer on settle so no
// stray timer/unhandled-rejection lingers after capture succeeds.
function _ttWithTimeout(p, ms) {
    return new Promise(function(resolve, reject) {
        const id = setTimeout(function() { reject(new Error('screenshot timeout')); }, ms);
        p.then(
            function(v) { clearTimeout(id); resolve(v); },
            function(e) { clearTimeout(id); reject(e); }
        );
    });
}

// Capture the underlying page as a PNG data URL. Returns Promise<string|null>;
// null on any failure so the report form always opens. The screenshot lib is a
// lazy import() chunk — shipped only to users who actually open a report.
window.captureTroubleScreenshot = async function captureTroubleScreenshot() {
    try {
        const mod = await import('modern-screenshot');
        const domToPng = mod.domToPng;
        const ignoreSel = '#modal-content, [data-modal-root], nav[aria-label="Main navigation"], #page-loading-bar, [data-tt-ignore]';
        const baseOpts = {
            backgroundColor: '#ffffff',
            width: window.innerWidth,
            height: window.innerHeight,
            filter: function(node) {
                return !(node instanceof Element && node.closest(ignoreSel));
            },
        };
        for (const scale of [1, 0.75, 0.5]) {
            const opts = Object.assign({}, baseOpts, { scale: scale });
            const url = await _ttWithTimeout(domToPng(document.body, opts), 3000);
            if (url && url.length <= TT_MAX_B64) return url;
        }
        return null; // still too big at smallest scale — drop it, don't block submit
    } catch (err) {
        console.error('[trouble-ticket] screenshot capture failed', err);
        return null;
    }
};

// Cheap client-side context bundle for diagnosis. current_view is derived from
// the URL so it stays correct across HTMX navigation.
window.collectTroubleContext = function collectTroubleContext() {
    const meta = document.querySelector('meta[name="app-build"]');
    const m = window.location.pathname.match(/\/v2\/([^/?#]+)/);
    let navTiming = null;
    try {
        const e = performance.getEntriesByType('navigation')[0];
        if (e) navTiming = { dom_interactive: Math.round(e.domInteractive), load: Math.round(e.loadEventEnd) };
    } catch (_) { navTiming = null; }
    return {
        nav_history: (window._ttNavHistory || []).slice(),
        current_view: m ? m[1] : null,
        app_build: meta ? meta.content : null,
        timestamp: new Date().toISOString(),
        referrer: document.referrer || null,
        online: navigator.onLine,
        nav_timing: navTiming,
    };
};

// More-menu entry point: capture the page first (so neither menu nor modal is in
// the shot), then open the report modal. Double-rAF guarantees the menu has
// painted out before capture.
window.openTroubleReport = async function openTroubleReport() {
    await new Promise(function(r) {
        requestAnimationFrame(function() { requestAnimationFrame(r); });
    });
    window._ttScreenshot = await window.captureTroubleScreenshot();
    window._ttContext = window.collectTroubleContext();
    window.dispatchEvent(new CustomEvent('open-modal', { detail: { url: '/api/trouble-tickets/form' } }));
};

// Submit the trouble report. Called from the form's @click as a single
// expression (window.submitTroubleReport($data)) — Alpine's evaluator rejects
// multi-statement var/if/return bodies, so the logic lives here. `data` is the
// form's reactive $data so toggling data.submitting drives the button state.
window.submitTroubleReport = function submitTroubleReport(data) {
    const descEl = document.getElementById('tr-description');
    const desc = descEl ? descEl.value.trim() : '';
    if (!desc) return;
    data.submitting = true;
    const csrf = document.cookie.match(/csrftoken=([^;]+)/) ? RegExp.$1 : '';
    fetch('/api/trouble-tickets/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify({
            description: desc,
            screenshot: window._ttScreenshot || null,
            page_url: window.location.href,
            user_agent: navigator.userAgent,
            viewport: window.innerWidth + 'x' + window.innerHeight,
            error_log: JSON.stringify(Alpine.store('errorLog').entries),
            network_log: JSON.stringify(Alpine.store('networkLog').entries),
            auto_captured_context: window._ttContext ? JSON.stringify(window._ttContext) : null,
        }),
    }).then(function(r) {
        return r.text();
    }).then(function(html) {
        htmx.swap('#modal-content', html, { swapStyle: 'innerHTML' });
        data.submitting = false;
    }).catch(function() {
        htmx.swap('#modal-content', '<div class="p-6 text-sm text-rose-600">Something went wrong. Please try again.</div>', { swapStyle: 'innerHTML' });
        data.submitting = false;
    });
};

// Admin bulk action on selected tickets ('diagnose-bulk' | 'bulk-status'). POSTs
// the ids, toasts the outcome, and fires 'ticketsUpdated' so the list refreshes.
window.ticketBulkAction = function ticketBulkAction(kind, ids, status) {
    if (!ids || !ids.length) return Promise.resolve();
    const csrf = document.cookie.match(/csrftoken=([^;]+)/) ? RegExp.$1 : '';
    const payload = { ticket_ids: ids };
    if (status) payload.status = status;
    return fetch('/api/trouble-tickets/' + kind, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify(payload),
    }).then(function(r) {
        const t = Alpine.store('toast');
        if (r.ok) {
            t.message = (kind === 'diagnose-bulk') ? 'Diagnosis started' : 'Tickets updated';
            t.type = 'success';
        } else {
            t.message = 'Action failed (' + r.status + ')';
            t.type = 'error';
        }
        t.show = true;
        document.body.dispatchEvent(new CustomEvent('ticketsUpdated', { bubbles: true }));
    }).catch(function(err) {
        console.error('[ticket-bulk] failed', err);
        const t = Alpine.store('toast');
        t.message = 'Network error'; t.type = 'error'; t.show = true;
    });
};

Alpine.store('callOutcome', {
    show: false,
    activityId: null,
    contactName: '',
    note: '',
    chips: [
        { value: 'connected', label: 'Connected' },
        { value: 'left_message', label: 'Left message' },
        { value: 'voicemail', label: 'Voicemail' },
        { value: 'no_answer', label: 'No answer' },
    ],
    dismiss() {
        this.show = false;
        this.note = '';
    },
    submit(outcome) {
        const id = this.activityId;
        const note = this.note.trim() || null;
        this.dismiss();
        if (!outcome) return;
        const headers = { 'Content-Type': 'application/json' };
        const csrf = csrfToken();
        if (csrf) headers['x-csrftoken'] = csrf;
        fetch('/api/activity/' + id + '/call-outcome', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ outcome: outcome, note: note }),
        }).then((resp) => {
            if (resp.ok) {
                showToast('Call outcome logged', 'success');
            } else {
                console.error('[call-outcome] failed', resp.status);
                const msg = resp.status === 429
                    ? 'Outcome not saved — rate limit hit, wait a minute'
                    : 'Outcome not saved (error ' + resp.status + ')';
                showToast(msg, 'error');
            }
        }).catch((err) => {
            console.error('[call-outcome] network error', err);
            showToast('Outcome not saved — network error', 'error');
        });
    },
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

// Sightings multi-select store (reactive object, not Set)
Alpine.store('sightingSelection', {
    _map: {},
    selectedReqId: null,
    clickPending: 0,    // count of click-initiated POSTs currently in-flight
    toggle(id) {
        if (this._map[id]) { delete this._map[id]; }
        else { this._map[id] = true; }
    },
    has(id) { return !!this._map[id]; },
    clear() { this._map = {}; },
    get count() { return Object.keys(this._map).length; },
    get array() { return Object.keys(this._map).map(Number); },
});

// ── HTMX config ─────────────────────────────────────────────
htmx.config.defaultSwapStyle = 'innerHTML';
htmx.config.historyCacheSize = 10;
htmx.config.selfRequestsOnly = true;
htmx.config.timeout = 15000;  // 15s timeout — prevents requests from hanging forever

// ── CSRF token for all HTMX requests ───────────────────────
// starlette_csrf middleware requires x-csrftoken header on POST/PUT/PATCH/DELETE.
// The csrftoken cookie is set by the middleware on every response.
document.body.addEventListener('htmx:configRequest', (evt) => {
    const csrf = csrfToken();
    if (csrf) {
        evt.detail.headers['x-csrftoken'] = csrf;
    }
});

// ── Click-to-contact outreach logger (CDM contact panel) ────
// Any element with [data-outreach-log] (tel:/mailto:/Teams/WeChat links in
// customer contact panels) fires a fire-and-forget POST to
// /api/activity/outreach-initiated when clicked, logging the touch and
// bumping company/site last_activity_at. The default link navigation is NOT
// prevented — the native handler (dialer, mail client, Teams) still opens.
document.body.addEventListener('click', (evt) => {
    const el = evt.target.closest('[data-outreach-log]');
    if (!el) return;
    const d = el.dataset;
    const payload = {
        channel: d.channel,
        contact_value: d.value,
        company_id: d.companyId ? parseInt(d.companyId, 10) : null,
        customer_site_id: d.siteId ? parseInt(d.siteId, 10) : null,
        site_contact_id: d.contactId ? parseInt(d.contactId, 10) : null,
        contact_name: d.contactName || null,
        origin: 'cdm_workspace',
    };
    const headers = { 'Content-Type': 'application/json' };
    const csrf = csrfToken();
    if (csrf) headers['x-csrftoken'] = csrf;
    // Refresh the CDM account list (if on the workspace) so the logged touch
    // is immediately visible in the staleness sort/labels. This refresh is
    // SYSTEM-initiated mid-workflow: unlike a user filter change it must not
    // reset pagination, so the current offset/limit (rendered as data-* on
    // the _account_list.html header — the filter form intentionally carries
    // no offset field) is passed through explicitly. source: #cdm-filters
    // includes the current filter values, like the pagination links do.
    const refreshAccountList = () => {
        const cdmFilters = document.getElementById('cdm-filters');
        const cdmList = document.getElementById('cdm-list');
        if (!cdmFilters || !cdmList) return;
        const meta = cdmList.querySelector('[data-offset]');
        const page = meta ? '?offset=' + meta.dataset.offset + '&limit=' + meta.dataset.limit : '';
        htmx.ajax('GET', '/v2/partials/customers/account-list' + page, {
            source: '#cdm-filters',
            target: '#cdm-list',
            swap: 'innerHTML',
            indicator: '#cdm-filters .htmx-indicator',
        });
    };
    // keepalive lets the request finish even if the click navigates away.
    // The fetch is never awaited before the default link action, so the
    // call/email/Teams handler always opens — but failures must still be
    // VISIBLE: a silent 429/500 means the rep believes the touch was logged
    // while the staleness sort quietly stops reflecting their work.
    fetch('/api/activity/outreach-initiated', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload),
        keepalive: true,
    }).then(async (resp) => {
        if (!resp.ok) {
            showToast(
                resp.status === 429
                    ? 'Outreach NOT logged — rate limit hit, wait a minute'
                    : 'Outreach NOT logged (error ' + resp.status + ')',
                'error'
            );
            return;
        }
        // The POST committed — from here on any failure is a RENDERING
        // problem, not a transport one. Contain it so the .catch below (the
        // rep-facing "NOT logged" message) only ever reports genuine fetch
        // rejections; a false "NOT logged" toast invites a duplicate re-click.
        let droppedLinks = [];
        let body = {};
        try {
            body = await resp.json();
            droppedLinks = body.dropped_links || [];
        } catch (err) {
            console.error('[outreach-log] could not parse response body', err);
        }
        try {
            if (droppedLinks.length) {
                // Logged, but the server dropped stale entity links — the touch
                // exists yet won't show on this account, so don't claim success
                // (and skip the list refresh: nothing changed for this view).
                showToast(
                    'Outreach logged, but the ' + droppedLinks.join('/') +
                    ' link no longer exists — refresh the page',
                    'warning'
                );
                return;
            }
            const labels = { phone: 'Call', email: 'Email', teams: 'Teams message', wechat: 'WeChat message' };
            showToast(
                (labels[d.channel] || 'Outreach') + ' logged' + (d.contactName ? ' — ' + d.contactName : ''),
                'success'
            );
            if (payload.channel === 'phone' && body && body.id) {
                const store = Alpine.store('callOutcome');
                store.activityId = body.id;
                store.contactName = d.contactName || '';
                store.note = '';
                store.show = true;
            }
            refreshAccountList();
        } catch (err) {
            console.error('[outreach-log] post-success UI update failed', err);
        }
    }).catch((err) => {
        console.error('[outreach-log] failed', err);
        showToast('Outreach NOT logged — network error', 'error');
    });
});

// ── HTMX error handler — show toast on failed requests ──────
htmx.on('htmx:responseError', (evt) => {
    const status = evt.detail.xhr && evt.detail.xhr.status;
    if (status >= 400 && status < 500) {
        let msg = 'Request failed. Please try again.';
        try {
            const body = JSON.parse(evt.detail.xhr.responseText);
            const msg_text = body.error || body.detail;
            if (msg_text && typeof msg_text === 'string') {
                msg = msg_text;
            }
        } catch (_) { /* not JSON — use fallback */ }
        showToast(msg, 'error');
    } else {
        showToast('Request failed. Please try again.', 'error');
    }
});

// ── Server-driven toast bridge ───────────────────────────────
// HTMX dispatches a DOM event named after each HX-Trigger key. Servers emit
// {"showToast": {"message": "...", "type": "..."}} (see htmx_views.py); bridge
// it into the global $store.toast the base layout renders (htmx/base.html).
// Plain string or {message,type} both supported; type defaults to "info".
document.body.addEventListener('showToast', (evt) => {
    const d = evt.detail;
    const msg = typeof d === 'string' ? d : (d && d.message) || '';
    if (!msg) return;
    showToast(msg, (d && d.type) || 'info');
});

// Stale-response guard: HTMX swaps can arrive out of order when the user
// clicks a new row before the previous /refresh resolves. Correlate via
// X-Rendered-Req-Id and drop swaps for the wrong row.
document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.detail.target.id === 'sightings-detail') {
        const store = Alpine.store('sightingSelection');
        const reqId = evt.detail.xhr?.getResponseHeader('X-Rendered-Req-Id');
        if (reqId) {
            if (store.selectedReqId && String(store.selectedReqId) !== String(reqId)) {
                // Stale response — drop the swap. The htmx:afterRequest
                // handler owns the clickPending counter and will decrement
                // it for this completed (rejected) request.
                evt.detail.shouldSwap = false;
                return;
            }
        } else {
            console.debug('[sightings] response to #sightings-detail missing X-Rendered-Req-Id');
        }
    }
});

// Decrement the clickPending counter when a #sightings-detail request
// finishes — success, error, timeout, abort, or stale-reject all funnel
// through here. Counter (vs. bool) handles the multi-click race where a
// user clicks row A then row B before A returns: each completion
// decrements once, and SSE suppression stays active until both clear.
// Math.max(0, …) clamps in case of an unexpected double-decrement.
htmx.on('htmx:afterRequest', function(evt) {
    var target = evt.detail.target || evt.detail.elt;
    if (target && target.id === 'sightings-detail') {
        var store = Alpine.store('sightingSelection');
        store.clickPending = Math.max(0, store.clickPending - 1);
    }
});

// ── Clear stuck loading/swapping states after errors or timeouts ──
htmx.on('htmx:timeout', (evt) => {
    showToast('Request timed out. Please try again.', 'error');
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
    showToast('Network error. Check your connection.', 'error');
});

// ── 401 → redirect to login ─────────────────────────────────
document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.detail.xhr.status === 401) {
        evt.detail.shouldSwap = false;
        window.location.href = '/auth/login';
    }
});

// ── 422 validation re-renders into the modal ───────────────
// Modal forms (e.g. Add part) answer 422 with the form re-rendered carrying
// per-field error messages. htmx treats 4xx as no-swap by default — allow the
// swap ONLY for responses targeted at #modal-content so the errors render.
document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.detail.xhr.status === 422 && evt.detail.target && evt.detail.target.id === 'modal-content') {
        evt.detail.shouldSwap = true;
        evt.detail.isError = false;
    }
});

/**
 * splitPanel — Alpine.js component for resizable split-panel layout.
 * Left panel is a scrollable list; right panel is a detail view.
 * User can drag the divider to resize. Position is persisted to localStorage.
 *
 * Called by: partials/shared/split_panel.html
 * Depends on: Alpine.js
 *
 * Sets window.__availSplitPanelRegistered so the standalone
 * requisitions2.js fallback skips its duplicate registration when this
 * bundle is loaded (CRIT-FE-2).
 */
window.__availSplitPanelRegistered = true;
Alpine.data('splitPanel', (panelId, defaultPct) => ({
    leftWidth: parseInt(localStorage.getItem('avail_split_' + panelId) || defaultPct),
    _resizing: false,
    _startX: 0,
    _startWidth: 0,

    // Shared resize math for both mouse and touch drags: clamp leftWidth to 20–70%
    // based on the pointer's distance from the drag start.
    _applyDrag(clientX) {
        if (!this._resizing) return;
        const container = document.getElementById('split-' + panelId);
        if (!container) return;
        const dx = clientX - this._startX;
        const newPct = this._startWidth + (dx / container.offsetWidth) * 100;
        this.leftWidth = Math.max(20, Math.min(70, Math.round(newPct)));
    },

    startResize(e) {
        this._resizing = true;
        this._startX = e.clientX;
        this._startWidth = this.leftWidth;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        const onMove = (ev) => this._applyDrag(ev.clientX);

        const onUp = () => {
            this._resizing = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            localStorage.setItem('avail_split_' + panelId, this.leftWidth);
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    },

    startTouchResize(e) {
        const touch = e.touches[0];
        this._resizing = true;
        this._startX = touch.clientX;
        this._startWidth = this.leftWidth;

        const onTouchMove = (ev) => this._applyDrag(ev.touches[0].clientX);

        const onTouchEnd = () => {
            this._resizing = false;
            localStorage.setItem('avail_split_' + panelId, this.leftWidth);
            document.removeEventListener('touchmove', onTouchMove);
            document.removeEventListener('touchend', onTouchEnd);
        };

        document.addEventListener('touchmove', onTouchMove);
        document.addEventListener('touchend', onTouchEnd);
    }
}));

/**
 * resizableModal — global modal wrapper behavior: open/close state plus, on
 * desktop, drag-to-move and drag-to-resize (4 edges + 4 corners) with the
 * chosen size/position remembered per size-bucket.
 *
 * Bound to the single modal wrapper in htmx/base.html. Every modal loads into
 * #modal-content inside this one panel, so geometry is owned here ONCE and
 * survives HTMX content swaps (the panel persists; only #modal-content's
 * innerHTML changes). Mirrors splitPanel's raw-localStorage idiom (per-drag
 * document listeners, no permanent global handler) but uses Pointer Events +
 * setPointerCapture so an embedded iframe can't swallow drag events mid-resize.
 *
 * Desktop only (>=1024px). Below that, isDesktop is false, panelStyle() returns
 * '' and the CSS handles responsive centering / the mobile bottom-sheet layout.
 *
 * Persistence: localStorage 'avail_modal_geom' -> { lg:{w,h,l,t}, wide:{...} },
 * keyed by the two existing size buckets; clamped to the live viewport on
 * restore. Double-clicking any handle or the drag-bar resets the current bucket.
 *
 * Called by: app/templates/htmx/base.html (x-data="resizableModal()").
 * Depends on: Alpine.js, ./modal_geometry.js.
 */
const MODAL_GEOM_KEY = 'avail_modal_geom';
const MODAL_DESKTOP_MQ = '(min-width: 1024px)';

Alpine.data('resizableModal', () => ({
    open: false,
    wide: false,
    custom: false,            // true once the user has dragged/resized this bucket
    width: 0, height: 0, left: 0, top: 0,
    isDesktop: window.matchMedia(MODAL_DESKTOP_MQ).matches,
    _drag: null,
    _mq: null,
    _onMQ: null,
    _onResize: null,
    _boundMove: null,
    _boundUp: null,
    _boundCancel: null,

    get bucket() {
        return this.wide ? 'wide' : 'lg';
    },

    init() {
        this._mq = window.matchMedia(MODAL_DESKTOP_MQ);
        this._onMQ = (e) => {
            this.isDesktop = e.matches;
            if (!e.matches) this.custom = false;  // drop floating geometry on shrink to mobile
        };
        this._mq.addEventListener('change', this._onMQ);
        // Re-clamp a custom (floating) panel when the window itself shrinks, so a panel
        // sized/positioned on a larger viewport can't end up partly or fully off-screen
        // while still desktop-width. _restore() only clamps on open; this covers live resize.
        this._onResize = () => {
            if (!this.custom || !this.isDesktop) return;
            const g = clampToViewport(
                { w: this.width, h: this.height, l: this.left, t: this.top },
                window.innerWidth,
                window.innerHeight,
            );
            this.width = g.w; this.height = g.h; this.left = g.l; this.top = g.t;
        };
        window.addEventListener('resize', this._onResize);
    },

    destroy() {
        if (this._mq && this._onMQ) this._mq.removeEventListener('change', this._onMQ);
        if (this._onResize) window.removeEventListener('resize', this._onResize);
        this._teardownDrag();
    },

    // Called from @open-modal — preserves the existing {url, wide} dispatch contract.
    onOpen(detail) {
        this.wide = !!(detail && detail.wide);
        this.open = true;
        this.isDesktop = this._mq ? this._mq.matches : window.matchMedia(MODAL_DESKTOP_MQ).matches;
        this._restore();
        if (detail && detail.url) {
            htmx.ajax('GET', detail.url, { target: '#modal-content', swap: 'innerHTML', indicator: '#modal-loading' });
        }
    },

    onClose() {
        this.open = false;  // keep wide + geometry; the next open re-reads the bucket
    },

    // ── Persistence ──────────────────────────────────────────
    _readAll() {
        try {
            return JSON.parse(localStorage.getItem(MODAL_GEOM_KEY) || '{}');
        } catch {
            return {};
        }
    },

    _restore() {
        if (!this.isDesktop) {
            this.custom = false;
            return;
        }
        const saved = this._readAll()[this.bucket];
        if (saved && saved.w && saved.h) {
            const g = clampToViewport(saved, window.innerWidth, window.innerHeight);
            this.width = g.w; this.height = g.h; this.left = g.l; this.top = g.t;
            this.custom = true;
        } else {
            this.custom = false;
        }
    },

    _persist() {
        const all = this._readAll();
        all[this.bucket] = { w: this.width, h: this.height, l: this.left, t: this.top };
        localStorage.setItem(MODAL_GEOM_KEY, JSON.stringify(all));
    },

    // Seed numeric geometry from the panel's current rendered box, so the first
    // drag continues from exactly where the centered layout placed it (no jump).
    _seed() {
        const r = this.$refs.panel.getBoundingClientRect();
        this.width = r.width; this.height = r.height; this.left = r.left; this.top = r.top;
        this.custom = true;
    },

    // ── Drag lifecycle (pointer events, bound only for the drag's duration) ──
    startMove(e) {
        if (!this.isDesktop || e.button !== 0) return;
        if (!this.custom) this._seed();
        this._begin(e, 'move', '');
    },

    startResize(e, edge) {
        if (!this.isDesktop || e.button !== 0) return;
        if (!this.custom) this._seed();
        this._begin(e, 'resize', edge);
    },

    _begin(e, mode, edge) {
        e.preventDefault();
        this._drag = {
            mode, edge,
            sx: e.clientX, sy: e.clientY,
            start: { w: this.width, h: this.height, l: this.left, t: this.top },
            pid: e.pointerId, target: e.target,
        };
        if (e.target.setPointerCapture) {
            try { e.target.setPointerCapture(e.pointerId); } catch { /* capture unsupported */ }
        }
        document.body.style.userSelect = 'none';
        this._boundMove = (ev) => this._onMove(ev);
        this._boundUp = () => this._onUp();
        this._boundCancel = () => this._onUp();
        document.addEventListener('pointermove', this._boundMove);
        document.addEventListener('pointerup', this._boundUp);
        // pointercancel (touch interrupted, capture lost, context menu, etc.) fires INSTEAD
        // of pointerup — without this the move/up listeners and user-select:none would leak.
        document.addEventListener('pointercancel', this._boundCancel);
    },

    _onMove(e) {
        const d = this._drag;
        if (!d) return;
        const dx = e.clientX - d.sx;
        const dy = e.clientY - d.sy;
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const g = d.mode === 'move'
            ? moveGeometry(d.start, dx, dy, vw, vh)
            : resizeGeometry(d.start, d.edge, dx, dy, vw, vh);
        this.width = g.w; this.height = g.h; this.left = g.l; this.top = g.t;
    },

    _onUp() {
        const d = this._drag;
        if (!d) return;
        if (d.target && d.target.releasePointerCapture) {
            try { d.target.releasePointerCapture(d.pid); } catch { /* already released */ }
        }
        this._teardownDrag();
        this._persist();
    },

    _teardownDrag() {
        if (this._boundMove) document.removeEventListener('pointermove', this._boundMove);
        if (this._boundUp) document.removeEventListener('pointerup', this._boundUp);
        if (this._boundCancel) document.removeEventListener('pointercancel', this._boundCancel);
        this._boundMove = null;
        this._boundUp = null;
        this._boundCancel = null;
        this._drag = null;
        document.body.style.userSelect = '';
    },

    // Double-click any handle / the drag-bar → forget this bucket, re-center.
    reset() {
        const all = this._readAll();
        delete all[this.bucket];
        localStorage.setItem(MODAL_GEOM_KEY, JSON.stringify(all));
        this.custom = false;
    },

    // Inline style for the panel: an explicit fixed box when the user has a custom
    // size on desktop, otherwise '' so the centered/responsive CSS layout wins.
    panelStyle() {
        if (!this.custom || !this.isDesktop) return '';
        return 'position:fixed;'
            + 'left:' + this.left + 'px;'
            + 'top:' + this.top + 'px;'
            + 'width:' + this.width + 'px;'
            + 'height:' + this.height + 'px;'
            + 'max-width:none;max-height:none;margin:0;';
    },
}));

/**
 * x-truncate-tip — Hover tooltip that fires when an element overflows
 * its box (scrollWidth > clientWidth), OR when the element has a
 * `_tipNodes` property (a DocumentFragment the directive appends as-is).
 *
 * The `_tipNodes` path is used by x-chip-overflow to show hidden chips
 * without ever touching innerHTML — we clone DOM subtrees directly.
 */
Alpine.directive('truncate-tip', (el, _directive, { cleanup }) => {
  let tip = null;

  const hasTipNodes = () => el._tipNodes && el._tipNodes.hasChildNodes && el._tipNodes.hasChildNodes();

  const show = () => {
    // Re-entrant guard: if a tooltip is already shown, skip. Without this,
    // rapid mouseenter events (e.g. synthetic events during HTMX swap or
    // mouse re-enter across nested elements) would overwrite `tip` and
    // orphan the prior DOM node on document.body with no cleanup path.
    if (tip) return;
    const viaNodes = hasTipNodes();
    if (!viaNodes && el.scrollWidth <= el.clientWidth) return;
    const text = viaNodes ? null : el.textContent.trim();
    if (!viaNodes && !text) return;

    tip = document.createElement('div');
    tip.className = 'truncate-tip';
    if (viaNodes) {
      // Clone the fragment so the original reference stays reusable.
      tip.appendChild(el._tipNodes.cloneNode(true));
    } else {
      tip.textContent = text;
    }
    document.body.appendChild(tip);

    const r = el.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    let top = r.top - tr.height - 6;
    if (top < 4) top = r.bottom + 6;
    let left = r.left + (r.width - tr.width) / 2;
    left = Math.max(4, Math.min(left, window.innerWidth - tr.width - 4));
    tip.style.top = top + 'px';
    tip.style.left = left + 'px';
    requestAnimationFrame(() => tip && tip.classList.add('visible'));
  };

  const hide = () => { if (tip) { tip.remove(); tip = null; } };

  el.addEventListener('mouseenter', show);
  el.addEventListener('mouseleave', hide);
  el.addEventListener('focusout', hide);

  // Remove any visible tooltip when Alpine tears down the element
  // (HTMX swap-out, component unmount). Without this, the orphaned tip
  // would stay on document.body with no removal path since its source
  // listeners have already been disconnected.
  cleanup(() => hide());
});

/**
 * x-chip-overflow — Measures chip row width and hides chips that don't
 * fit. Exposes a trailing +N button (must be last child, .opp-chip-more)
 * whose `_tipNodes` property holds a cloned DocumentFragment of the
 * hidden chips — x-truncate-tip reads that on hover.
 *
 * Primaries-first DOM order (enforced by _build_row_mpn_chips) ensures
 * the left-to-right overflow walk never hides a primary while a sub is
 * still visible.
 */
Alpine.directive('chip-overflow', (el, _directive, { cleanup }) => {
  const more = el.querySelector('.opp-chip-more');
  if (!more) return;
  const chips = Array.from(el.children).filter((c) => c !== more);

  let rafId = 0;

  const measure = () => {
    rafId = 0;
    chips.forEach((c) => (c.style.display = ''));
    more.style.display = 'none';
    more.textContent = '';
    more._tipNodes = null;

    const containerWidth = el.clientWidth;
    if (containerWidth === 0) return;

    const style = window.getComputedStyle(el);
    const gap = parseFloat(style.columnGap || style.gap || '0') || 4;

    // Measure +N width at worst-case placeholder, then clear.
    more.style.display = '';
    more.textContent = '+9';
    const moreWidth = more.getBoundingClientRect().width + gap;
    more.textContent = '';

    let used = 0;
    let fitCount = 0;
    for (const chip of chips) {
      const w = chip.getBoundingClientRect().width;
      const projected = used + w + (fitCount > 0 ? gap : 0);
      const reserve = fitCount < chips.length - 1 ? moreWidth : 0;
      if (projected + reserve <= containerWidth) {
        used = projected;
        fitCount++;
      } else {
        break;
      }
    }

    if (fitCount === chips.length) {
      more.style.display = 'none';
      return;
    }

    const hidden = chips.slice(fitCount);
    chips.slice(0, fitCount).forEach((c) => (c.style.display = ''));
    hidden.forEach((c) => (c.style.display = 'none'));

    more.textContent = '+' + hidden.length;

    // Build a DocumentFragment of cloned hidden chips, inside a chip-row wrapper.
    // x-truncate-tip will clone this fragment into the tooltip when hovered.
    const frag = document.createDocumentFragment();
    const wrap = document.createElement('span');
    wrap.className = 'opp-chip-row';
    hidden.forEach((c) => {
      const clone = c.cloneNode(true);
      clone.style.display = '';
      wrap.appendChild(clone);
    });
    frag.appendChild(wrap);
    more._tipNodes = frag;
  };

  const schedule = () => {
    if (rafId) return;
    rafId = requestAnimationFrame(measure);
  };

  schedule();

  const ro = new ResizeObserver(schedule);
  ro.observe(el);

  // Cleanup when Alpine tears down the element. Using Alpine's cleanup()
  // callback (third-arg of the directive signature) ensures the
  // ResizeObserver and any pending RAF are disconnected on HTMX swap-out,
  // preventing an observer leak per chip row.
  cleanup(() => {
    ro.disconnect();
    if (rafId) cancelAnimationFrame(rafId);
  });
});

/**
 * contactsView — Alpine component for the CRM account Contacts surface
 * (contacts_tab.html). Owns the people-search (`q`) + site filter (`siteFilter`)
 * and filters the rendered contact rows CLIENT-SIDE by toggling a `hidden` class
 * — no round-trip. The controls live OUTSIDE the #contacts-tab-list swap target,
 * so a CRUD re-render replaces only the rows; re-applies on htmx:afterSettle.
 */
Alpine.data('contactsView', () => ({
  q: '',
  siteFilter: '',
  init() {
    // Pre-select site filter when the tab was opened via a "View N contacts →" link.
    const initialSite = this.$root.getAttribute('data-initial-site');
    if (initialSite) this.siteFilter = initialSite;
    this.apply();
    // Re-filter after a CRUD swap replaces the inner #contacts-tab-list rows.
    this._onSettle = () => this.apply();
    this.$root.addEventListener('htmx:afterSettle', this._onSettle);
  },
  destroy() {
    if (this._onSettle) this.$root.removeEventListener('htmx:afterSettle', this._onSettle);
  },
  apply() {
    this.$nextTick(() => {
      const root = this.$root;
      const needle = this.q.trim().toLowerCase();
      const site = this.siteFilter;
      let visible = 0;
      root.querySelectorAll('[data-contact-row]').forEach((row) => {
        const nameMatch = !needle || (row.getAttribute('data-contact-search') || '').includes(needle);
        const siteMatch = !site || row.getAttribute('data-site-id') === site;
        const show = nameMatch && siteMatch;
        row.classList.toggle('hidden', !show);
        if (show) visible += 1;
      });
      // Hide a whole site section when none of its rows survive the filter.
      root.querySelectorAll('[data-contacts-section]').forEach((sec) => {
        const anyVisible = sec.querySelector('[data-contact-row]:not(.hidden)');
        sec.classList.toggle('hidden', !anyVisible);
      });
      const emptyHint = root.querySelector('[data-contacts-empty]');
      if (emptyHint) {
        const hasRows = root.querySelector('[data-contact-row]');
        emptyHint.classList.toggle('hidden', visible > 0 || !hasRows);
      }
    });
  },
}));

/**
 * rowActionRail — Alpine component for requisitions2 <tr>.
 * CSS handles hover visibility via tr:hover; this component exposes
 * `show` state so keyboard users (Tab, Enter, Escape) have a path.
 */
Alpine.data('rowActionRail', () => ({
  show: false,
  // Won/Lost require a close reason. `outcomePrompt` names the pending terminal
  // action ('won' | 'lost' | null); `outcomeReason` holds the typed reason. The
  // confirm button is gated on a non-empty trimmed reason, mirroring the
  // buy-plan cancel modal's required-textarea pattern.
  outcomePrompt: null,
  outcomeReason: '',
  openOutcome(action) {
    this.outcomeReason = '';
    this.outcomePrompt = action;
  },
  closeOutcome() {
    this.outcomePrompt = null;
    this.outcomeReason = '';
  },
  get outcomeReady() { return this.outcomeReason.trim().length > 0; },
  init() {
    this.$el.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { this.show = false; this.closeOutcome(); }
    });
  },
}));

// Data Ops dedup multi-select — one instance per dedup section (vendor / company).
// Selection unit is a PAIR token "<keeperId>-<loserId>" (keeper-first so bulk-merge
// keeps the suggested side). Mirrors rq2Page's Set-based pattern (reassign the Set to
// trigger Alpine reactivity). Lives inside #settings-content, which the htmx:afterSwap
// handler re-initTrees, so it rebinds cleanly after each merge/delete re-render.
Alpine.data('dedupSelect', () => ({
  selected: new Set(),
  toggle(token, checked) {
    if (checked) { this.selected.add(token); } else { this.selected.delete(token); }
    this.selected = new Set(this.selected);
  },
  toggleAll(checked, tokens) {
    if (checked) { tokens.forEach(t => this.selected.add(t)); } else { this.selected.clear(); }
    this.selected = new Set(this.selected);
  },
  clear() { this.selected = new Set(); },
  has(token) { return this.selected.has(token); },
  get count() { return this.selected.size; },
  // Comma-joined "a-b,c-d" string the bulk endpoint parses.
  get pairsStr() { return [...this.selected].join(','); },
  // Hide the dismissed rows immediately (client-only); the form re-renders the list.
  hideSelected() {
    this.selected.forEach(token => {
      const row = this.$root.querySelector('[data-pair="' + token + '"]');
      if (row) { row.style.display = 'none'; }
    });
  },
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
    // Reset body overflow only on full main-column navigations — not on drawer partials
    // (otherwise opening the search lead drawer loses scroll lock immediately).
    var t = evt.detail.target;
    if (t && t.id === 'main-content') {
        document.body.style.overflow = '';
    }
    // HTMX innerHTML swaps do not always auto-run Alpine on new nodes.
    // Explicit initTree for targets known to contain Alpine components/directives
    // (lead drawer close button; rq2-table rows with rowActionRail, x-truncate-tip,
    // x-chip-overflow — directives that must re-bind after filter/sort/action swaps;
    // rfq-affinity-section — affinity rows whose :checked/@change checkboxes bind to
    // the surrounding rfqVendorModal x-data scope, otherwise the checkboxes are inert
    // and ticked affinity vendors never enter selectedVendors / never get sent;
    // settings-content — the Settings tab body is lazy-swapped here and re-swapped by
    // every settings mutation (e.g. a dedup merge re-renders Data Ops), so its Alpine
    // directives — the Data Ops multi-select bar — must re-init or the checkboxes go
    // inert and selection state is lost after the first action).
    if (t && typeof Alpine !== 'undefined' && typeof Alpine.initTree === 'function') {
        if (
            t.id === 'lead-drawer-content' ||
            t.id === 'rq2-table' ||
            t.id === 'rfq-affinity-section' ||
            t.id === 'settings-content'
        ) {
            Alpine.initTree(t);
        }
    }
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
    // Escape → close search lead drawer (Alpine drawerOpen on #lead-drawer)
    if (e.key === 'Escape') {
        var drawer = document.getElementById('lead-drawer');
        if (drawer && typeof Alpine !== 'undefined' && typeof Alpine.$data === 'function') {
            var data = Alpine.$data(drawer);
            if (data && data.drawerOpen) {
                data.drawerOpen = false;
            }
        }
    }
});

/* Faceted materials search — Alpine.js component.
 * Manages commodity, sub-filters, search query, pagination.
 * URL is the canonical source of truth (back button, deep links work).
 */
// $persist when the plugin is registered (browser, before Alpine.start); plain default
// otherwise (vitest mocks / plugin absent) — never throws at factory-call time.
const persistOr = (def, key) => (typeof Alpine !== 'undefined' && Alpine.$persist) ? Alpine.$persist(def).as(key) : def;

// One-time storage migration: the confidence fold default flipped false→true, but
// @alpinejs/persist writes the CURRENT value to storage on init — so every browser that
// ever loaded the page under the old `persistOr(false, 'mat_confidence_open')` carries a
// persisted `false` that would override the new default. The fold state moved to
// 'mat_confidence_open2'; drop the dead key so a revert can't resurrect it.
if (typeof localStorage !== 'undefined') localStorage.removeItem('mat_confidence_open');

Alpine.data('materialsFilter', () => ({
  commodity: '',
  subFilters: {},
  q: '',
  page: 0,
  drawerOpen: false,
  displayNames: {},
  // Data-confidence selection — the flat list of enrichment tiers sent to the backend.
  // Surfaced as 3 user-facing checkboxes (see CONFIDENCE_GROUPS). Default = all tiers on
  // (the filter only narrows; the page opens showing everything).
  statuses: ['verified', 'web_sourced', 'oem_sourced', 'ai_inferred', 'not_catalogued', 'not_found', 'unenriched'],
  // Global facets — MaterialCard columns (OR-within each).
  lifecycle: [],
  rohs: [],
  condition: [],
  hasDatasheet: false,
  // Review queue — has_validation_conflict (authoritative evidence contradicts a manual value).
  needsReview: false,
  // Sourcing signals (Layer-3 operational filters) — MaterialCard + vendor history.
  hasStock: false,
  hasPrice: false,
  hasCrosses: false,
  internal: 'all',            // 'all' | 'standard' | 'internal'
  searchedWithin: 'any',      // '7d' | '30d' | '90d' | 'any'
  minSearches: 0,
  _onPopstate: null,

  // ── Direction-B UI state ─────────────────────────────────────────────
  // Hoisted sub-filter UI state (fold / typeahead text) so it survives HTMX re-renders of
  // #subfilters-container on every filters-changed. Keyed by spec_key; session-scoped.
  ui: { moreOpen: false, facetExpanded: {}, facetSearch: {} },
  // Type-to-find over the category tree (client-side filter; see tree.html).
  categorySearch: '',
  // Transient "Copied" flash for the copy-link control.
  copied: false,
  // Persisted CHROME only (layout prefs); filter STATE stays URL-bound.
  recentCommodities: persistOr([], 'mat_recent_commodities'),
  moreAttrsOpen: persistOr(false, 'mat_more_attrs_open'),
  sourcingOpen: persistOr(false, 'mat_sourcing_open'),
  // Confidence fold (first filter fold) opens by default — trust is the headline
  // filter; the heavy folds (sourcing / more attributes) stay closed until opened.
  // Key is the rotated 'mat_confidence_open2' so the new open default actually reaches
  // returning users (see the legacy-key removal above persistOr's call sites).
  confidenceOpen: persistOr(true, 'mat_confidence_open2'),

  // 3 user-facing confidence groups, each expanding to a set of enrichment tiers.
  // Array order pins the visual ordering of the Data-confidence section.
  CONFIDENCE_GROUPS: [
    { key: 'trusted', label: 'Trusted', dot: 'bg-emerald-500', tiers: ['verified', 'web_sourced', 'oem_sourced'] },
    { key: 'ai_inferred', label: 'AI-inferred', dot: 'bg-amber-500', tiers: ['ai_inferred'] },
    { key: 'no_data', label: 'No data', dot: 'bg-gray-400', tiers: ['not_catalogued', 'not_found', 'unenriched'] },
  ],
  // Derived from the groups so the tier set has a single source of truth.
  get DEFAULT_STATUSES() {
    return this.CONFIDENCE_GROUPS.flatMap(g => g.tiers);
  },

  // Sourcing-signal vocabularies — the single front-end source of truth as
  // [value, label] pairs (incl. the no-op sentinel 'all'/'any'). Rendered by
  // workspace.html's x-for templates and consulted by syncFromURL + the setters.
  // Backend twin (must stay in sync): INTERNAL_FILTER_VALUES / SEARCHED_WITHIN_VALUES
  // in app/services/faceted_search_service.py — the route logs a WARNING and degrades
  // to the sentinel when the vocabularies drift.
  INTERNAL_MODES: [['all', 'All'], ['standard', 'Standard MPNs'], ['internal', 'Internal parts']],
  SEARCH_BUCKETS: [['7d', '7d'], ['30d', '30d'], ['90d', '90d'], ['any', 'Any']],

  get commodityDisplayName() {
    if (!this.commodity) return '';
    return this.displayNames[this.commodity]
      || this.commodity.replace(/_/g, ' ').replace(/(^|\s)\S/g, l => l.toUpperCase());
  },

  // True when the confidence selection is narrowed from the all-on default.
  get confidenceNarrowed() {
    return !(this.statuses.length === this.DEFAULT_STATUSES.length
      && this.DEFAULT_STATUSES.every(s => this.statuses.includes(s)));
  },

  _groupChecked(group) {
    return group.tiers.every(t => this.statuses.includes(t));
  },

  // Fully-checked confidence groups — surfaced as active chips, but only when narrowed.
  get activeConfidenceGroups() {
    if (!this.confidenceNarrowed) return [];
    return this.CONFIDENCE_GROUPS.filter(g => this._groupChecked(g));
  },

  confidenceGroupChecked(groupKey) {
    const group = this.CONFIDENCE_GROUPS.find(g => g.key === groupKey);
    return !!group && this._groupChecked(group);
  },

  toggleConfidenceGroup(groupKey) {
    const group = this.CONFIDENCE_GROUPS.find(g => g.key === groupKey);
    if (!group) return;
    if (this._groupChecked(group)) {
      this.statuses = this.statuses.filter(s => !group.tiers.includes(s));
    } else {
      for (const t of group.tiers) {
        if (!this.statuses.includes(t)) this.statuses.push(t);
      }
    }
    this.applyFilters();
  },

  // Active selections inside the "Sourcing signals" section (for its badge + chips).
  get sourcingActiveCount() {
    return (this.hasStock ? 1 : 0) + (this.hasPrice ? 1 : 0) + (this.hasCrosses ? 1 : 0)
      + (this.internal !== 'all' ? 1 : 0)
      + (this.searchedWithin !== 'any' ? 1 : 0)
      + (this.minSearches > 0 ? 1 : 0);
  },

  get activeFilterCount() {
    let count = 0;
    for (const [key, val] of Object.entries(this.subFilters)) {
      if (Array.isArray(val)) count += val.length;
      else if (val !== '' && val !== null) count += 1;
    }
    count += this.activeConfidenceGroups.length;
    count += this.lifecycle.length;
    count += this.rohs.length;
    count += this.condition.length;
    if (this.hasDatasheet) count += 1;
    if (this.needsReview) count += 1;
    count += this.sourcingActiveCount;
    return count;
  },

  // Active selections inside the collapsed "More attributes" section (for its badge).
  get attributesActiveCount() {
    return this.lifecycle.length + this.rohs.length + this.condition.length
      + (this.hasDatasheet ? 1 : 0)
      + (this.needsReview ? 1 : 0)
      + (Array.isArray(this.subFilters.manufacturers) ? this.subFilters.manufacturers.length : 0);
  },

  // Top summary "Clear all" — resets every filter but KEEPS the selected commodity
  // (commodity is navigation, not a filter). The spec-scoped control is "Clear specs".
  clearAllFilters() {
    this.subFilters = {};
    this.lifecycle = [];
    this.rohs = [];
    this.condition = [];
    this.hasDatasheet = false;
    this.needsReview = false;
    this.hasStock = false;
    this.hasPrice = false;
    this.hasCrosses = false;
    this.internal = 'all';
    this.searchedWithin = 'any';
    this.minSearches = 0;
    this.statuses = [...this.DEFAULT_STATUSES];
    this.q = '';
    this.ui.facetSearch = {};
    this.ui.facetExpanded = {};
    this.applyFilters();
  },

  // True when the type-to-find query matches at least one known category (else show a
  // "no matches" hint instead of a blank tree). Over displayNames — the dominant
  // gibberish/typo no-match case; a query is "" → always true.
  get anyCategoryMatches() {
    if (!this.categorySearch) return true;
    const t = this.categorySearch.toLowerCase();
    return Object.values(this.displayNames).some(n => String(n).toLowerCase().includes(t));
  },

  copyLink() {
    const url = window.location.href;
    const flash = () => { this.copied = true; setTimeout(() => { this.copied = false; }, 1500); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(url).then(flash).catch(() => window.prompt('Copy this link:', url));
    } else {
      window.prompt('Copy this link:', url);  // clipboard API unavailable (HTTP / old browser)
    }
  },

  init() {
    try { this.displayNames = JSON.parse(this.$el.dataset.displayNames || '{}'); } catch (e) { this.displayNames = {}; }
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
      // Trust ladder: explicit `statuses` CSV wins; otherwise fall back to the
      // trustworthy default. (Legacy verified_only/web_sourced links still map in.)
      const statusesParam = params.get('statuses');
      if (statusesParam !== null) {
        this.statuses = statusesParam.split(',').filter(s => s !== '');
      } else {
        const legacy = [];
        if (params.get('verified_only') === 'true') legacy.push('verified');
        if (params.get('web_sourced') === 'true') legacy.push('web_sourced');
        this.statuses = legacy.length > 0 ? legacy : [...this.DEFAULT_STATUSES];
      }
      this.lifecycle = (params.get('lifecycle') || '').split(',').filter(s => s !== '');
      this.rohs = (params.get('rohs') || '').split(',').filter(s => s !== '');
      this.condition = (params.get('condition') || '').split(',').filter(s => s !== '');
      this.hasDatasheet = params.get('has_datasheet') === 'true';
      this.needsReview = params.get('has_validation_conflict') === 'true';
      this.hasStock = params.get('has_stock') === 'true';
      this.hasPrice = params.get('has_price') === 'true';
      this.hasCrosses = params.get('has_crosses') === 'true';
      const internalParam = params.get('internal');
      this.internal = this.INTERNAL_MODES.some(([v]) => v === internalParam) ? internalParam : 'all';
      const withinParam = params.get('searched_within');
      this.searchedWithin = this.SEARCH_BUCKETS.some(([v]) => v === withinParam) ? withinParam : 'any';
      const minSearchesVal = parseInt(params.get('min_searches') || '0', 10);
      this.minSearches = (isNaN(minSearchesVal) || minSearchesVal < 0) ? 0 : minSearchesVal;
      const pageVal = parseInt(params.get('page') || '0', 10);
      this.page = isNaN(pageVal) ? 0 : pageVal;
      this.subFilters = {};
      for (const [key, val] of params.entries()) {
        if (key.startsWith('sf_')) {
          const specKey = key.slice(3);
          try {
            if (specKey.endsWith('__vals')) {
              // Numeric common-value chips (P2): a comma-joined number list.
              // Coerce each to a number and drop NaN so the chip :class membership
              // check (which compares numbers) and the value_numeric IN predicate stay
              // numeric — string entries would silently never match.
              // Drop empty segments BEFORE coercion: Number('') === 0 (not NaN), so a
              // malformed/truncated link like "8," would otherwise inject a phantom 0.
              const nums = val.split(',').filter(s => s !== '').map(Number).filter(n => !isNaN(n));
              if (nums.length > 0) {
                this.subFilters[specKey] = nums;
              }
            } else if (specKey.endsWith('_min') || specKey.endsWith('_max')) {
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
      console.warn('[materialsFilter] Broken URL — resetting filters', e);
      // Broken URL — reset to defaults
      this.commodity = '';
      this.q = '';
      this.statuses = [...this.DEFAULT_STATUSES];
      this.lifecycle = [];
      this.rohs = [];
      this.condition = [];
      this.hasDatasheet = false;
      this.needsReview = false;
      this.hasStock = false;
      this.hasPrice = false;
      this.hasCrosses = false;
      this.internal = 'all';
      this.searchedWithin = 'any';
      this.minSearches = 0;
      this.page = 0;
      this.subFilters = {};
    }
  },

  pushURL(push = false) {
    const params = new URLSearchParams();
    if (this.commodity) params.set('commodity', this.commodity);
    if (this.q) params.set('q', this.q);
    // Persist the trust ladder only when it differs from the default set, so
    // clean URLs stay clean. An empty selection is meaningful → always written.
    if (this.confidenceNarrowed) params.set('statuses', this.statuses.join(','));
    if (this.lifecycle.length > 0) params.set('lifecycle', this.lifecycle.join(','));
    if (this.rohs.length > 0) params.set('rohs', this.rohs.join(','));
    if (this.condition.length > 0) params.set('condition', this.condition.join(','));
    if (this.hasDatasheet) params.set('has_datasheet', 'true');
    if (this.needsReview) params.set('has_validation_conflict', 'true');
    if (this.hasStock) params.set('has_stock', 'true');
    if (this.hasPrice) params.set('has_price', 'true');
    if (this.hasCrosses) params.set('has_crosses', 'true');
    if (this.internal !== 'all') params.set('internal', this.internal);
    if (this.searchedWithin !== 'any') params.set('searched_within', this.searchedWithin);
    if (this.minSearches > 0) params.set('min_searches', this.minSearches);
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
    // Reset hoisted per-facet UI so a previous commodity's typeahead text / fold (keyed by a
    // shared spec_key like "package") can't silently filter the new commodity's facets.
    this.ui.facetSearch = {};
    this.ui.facetExpanded = {};
    this.ui.moreOpen = false;
    if (this.commodity) {
      // Most-recent-first, deduped, capped at 5 (persisted navigation history).
      const list = this.recentCommodities.filter(x => x !== this.commodity);
      list.unshift(this.commodity);
      this.recentCommodities = list.slice(0, 5);
    }
    document.body.dispatchEvent(new CustomEvent('commodity-changed'));
    this.applyFilters();
  },

  // Global-facet array toggle (lifecycle / rohs). OR-within each facet.
  toggleGlobalFacet(facet, value) {
    const arr = this[facet];
    if (!Array.isArray(arr)) return;
    const idx = arr.indexOf(value);
    if (idx >= 0) arr.splice(idx, 1);
    else arr.push(value);
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  toggleDatasheet() {
    this.hasDatasheet = !this.hasDatasheet;
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Review-queue toggle — has_validation_conflict (needs human conflict review).
  toggleNeedsReview() {
    this.needsReview = !this.needsReview;
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Sourcing-signal boolean toggle (hasStock / hasPrice / hasCrosses).
  toggleSourcingFlag(flag) {
    if (!['hasStock', 'hasPrice', 'hasCrosses'].includes(flag)) {
      console.warn(`materialsFilter: unknown sourcing flag ${flag}`);
      return;
    }
    this[flag] = !this[flag];
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Internal-vs-standard segmented control ('all' | 'standard' | 'internal').
  setInternal(mode) {
    this.internal = this.INTERNAL_MODES.some(([v]) => v === mode) ? mode : 'all';
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Recently-searched chips ('7d' | '30d' | '90d' | 'any'). Re-clicking the active
  // bucket resets to 'any'.
  setSearchedWithin(bucket) {
    const next = this.SEARCH_BUCKETS.some(([v]) => v === bucket) ? bucket : 'any';
    this.searchedWithin = (this.searchedWithin === next) ? 'any' : next;
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Min-searches numeric input (0 = off).
  setMinSearches(value) {
    const num = parseInt(value, 10);
    this.minSearches = (isNaN(num) || num < 0) ? 0 : num;
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Chip removal for a global facet — always re-applies (explicit user action).
  removeGlobalFacet(facet, value) {
    const arr = this[facet];
    if (!Array.isArray(arr)) return;
    const idx = arr.indexOf(value);
    if (idx >= 0) arr.splice(idx, 1);
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

  // Numeric common-value chip toggle (P2). Maintains subFilters[specKey + '__vals']
  // as an array of NUMBERS — the backend predicate is value_numeric IN (...), and the
  // chip :class membership check (.includes()) compares against JS numbers. Mirrors
  // toggleFilter's add/remove + delete-when-empty shape; the value is server-rendered
  // from value_numeric (chip.value|tojson), so it is always a number.
  toggleNumericChip(specKey, value) {
    const key = specKey + '__vals';
    if (!this.subFilters[key]) {
      this.subFilters[key] = [value];
    } else {
      const idx = this.subFilters[key].indexOf(value);
      if (idx >= 0) {
        this.subFilters[key].splice(idx, 1);
        if (this.subFilters[key].length === 0) {
          delete this.subFilters[key];
        }
      } else {
        this.subFilters[key].push(value);
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
 * Depends on: /api/companies/typeahead, /v2/partials/customers/lookup
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
    _onCustomerCreated: null,
    init() {
        this.fetchCompanies();
        // Listen for customer-created event from quick-create
        this._onCustomerCreated = (e) => {
            this.selectById(e.detail.siteId, e.detail.displayName);
            this.fetchCompanies();
        };
        document.addEventListener('customer-created', this._onCustomerCreated);
    },
    destroy() {
        if (this._onCustomerCreated) {
            document.removeEventListener('customer-created', this._onCustomerCreated);
        }
    },
    loadError: '',
    fetchCompanies() {
        this.loadError = '';
        fetch('/api/companies/typeahead')
            .then(r => {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then(data => { this.companies = data; })
            .catch(e => {
                console.error('Failed to load customer list:', e);
                this.loadError = 'Could not load customers.';
            });
    },
    get filtered() {
        if (!this.query.trim()) return this.companies.slice(0, 20);
        const q = this.query.toLowerCase();
        return this.companies.filter(c => c.name.toLowerCase().includes(q)).slice(0, 20);
    },
    select(company, site) {
        this.selectedSiteId = site.id || '';
        this.selectedName = site.id ? company.name + ' \u2014 ' + site.site_name : company.name;
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
        const resultEl = this.$el.querySelector('[data-lookup-result]');
        if (!resultEl) {
            console.error('customerPicker: [data-lookup-result] element not found');
            this.lookingUp = false;
            return;
        }
        try {
            const formData = new FormData();
            formData.append('company_name', this.newName);
            formData.append('location', this.newLocation);
            const resp = await fetch('/v2/partials/customers/lookup', { method: 'POST', body: formData });
            if (!resp.ok) {
                resultEl.textContent = `Lookup failed (${resp.status}). Try again.`;
                resultEl.classList.add('text-xs', 'text-rose-500');
                this.lookingUp = false;
                return;
            }
            // Server HTML is trusted (same-origin, auth-protected endpoint)
            resultEl.replaceChildren();
            resultEl.insertAdjacentHTML('afterbegin', await resp.text());
            htmx.process(resultEl);
        } catch (e) {
            console.error('Customer lookup failed:', e);
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
    parsing: false,
    saving: false,
    parseError: '',
    parts: [],
    activePartIdx: 0,
    showBulkFill: false,
    init() {
        this.addBlankPart();
    },
    focusPart(idx) {
        this.activePartIdx = idx;
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
    /** Build a sub object for a substitute part. */
    _makeSub(src) {
        if (typeof src === 'string') return { mpn: src, manufacturer: '', revision: '', hardware_codes: '' };
        return {
            mpn: src?.mpn || src?.primary_mpn || '',
            manufacturer: src?.manufacturer || '',
            revision: src?.revision || '',
            hardware_codes: src?.hardware_codes || '',
        };
    },
    /** Build a part row object, optionally seeded from AI-parsed data. */
    _makePart(src) {
        const subs = (src?.substitutes || []).map(s => this._makeSub(s));
        return {
            _id: Date.now() + Math.random(),
            primary_mpn: src?.primary_mpn || '',
            manufacturer: src?.manufacturer || '',
            target_qty: src?.target_qty || 1,
            brand: src?.brand || '',
            condition: src?.condition || 'new',
            target_price: src?.target_price || '',
            customer_pn: src?.customer_pn || '',
            date_codes: src?.date_codes || '',
            packaging: src?.packaging || '',
            firmware: src?.firmware || '',
            hardware_codes: src?.hardware_codes || '',
            description: src?.description || '',
            package_type: src?.package_type || '',
            revision: src?.revision || '',
            need_by_date: src?.need_by_date || '',
            sale_notes: src?.notes || src?.sale_notes || '',
            substitutes: subs,
            showSubs: subs.length > 0,
            noteOpen: false,
        };
    },
    addBlankPart() {
        this.parts.push(this._makePart());
    },
    addSub(part) {
        const target = part || this.parts[this.activePartIdx] || this.parts[0];
        if (!target) return;
        target.substitutes.push(this._makeSub());
        target.showSubs = true;
    },
    addSubToActive() {
        this.addSub(this.parts[this.activePartIdx]);
    },
    removeSub(part, idx) {
        part.substitutes.splice(idx, 1);
        if (part.substitutes.length === 0) part.showSubs = false;
    },
    removePart(idx) {
        this.parts.splice(idx, 1);
        if (this.activePartIdx >= this.parts.length) this.activePartIdx = Math.max(0, this.parts.length - 1);
    },
    async standardizeDescription(part) {
        const raw = (part.description || '').trim();
        if (!raw || raw.length < 3) {
            // No user description — auto-generate from MPN if available
            const mpn = (part.primary_mpn || '').trim();
            if (mpn.length >= 3) {
                await this.generateDescription(part);
            }
            return;
        }
        try {
            const resp = await fetch('/api/ai/standardize-description', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    description: raw,
                    mpn: part.primary_mpn || '',
                    manufacturer: part.manufacturer || '',
                }),
            });
            if (resp.ok) {
                const data = await resp.json();
                if (data.description) part.description = data.description;
            }
        } catch (e) {
            console.warn('Description standardize failed:', e);
        }
    },
    async generateDescription(part) {
        const mpn = (part.primary_mpn || '').trim();
        if (!mpn || mpn.length < 3) return;
        try {
            const resp = await fetch('/api/ai/generate-description', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mpn: mpn,
                    manufacturer: part.manufacturer || '',
                    existing_description: part.description || '',
                }),
            });
            if (resp.ok) {
                const data = await resp.json();
                if (data.description && data.confidence >= 0.75) {
                    part.description = data.description;
                }
            }
        } catch (e) {
            console.warn('Description generate failed:', e);
        }
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
            if (!resp.ok) {
                this.parseError = resp.status === 401
                    ? 'Session expired. Please refresh and log in again.'
                    : `Server error (${resp.status}). Please try again.`;
                this.parsing = false;
                return;
            }
            const data = await resp.json();
            if (data.error) {
                this.parseError = data.error;
            } else {
                const parsed = (data.requirements || []).map(r => this._makePart(r));
                if (parsed.length === 0) {
                    this.parseError = 'No parts could be extracted. Try a different format.';
                } else {
                    // Remove empty rows, then append parsed parts
                    this.parts = this.parts.filter(p => p.primary_mpn.trim());
                    this.parts.push(...parsed);
                    this.showBulkFill = false;
                    this.rawText = '';
                }
                if (data.inferred_name && !this.reqName.trim()) {
                    this.reqName = data.inferred_name;
                }
                if (data.inferred_customer && !this.customerName.trim()) {
                    this.customerName = data.inferred_customer;
                }
            }
        } catch (e) {
            console.error('parseWithAI error:', e);
            this.parseError = 'Network error — check your connection and try again.';
        }
        this.parsing = false;
    },
}));

Alpine.data('quoteBuilder', (initialLines, reqId, hasCustomerSite, requirementIds, multiReqIds) => ({
  lines: initialLines,
  reqId: reqId,
  hasCustomerSite: hasCustomerSite,
  requirementIds: requirementIds || '',
  multiReqIds: multiReqIds || '',
  activeIdx: 0,
  activeFilter: 'has_offers',
  saving: false,
  saved: false,
  loading: true,
  loadError: null,
  quoteId: null,
  quoteNumber: null,
  saveError: null,
  bulkMarkupPct: 25,

  init() {
    // Keyboard handler
    this._keyHandler = (e) => this.handleKeydown(e);
    window.addEventListener('keydown', this._keyHandler);
    // If lines were passed inline (tests/fallback), skip fetch
    if (this.lines.length > 0) {
      this.loading = false;
      this._autoSelectFirst();
    }
  },

  async loadData() {
    try {
      let dataUrl;
      if (this.multiReqIds) {
        dataUrl = `/v2/partials/quote-builder/multi/data?requisition_ids=${this.multiReqIds}`;
      } else {
        dataUrl = `/v2/partials/quote-builder/${this.reqId}/data` + (this.requirementIds ? `?requirement_ids=${this.requirementIds}` : '');
      }
      const resp = await fetch(dataUrl);
      if (!resp.ok) {
        this.loadError = `Failed to load quote data (HTTP ${resp.status}). Please close and try again.`;
        this.loading = false;
        return;
      }
      const data = await resp.json();
      this.lines = data.lines || [];
      this._autoSelectFirst();
    } catch (e) {
      this.loadError = 'Network error loading quote data. Please check your connection.';
    } finally {
      this.loading = false;
    }
  },

  _autoSelectFirst() {
    const idx = this.filteredLines.findIndex(l => l.status === 'needs_review' || l.status === 'decided');
    if (idx >= 0) this.activeIdx = idx;
  },
  destroy() {
    window.removeEventListener('keydown', this._keyHandler);
  },

  // ── Computed ──
  get activeLine() { return this.filteredLines[this.activeIdx] ?? null; },
  get selectedOffer() {
    if (!this.activeLine) return null;
    return this.activeLine.offers.find(o => o.id === this.activeLine.selected_offer_id) ?? null;
  },
  get margin() {
    if (!this.activeLine?.sell_price || !this.selectedOffer) return null;
    const sell = this.activeLine.sell_price;
    const cost = this.selectedOffer.unit_price;
    return sell > 0 ? ((sell - cost) / sell * 100) : 0;
  },
  get extCost() {
    if (!this.activeLine || !this.selectedOffer) return 0;
    return (this.activeLine.target_qty || 0) * this.selectedOffer.unit_price;
  },
  get extSell() {
    if (!this.activeLine) return 0;
    return (this.activeLine.target_qty || 0) * (this.activeLine.sell_price || 0);
  },
  get lineProfit() { return this.extSell - this.extCost; },
  get minPrice() {
    if (!this.activeLine?.offers.length) return 0;
    return Math.min(...this.activeLine.offers.map(o => o.unit_price));
  },
  get maxPrice() {
    if (!this.activeLine?.offers.length) return 0;
    return Math.max(...this.activeLine.offers.map(o => o.unit_price));
  },
  pricePosition(price) {
    const range = this.maxPrice - this.minPrice;
    if (range === 0) return 50;
    return Math.round(((price - this.minPrice) / range) * 100);
  },

  // Single-pass stats — avoids 6-8 separate filter scans per render cycle
  get _stats() {
    let decided = 0, skipped = 0, hasOffers = 0, needsReview = 0, cost = 0, sell = 0;
    for (const l of this.lines) {
      if (l.offer_count > 0) hasOffers++;
      if (l.status === 'decided') {
        decided++;
        const offer = l.offers.find(o => o.id === l.selected_offer_id);
        cost += (l.target_qty || 0) * (offer?.unit_price || 0);
        sell += (l.target_qty || 0) * (l.sell_price || 0);
      } else if (l.status === 'skipped') {
        skipped++;
      } else if (l.status === 'needs_review') {
        needsReview++;
      }
    }
    return { decided, skipped, hasOffers, needsReview, cost, sell };
  },
  get filterOptions() {
    const s = this._stats;
    return [
      { key: 'all', label: 'All', count: this.lines.length },
      { key: 'has_offers', label: 'Has Offers', count: s.hasOffers },
      { key: 'needs_review', label: 'Needs Review', count: s.needsReview },
      { key: 'decided', label: 'Decided', count: s.decided },
      { key: 'skipped', label: 'Skipped', count: s.skipped },
    ];
  },
  get filteredLines() {
    if (this.activeFilter === 'all') return this.lines;
    if (this.activeFilter === 'has_offers') return this.lines.filter(l => l.offer_count > 0);
    return this.lines.filter(l => l.status === this.activeFilter);
  },
  get decidedCount() { return this._stats.decided; },
  get skippedCount() { return this._stats.skipped; },
  get totalCount() { return this.lines.length; },
  get decidedPct() { return this.lines.length ? Math.round(this.decidedCount / this.lines.length * 100) : 0; },
  get totalCost() { return this._stats.cost; },
  get totalSell() { return this._stats.sell; },
  get blendedMargin() {
    return this.totalSell > 0 ? ((this.totalSell - this.totalCost) / this.totalSell * 100) : 0;
  },

  // ── Actions ──
  selectLine(idx) { this.activeIdx = idx; },
  setFilter(f) { this.activeFilter = f; this.activeIdx = 0; },

  selectOffer(offer) {
    if (!this.activeLine) return;
    this.activeLine.selected_offer_id = offer.id;
    if (!this.activeLine.sell_price_manual) {
      this.activeLine.sell_price = offer.unit_price;
    }
  },

  confirmDecision() {
    if (!this.activeLine?.selected_offer_id || !this.activeLine?.sell_price) return;
    this.activeLine.status = 'decided';
    // Flash the left-panel row (use data attribute to find correct row regardless of filter)
    const reqId = this.activeLine.requirement_id;
    this.$nextTick(() => {
      const row = this.$el.querySelector(`.qb-list button[data-req-id="${reqId}"]`) ||
                  this.$el.querySelectorAll('.qb-list button')[this.activeIdx];
      if (row) {
        row.classList.add('qb-decision-flash');
        setTimeout(() => row?.classList.remove('qb-decision-flash'), 800);
      }
    });
    // Auto-advance to next undecided
    this.advanceToNext();
  },

  skipLine() {
    if (!this.activeLine) return;
    this.activeLine.status = 'skipped';
    this.advanceToNext();
  },

  undoDecision() {
    if (!this.activeLine) return;
    this.activeLine.status = this.activeLine.offer_count > 0 ? 'needs_review' : 'no_offers';
    this.activeLine.selected_offer_id = null;
    this.activeLine.sell_price = null;
    this.activeLine.sell_price_manual = false;
  },

  advanceToNext() {
    const nextIdx = this.filteredLines.findIndex((l, i) => i > this.activeIdx && l.status === 'needs_review');
    if (nextIdx >= 0) {
      this.activeIdx = nextIdx;
    } else {
      // Wrap around or stay
      const wrapIdx = this.filteredLines.findIndex(l => l.status === 'needs_review');
      if (wrapIdx >= 0) this.activeIdx = wrapIdx;
    }
  },

  applyBulkMarkup() {
    const pct = this.bulkMarkupPct;
    if (!pct || pct <= 0) return;
    this.lines.forEach(l => {
      if (l.status === 'decided' && !l.sell_price_manual) {
        const offer = l.offers.find(o => o.id === l.selected_offer_id);
        if (offer) {
          l.sell_price = parseFloat((offer.unit_price * (1 + pct / 100)).toFixed(4));
        }
      }
    });
  },

  closeBuilder() {
    const hasChanges = this.lines.some(l => l.status === 'decided' || l.status === 'skipped');
    if (hasChanges && !this.saved) {
      if (!confirm('You have unsaved line decisions. Close anyway?')) return;
    }
    window.dispatchEvent(new CustomEvent('close-quote-builder'));
  },

  async saveQuote() {
    this.saving = true;
    this.saveError = null;
    const decided = this.lines.filter(l => l.status === 'decided');
    const linePayload = decided.map(l => {
      const offer = l.offers.find(o => o.id === l.selected_offer_id);
      const cost = offer?.unit_price || 0;
      const sell = l.sell_price || 0;
      const margin = sell > 0 ? parseFloat(((sell - cost) / sell * 100).toFixed(2)) : 0;
      return {
        requirement_id: l.requirement_id,
        offer_id: l.selected_offer_id,
        mpn: l.mpn,
        manufacturer: l.manufacturer,
        qty: l.target_qty,
        cost_price: cost,
        sell_price: sell,
        margin_pct: margin,
        lead_time: offer?.lead_time || null,
        date_code: offer?.date_code || null,
        condition: offer?.condition || null,
        packaging: offer?.packaging || null,
        moq: offer?.moq || null,
        material_card_id: offer?.material_card_id || null,
        notes: l.buyer_notes || null,
      };
    });
    try {
      const resp = await fetch(`/v2/partials/quote-builder/${this.reqId}/save`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrfToken(),
        },
        body: JSON.stringify({
          lines: linePayload,
          quote_id: this.quoteId,
        }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        this.quoteId = data.quote_id;
        this.quoteNumber = data.quote_number;
        this.saved = true;
        showToast(`Quote ${data.quote_number} saved`, 'success');
      } else {
        this.saveError = data.error || data.detail || 'Save failed';
      }
    } catch (e) {
      this.saveError = 'Network error';
    }
    this.saving = false;
  },

  _doExport(format) {
    if (!this.quoteId) return;
    window.location.href = `/v2/partials/quote-builder/${this.reqId}/export/${format}?quote_id=${this.quoteId}`;
  },
  exportExcel() { this._doExport('excel'); },
  exportPdf() { this._doExport('pdf'); },

  handleKeydown(e) {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
      if (e.key === 'Enter' && e.target.matches('[x-ref=sellPriceInput]')) {
        e.preventDefault();
        this.confirmDecision();
      }
      return;
    }
    if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); this.activeIdx = Math.min(this.activeIdx + 1, this.filteredLines.length - 1); }
    if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); this.activeIdx = Math.max(this.activeIdx - 1, 0); }
    if (e.key === 'Tab' && !e.shiftKey) { e.preventDefault(); this.$refs.sellPriceInput?.focus(); }
    if (e.key >= '1' && e.key <= '9') {
      const idx = parseInt(e.key) - 1;
      if (this.activeLine?.offers[idx]) this.selectOffer(this.activeLine.offers[idx]);
    }
    if (e.key === 's') this.skipLine();
    if (e.key === 'f') {
      const keys = this.filterOptions.map(f => f.key);
      const cur = keys.indexOf(this.activeFilter);
      this.setFilter(keys[(cur + 1) % keys.length]);
    }
  },
}));

// ── quoteBuilderTab: in-workspace Build-Quote tab (single-stage inline) ──
// The simplified reshape of the full quoteBuilder modal for the requisition-detail tab.
// `data` is a plain reactive object keyed by requirement id, seeded inline by the server
// template (best cost, best-offer id, sell seed, qty, mpn/mfr/condition per line). Reuses
// the same margin math as the modal (margin = (sell - cost) / sell) and the same blended
// rollup, but as a single inline form: check a line -> sell-price seeds -> live margin +
// guardrail -> Assemble posts a QuoteBuilderLine[] payload to the assemble endpoint.
Alpine.data('quoteBuilderTab', (reqId, hasCustomerSite, minMarginPct, quoteExists, data) => ({
  reqId,
  hasCustomerSite,
  minMarginPct: minMarginPct || 10,
  quoteExists: !!quoteExists,
  markupPct: 20,
  data: data || {},

  // ── Per-line getters (reuse the modal's margin definition) ──
  _sell(id) {
    const l = this.data[id];
    const v = parseFloat(l && l.price);
    return Number.isFinite(v) ? v : null;
  },
  marginPct(id) {
    const l = this.data[id];
    const sell = this._sell(id);
    if (!l || sell === null || sell <= 0 || l.cost === null) return null;
    return (sell - l.cost) / sell * 100;
  },
  marginClass(id) {
    const m = this.marginPct(id);
    if (m === null) return 'text-gray-300';
    if (m >= 25) return 'text-emerald-600';
    if (m >= this.minMarginPct) return 'text-amber-600';
    return 'text-rose-600';
  },
  guardrail(id) {
    const l = this.data[id];
    const sell = this._sell(id);
    if (!l || sell === null || sell <= 0 || l.cost === null) return null;
    if (sell < l.cost) return 'below cost';
    const m = (sell - l.cost) / sell * 100;
    if (m < this.minMarginPct) return 'thin margin';
    return null;
  },

  // ── Selection + blended rollup ──
  _sellOf(l) {
    const v = parseFloat(l.price);
    return Number.isFinite(v) && v > 0 ? v : null;
  },
  _selected() { return Object.values(this.data).filter(l => l.sel && this._sellOf(l) !== null); },
  anySelected() { return Object.values(this.data).some(l => l.sel); },
  get selectedCount() { return Object.values(this.data).filter(l => l.sel).length; },
  get totalSell() {
    return this._selected().reduce((sum, l) => sum + this._sellOf(l) * (l.qty || 0), 0);
  },
  get totalCost() {
    return this._selected().reduce((sum, l) => sum + (l.cost || 0) * (l.qty || 0), 0);
  },
  get blendedMargin() {
    const sell = this.totalSell;
    if (sell <= 0) return null;
    return (sell - this.totalCost) / sell * 100;
  },
  get blendedMarginClass() {
    const m = this.blendedMargin;
    if (m === null) return 'text-gray-300';
    if (m >= 25) return 'text-emerald-600';
    if (m >= this.minMarginPct) return 'text-amber-600';
    return 'text-rose-600';
  },
  get blendedWarning() {
    const m = this.blendedMargin;
    if (m === null) return null;
    if (this.totalSell < this.totalCost) return 'Blended quote is below cost.';
    if (m < this.minMarginPct) return `Blended margin ${m.toFixed(1)}% is below the ${this.minMarginPct}% floor.`;
    return null;
  },

  // ── Actions ──
  applyMarkup() {
    const factor = 1 + (this.markupPct || 0) / 100;
    Object.values(this.data).forEach(l => {
      if (l.cost !== null) l.price = (l.cost * factor).toFixed(4);
    });
  },

  // Pick WHICH offer this line uses (default = best). Sets the chosen offerId (persisted on
  // the QuoteLine, and the buy-plan default at build time) and re-points cost to that
  // offer's price so the live margin reflects the offer actually being quoted. Vendor
  // identity never leaves the builder — the customer doc strips it (quote_export_context).
  selectOffer(id, offerId) {
    const l = this.data[id];
    if (!l) return;
    const oid = parseInt(offerId, 10);
    l.offerId = Number.isFinite(oid) ? oid : null;
    const chosen = (l.offers || []).find(o => o.id === l.offerId);
    if (chosen) l.cost = chosen.cost;
  },

  payload() {
    return JSON.stringify(
      Object.entries(this.data)
        .filter(([id, l]) => l.sel && this._sellOf(l) !== null)
        .map(([id, l]) => {
          const sell = this._sellOf(l);
          const cost = l.cost || 0;
          const margin = sell > 0 ? parseFloat(((sell - cost) / sell * 100).toFixed(2)) : 0;
          return {
            requirement_id: Number(id),
            offer_id: l.offerId,
            mpn: l.mpn,
            manufacturer: l.mfr,
            qty: l.qty || 0,
            cost_price: cost,
            sell_price: sell,
            margin_pct: margin,
            condition: l.cond,
          };
        })
    );
  },
}));

// ── rfqVendorModal: sightings "Send RFQ" vendor-selection + compose modal ──
// Rendered by app/templates/htmx/partials/sightings/vendor_modal.html. The server
// passes the pre-selected vendor normalized-names and the requirement ids through a
// SINGLE-quoted x-data attribute via |tojson — kept out of an inline x-data because
// |tojson emits double quotes that would close a double-quoted attribute and break
// Alpine init (see CLAUDE.md Alpine-quoting anti-pattern).
Alpine.data('rfqVendorModal', (suggestedNames, requirementIds) => ({
  step: 'compose',
  // Selection state as a plain reactive object keyed by vendor name (NOT a Set) — matches
  // the sightingSelection store and the project's Alpine-reactivity guidance: Alpine tracks
  // object key add/delete reliably, Set mutations less so.
  selectedVendors: Object.fromEntries((suggestedNames || []).map((n) => [n, true])),
  requirementIds: requirementIds || [],
  // Opt-in datasheet attachment ids (array of integers). Included in _form() so the
  // send-inquiry route can resolve + fetch + encode them. Same list sent to EVERY vendor.
  selectedDatasheetIds: [],
  emailBody: '',
  previewing: false,
  sending: false,

  // ── Any-vendor picker + inline create (bulk composer spec Part 2 §3/§4) ──
  vendorQuery: '',
  vendorResults: [],
  searchOpen: false,
  addingVendor: false,
  addingVendorBusy: false,
  newVendorName: '',
  newVendorWebsite: '',
  newVendorEmail: '',

  get selectedCount() {
    return Object.keys(this.selectedVendors).length;
  },
  isSelected(name) {
    return !!this.selectedVendors[name];
  },
  toggleVendor(name) {
    if (this.selectedVendors[name]) delete this.selectedVendors[name];
    else this.selectedVendors[name] = true;
  },
  // Server-returned composer rows (composer_vendor_row.html) x-init through here so
  // they arrive CHECKED — runtime-added keys flow into vendor_names via _form().
  selectVendor(name) {
    this.selectedVendors[name] = true;
  },

  // Toggle a datasheet id in/out of selectedDatasheetIds (opt-in attachment list).
  toggleDatasheet(id) {
    const idx = this.selectedDatasheetIds.indexOf(id);
    if (idx >= 0) this.selectedDatasheetIds.splice(idx, 1);
    else this.selectedDatasheetIds.push(id);
  },

  // Debounced (template-side @input.debounce.300ms) lookup against the existing
  // /api/autocomplete/names endpoint. It mixes vendors + customers — filter to
  // vendors client-side; never fork the endpoint. A failure is VISIBLE (toast),
  // but only once per failure streak — debounced keystrokes would otherwise
  // stack identical toasts; a success resets the flag.
  _searchErrorToasted: false,
  async searchVendors() {
    const q = this.vendorQuery.trim();
    if (q.length < 2) {
      this.vendorResults = [];
      this.searchOpen = false;
      return;
    }
    try {
      const resp = await fetch('/api/autocomplete/names?q=' + encodeURIComponent(q));
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const items = await resp.json();
      this.vendorResults = (items || []).filter((it) => it.type === 'vendor');
      this.searchOpen = true;
      this._searchErrorToasted = false;
    } catch (err) {
      console.error('[rfqVendorModal] vendor search failed', err);
      this.vendorResults = [];
      this.searchOpen = false;
      if (!this._searchErrorToasted) {
        this._searchErrorToasted = true;
        this._toast('Vendor search failed — please try again', 'error');
      }
    }
  },

  async pickVendor(name) {
    this.searchOpen = false;
    this.vendorQuery = '';
    this.vendorResults = [];
    await this._addComposerVendor({ vendor_name: name });
  },

  async createVendor() {
    if (!this.newVendorName.trim() || this.addingVendorBusy) return;
    this.addingVendorBusy = true;
    try {
      const ok = await this._addComposerVendor({
        vendor_name: this.newVendorName.trim(),
        website: this.newVendorWebsite.trim(),
        email: this.newVendorEmail.trim(),
      });
      if (ok) {
        this.newVendorName = '';
        this.newVendorWebsite = '';
        this.newVendorEmail = '';
        this.addingVendor = false;
      }
    } finally {
      this.addingVendorBusy = false;
    }
  },

  // "Add contact" on a non-contactable (cardless / emailless) suggested row: reveal the
  // existing inline "Add new vendor" form pre-filled with this vendor's display name and
  // focus the email input — the buyer types the known email and the existing
  // composer-vendor POST (createVendor) creates the card + VendorContact. No new endpoint.
  // Only seed the name when the field is empty so a half-typed manual entry survives a
  // click on this action (L2 — don't clobber in-progress input). $nextTick waits for
  // x-show to mount the form before focusing the (now-visible) input.
  addContactFor(name) {
    if (!this.newVendorName.trim()) this.newVendorName = name || '';
    this.addingVendor = true;
    this.$nextTick(() => this.$refs.newVendorEmail?.focus());
  },

  // Fast-path dedup: true when `name` matches a selection key case-insensitively.
  // Keys are server-NORMALIZED names (suffixes stripped) while picker/typed names
  // are display names, so this only catches exact/case matches — the authoritative
  // check in _addComposerVendor re-tests the server's normalized name from the row.
  _isVendorSelected(name) {
    const q = (name || '').trim().toLowerCase();
    return Object.keys(this.selectedVendors).some((k) => k.toLowerCase() === q);
  },

  // Extract the server-normalized vendor name from a composer_vendor_row.html
  // payload. Both row branches carry a data-vendor-norm attribute (excluded rows
  // have no x-init, so the attribute is their ONLY carrier); the x-init
  // selectVendor("<normalized>") parse stays as a fallback for selectable rows.
  // Parsed detached via DOMParser (no script execution, no insert).
  _rowVendorName(html) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const norm = doc.querySelector('[data-vendor-norm]')?.getAttribute('data-vendor-norm');
    if (norm) return norm;
    const xInit = doc.querySelector('[x-init]')?.getAttribute('x-init') || '';
    const m = xInit.match(/selectVendor\(("(?:[^"\\]|\\.)*")\)/);
    if (!m) return null;
    try {
      return JSON.parse(m[1]);
    } catch {
      return null;
    }
  },

  // True when #rfq-added-vendors already holds a row for this normalized name —
  // the dedupe for EXCLUDED rows, which never join selectedVendors (disabled
  // checkbox) and would otherwise stack duplicates on repeated picks.
  _containerHasVendor(norm) {
    const container = document.querySelector('#rfq-added-vendors');
    if (!container) return false;
    return Array.from(container.querySelectorAll('[data-vendor-norm]')).some(
      (el) => el.getAttribute('data-vendor-norm') === norm,
    );
  },

  // POST to composer-vendor and append the returned row into the stable-id
  // #rfq-added-vendors sub-container INSIDE this x-data wrapper (explicit container —
  // swapping the wrapper would re-init rfqVendorModal and wipe selection state).
  // Raw fetch + manual insert (mirrors confirmSend / customerPicker.lookupCompany)
  // so a server 4xx is DETECTED: htmx.ajax resolves on HTTP errors, which used to
  // clear the inline create form on a 400. Returns true only when the vendor ended
  // up selected (row appended, or already present — duplicate picks skip the
  // append, INCLUDING excluded rows via the container check, so #rfq-added-vendors
  // never shows the same vendor twice); false on any error so createVendor keeps
  // the typed values.
  async _addComposerVendor(fields) {
    // Bare picks only: a createVendor submission carrying an email/website must
    // reach the server even when the name matches a selection — the server
    // attaches the typed email/domain to the existing card; skipping here would
    // silently discard the input.
    if (!fields.email && !fields.website && this._isVendorSelected(fields.vendor_name)) {
      this._toast('Vendor already added', 'info');
      return true;
    }
    const form = new FormData();
    form.append('vendor_name', fields.vendor_name);
    if (fields.website) form.append('website', fields.website);
    if (fields.email) form.append('email', fields.email);
    this.requirementIds.forEach((id) => form.append('requirement_ids', id));
    const spinner = document.querySelector('#rfq-added-vendors-spinner');
    spinner?.classList.add('htmx-request');
    try {
      // starlette_csrf requires the x-csrftoken header on POST (mirrors confirmSend).
      const resp = await fetch('/v2/partials/sightings/composer-vendor', {
        method: 'POST',
        headers: { 'x-csrftoken': csrfToken() },
        body: form,
      });
      if (!resp.ok) {
        // The server emits the repo JSON error format ({"error": ...}). A 4xx
        // reason is actionable user input ("invalid website — ...") — surface it
        // verbatim; 5xx bodies are internals, keep the generic try-again.
        let reason = '';
        try {
          reason = (await resp.json()).error || '';
        } catch {
          /* non-JSON / empty body — fall through to the generic message */
        }
        console.error('[rfqVendorModal] add vendor failed: HTTP ' + resp.status, reason);
        const msg = resp.status < 500 && reason
          ? 'Could not add vendor: ' + reason
          : 'Could not add vendor — please try again';
        this._toast(msg, 'error');
        return false;
      }
      const html = await resp.text();
      // Authoritative dedup on the server-normalized name: picking
      // "Mouser Electronics, Inc." when "mouser electronics" is already selected
      // would append a duplicate row while selection state stays unchanged.
      // Excluded rows never enter selectedVendors, so they dedupe against the
      // rows already in the container instead.
      const normalized = this._rowVendorName(html);
      if (normalized && (this.selectedVendors[normalized] || this._containerHasVendor(normalized))) {
        this._toast('Vendor already added', 'info');
        return true;
      }
      const container = document.querySelector('#rfq-added-vendors');
      // Server HTML is trusted (same-origin, auth-protected endpoint).
      container.insertAdjacentHTML('beforeend', html);
      htmx.process(container);
      // The appended row carries Alpine directives (x-init='selectVendor(...)',
      // :checked, @change) that bind to THIS rfqVendorModal x-data scope. htmx.process
      // only wires htmx attributes, not Alpine's, and relying on Alpine 3's
      // MutationObserver is exactly the unreliable path the afterSwap handler warns
      // about — explicitly initTree the new node so the row arrives CHECKED and its
      // checkbox is live (matches the lead-drawer / rq2-table workaround).
      const addedRow = container.lastElementChild;
      if (addedRow && typeof Alpine !== 'undefined' && typeof Alpine.initTree === 'function') {
        Alpine.initTree(addedRow);
      }
      return true;
    } catch (err) {
      console.error('[rfqVendorModal] add vendor failed', err);
      this._toast('Could not add vendor — please try again', 'error');
      return false;
    } finally {
      spinner?.classList.remove('htmx-request');
    }
  },

  // Build a FormData with REPEATED keys for the multi-valued fields. (Object.fromEntries
  // on a FormData silently collapses duplicate keys to the last value — that would send
  // only one requirement_id / vendor_name.) htmx.ajax accepts a FormData for `values`
  // as-is, and fetch sends it directly.
  _form() {
    const form = new FormData();
    this.requirementIds.forEach((id) => form.append('requirement_ids', id));
    Object.keys(this.selectedVendors).forEach((v) => form.append('vendor_names', v));
    form.append('email_body', this.emailBody);
    // Opt-in datasheet attachment ids (integers). Empty selection → no fields posted
    // → server treats as no attachments (regression-safe).
    this.selectedDatasheetIds.forEach((id) => form.append('datasheet_ids', id));
    return form;
  },

  _toast(message, type) {
    // Toast store is { message, type, show } — set fields directly; show is a boolean.
    this.$store.toast.message = message;
    this.$store.toast.type = type;
    this.$store.toast.show = true;
  },

  async loadPreview() {
    if (this.selectedCount === 0 || !this.emailBody || this.previewing) return;
    this.previewing = true;
    try {
      await htmx.ajax('POST', '/v2/partials/sightings/preview-inquiry', {
        target: this.$refs.previewContent,
        swap: 'innerHTML',
        indicator: this.$refs.previewContent,
        values: this._form(),
      });
      // preview_inquiry.html contains Alpine x-data / x-model / @rfq-email-fixed.window
      // directives for the inline fix-email mini-form. htmx.ajax swaps innerHTML but does
      // not run Alpine on new nodes — the afterSwap handler only covers its hardcoded id
      // allowlist (lead-drawer-content, rq2-table, rfq-affinity-section). previewContent
      // has no id, so we must explicitly initTree here to bind the fix-email component.
      if (this.$refs.previewContent && typeof Alpine !== 'undefined' && typeof Alpine.initTree === 'function') {
        Alpine.initTree(this.$refs.previewContent);
      }
      this.step = 'preview';
    } catch (err) {
      console.error('[rfqVendorModal] preview failed', err);
      this._toast('Preview failed — please try again', 'error');
    } finally {
      this.previewing = false;
    }
  },

  // One-click skip remediation from the preview step: attach a contact email to a
  // previously-skipped (no-email) vendor then re-run preview so the vendor resolves.
  // POSTs to the existing composer-vendor endpoint (which creates/updates the
  // VendorContact). On non-ok response, shows a toast and keeps the inline form open
  // by NOT calling loadPreview(). On success, selectVendor() ensures the vendor is
  // in selectedVendors, then loadPreview() refreshes the preview panel in-place
  // (no modal close or wrapper re-init — the preview container is a stable-id swap).
  async fixVendorEmail(vendorName, email) {
    if (!email || !vendorName) return;
    const form = new FormData();
    form.append('vendor_name', vendorName);
    form.append('email', email);
    this.requirementIds.forEach((id) => form.append('requirement_ids', id));
    try {
      const resp = await fetch('/v2/partials/sightings/composer-vendor', {
        method: 'POST',
        headers: { 'x-csrftoken': csrfToken() },
        body: form,
      });
      if (!resp.ok) {
        let reason = '';
        try { reason = (await resp.json()).error || ''; } catch { /* non-JSON body */ }
        const msg = resp.status < 500 && reason
          ? 'Could not add email: ' + reason
          : 'Could not add email — please try again';
        this._toast(msg, 'error');
        return; // keep the form open with the typed value
      }
      // Ensure the vendor is in selectedVendors so it is included in the re-preview POST.
      this.selectVendor(vendorName);
      // Signal the nested x-data scope to clear its fixEmail input (success path only).
      window.dispatchEvent(new CustomEvent('rfq-email-fixed'));
      await this.loadPreview();
    } catch (err) {
      console.error('[rfqVendorModal] fixVendorEmail failed', err);
      this._toast('Could not add email — please try again', 'error');
    }
  },

  async confirmSend() {
    if (this.selectedCount === 0 || !this.emailBody || this.sending) return;
    this.sending = true;
    const count = this.selectedCount;
    try {
      // Raw fetch so we can read the result headers below. starlette_csrf requires the
      // x-csrftoken header on POST (mirrors quoteBuilder).
      const resp = await fetch('/v2/partials/sightings/send-inquiry', {
        method: 'POST',
        headers: { 'x-csrftoken': csrfToken() },
        body: this._form(),
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      // The route returns 200 even on a partial/total send failure, so report the TRUE
      // outcome from the X-RFQ-* headers rather than assuming success.
      const sent = parseInt(resp.headers.get('X-RFQ-Sent') || '0', 10);
      const total = parseInt(resp.headers.get('X-RFQ-Total') || String(count), 10);
      const skipped = parseInt(resp.headers.get('X-RFQ-Skipped') || '0', 10);
      // X-RFQ-Unavailable = vendors dropped by the send-time unavailability re-check.
      // They are NOT delivery failures — without subtracting them they'd be
      // misattributed to the 'failed' bucket (total - sent - skipped).
      const unavailable = parseInt(resp.headers.get('X-RFQ-Unavailable') || '0', 10);
      // X-RFQ-Datasheets-Dropped = oversized datasheets silently dropped before send.
      const datasheetsDropped = parseInt(resp.headers.get('X-RFQ-Datasheets-Dropped') || '0', 10);
      const outcome = this._sendOutcome(sent, total, skipped, unavailable, datasheetsDropped);
      this._toast(outcome.message, outcome.type);
      if (!outcome.delivered) return; // nothing sent — keep the modal open to retry
      this._refreshSightings();
      this.$dispatch('close-modal');
    } catch (err) {
      console.error('[rfqVendorModal] send failed', err);
      this._toast('Send failed — please try again', 'error');
    } finally {
      this.sending = false;
    }
  },

  // Map the server's sent/total/skipped/unavailable/datasheetsDropped counts to a toast.
  // `delivered` is false only when nothing went out, so the caller can keep the modal open
  // for a retry. `skipped` = vendors with no contact email; `unavailable` = vendors dropped
  // by the send-time unavailability re-check; `datasheetsDropped` = attachments silently
  // dropped for exceeding the ~3 MB Graph simple-send cap (largest-first).
  _sendOutcome(sent, total, skipped = 0, unavailable = 0, datasheetsDropped = 0) {
    if (sent === 0) {
      return { type: 'error', delivered: false, message: 'Send failed — no RFQs were delivered' };
    }
    let baseMsg;
    if (sent < total) {
      const failed = total - sent - skipped - unavailable;
      const reasons = [];
      if (failed > 0) reasons.push(failed + ' failed');
      if (skipped > 0) reasons.push(skipped + ' had no email');
      if (unavailable > 0) reasons.push(unavailable + ' marked unavailable');
      baseMsg = 'Sent to ' + sent + ' of ' + total + ' vendors' + (reasons.length ? ' — ' + reasons.join(', ') : '');
    } else {
      baseMsg = 'RFQ sent to ' + sent + ' vendor' + (sent === 1 ? '' : 's');
    }
    if (datasheetsDropped > 0) {
      baseMsg += ' (' + datasheetsDropped + ' attachment' + (datasheetsDropped === 1 ? '' : 's') + ' dropped — too large)';
    }
    return {
      type: sent < total ? 'warning' : 'success',
      delivered: true,
      message: baseMsg,
    };
  },

  // A successful send can change BOTH the open detail panel (status pill auto-advances
  // OPEN→SOURCING, new "RFQ sent" activity rows) and the requirements list. Refresh
  // whichever is on screen.
  _refreshSightings() {
    // Best-effort refresh of the open panel + list after a successful send. htmx.ajax
    // rejects only on network/timeout/target errors (HTTP 4xx/5xx are surfaced by the
    // global htmx:responseError toast registered above), so this .catch covers the
    // connection-failure case with a clearer "you already sent" message.
    const onRefreshError = (err) => {
      console.error('[rfqVendorModal] post-send refresh failed', err);
      this._toast('Sent — refresh the page to see updated status', 'warning');
    };
    const selectedReqId = Alpine.store('sightingSelection')?.selectedReqId;
    if (selectedReqId) {
      htmx.ajax('GET', '/v2/partials/sightings/' + selectedReqId + '/detail', {
        target: '#sightings-detail',
        swap: 'innerHTML',
        indicator: '#sightings-detail-skeleton',
      }).catch(onRefreshError);
    }
    const table = document.getElementById('sightings-table');
    const tableUrl = table && table.getAttribute('hx-get');
    if (tableUrl) {
      htmx.ajax('GET', tableUrl, {
        target: '#sightings-table',
        swap: 'innerHTML',
        indicator: '#sightings-load-spinner',
      }).catch(onRefreshError);
    }
  },
}));

// ── offerQualification: condition-driven offer form (chip panels + note preview + meter) ──
// Rendered by sightings/offer_form_modal.html and requisitions/add_offer_form.html.
// x-data attribute on the <form> must be SINGLE-quoted with |tojson so that prefill
// values containing quotes or special chars cannot break Alpine init.
//
// noteText() mirrors server compose_note() byte-for-byte:
//   - Chip values are sent as-is (e.g. "Tape & Reel"); server normalizes via
//     normalize_packaging() then humanizes via _PKG_DISPLAY. The JS _pkgDisplay map
//     replicates that two-step so preview == stored note for all six chips.
//
// _items() mirrors server _items_for() per condition:
//   new:        [manufacturer, package_type(=any non-empty packaging), date_code] — no images
//   new_no_pkg: [packaging, images=false, date_code]
//   pulls:      [packaging, usage, images=false, part_condition]
//   refurb:     [refurbished_by, refurb_process, images=false] + cert_doc if third_party
Alpine.data('offerQualification', (prefill) => ({
  condition: (prefill && prefill.condition) || '',
  packaging: (prefill && prefill.packaging) || '',
  usage: (prefill && prefill.usage) || '',
  refurbished_by: (prefill && prefill.refurbished_by) || '',
  cert_doc: (prefill && prefill.cert_doc) || '',
  refurb_process: (prefill && prefill.refurb_process) || '',
  part_condition: (prefill && prefill.part_condition) || '',
  manufacturer: (prefill && prefill.manufacturer) || '',
  date_code: (prefill && prefill.date_code) || '',
  _pkgChips: ['Tape & Reel', 'Reels', 'Trays', 'Tubes', 'Antistatic bags', 'Boxes'],
  // Map chip value → humanized display label, mirroring normalize_packaging + _PKG_DISPLAY on the server.
  // normalize_packaging("Tape & Reel") → "reel"; _PKG_DISPLAY["reel"] → "Reels"
  // normalize_packaging("Reels")       → "reel"; _PKG_DISPLAY["reel"] → "Reels"
  // normalize_packaging("Trays")       → "tray"; _PKG_DISPLAY["tray"] → "Trays"
  // normalize_packaging("Tubes")       → "tube"; _PKG_DISPLAY["tube"] → "Tubes"
  // normalize_packaging("Antistatic bags") → "bag"; _PKG_DISPLAY["bag"] → "Antistatic bags"
  // normalize_packaging("Boxes")       → "box";  _PKG_DISPLAY["box"]  → "Boxes"
  _pkgDisplay: {
    'Tape & Reel': 'Reels',
    'Reels': 'Reels',
    'Trays': 'Trays',
    'Tubes': 'Tubes',
    'Antistatic bags': 'Antistatic bags',
    'Boxes': 'Boxes',
  },
  essentialsMet() {
    const c = this.condition;
    if (!c) return true; // unset is allowed to save
    if (c === 'new') return !!this.manufacturer.trim();
    if (c === 'new_no_pkg') return this._pkgOk();
    if (c === 'pulls') return this._pkgOk() && (this.usage === 'boards' || this.usage === 'systems');
    if (c === 'refurb') return (this.refurbished_by === 'supplier' || this.refurbished_by === 'third_party') && !!this.refurb_process.trim();
    return true;
  },
  _pkgOk() { return this._pkgChips.includes(this.packaging); },
  // Returns the display label for the current packaging chip, mirroring server _PKG_DISPLAY.
  _pkgLabel() { return this._pkgDisplay[this.packaging] || this.packaging; },
  noteText() {
    const c = this.condition;
    const pkg = this._pkgLabel();
    if (c === 'new') return "New — parts are in the original manufacturer's packaging.";
    if (c === 'new_no_pkg') {
      return pkg
        ? `New, no original manufacturer packaging. Packaged in ${pkg}.`
        : 'New, no original manufacturer packaging.';
    }
    if (c === 'pulls') {
      const u = this.usage === 'boards' ? 'boards' : this.usage === 'systems' ? 'systems' : '';
      let n;
      if (pkg && u) n = `Pulls — packaged in ${pkg}, pulled from ${u}.`;
      else if (pkg) n = `Pulls — packaged in ${pkg}.`;
      else if (u) n = `Pulls — pulled from ${u}.`;
      else n = 'Pulls.';
      const pc = this.part_condition.trim();
      return pc ? `${n} Condition: ${pc}.` : n;
    }
    if (c === 'refurb') {
      const who = this.refurbished_by === 'supplier' ? 'the supplier'
        : this.refurbished_by === 'third_party' ? 'a third party' : '';
      let n = who ? `Refurbished by ${who}.` : 'Refurbished.';
      const proc = this.refurb_process.trim();
      if (proc) n += ` Process: ${proc}.`;
      if (this.refurbished_by === 'third_party') {
        if (this.cert_doc === 'yes') n += ' Certifying document on file.';
        else if (this.cert_doc === 'no') n += ' No certifying document.';
      }
      return n;
    }
    return '';
  },
  // Mirrors server _items_for(condition, data, has_images) with has_images=false (no attachments at entry).
  _items() {
    const c = this.condition;
    const pkgOk = this._pkgOk();
    const dcOk = !!this.date_code.trim();
    // For condition=new the server counts package_type as any non-empty packaging string
    // (free-text in "More details"), NOT chip-membership — mirror bool(_s(data,"packaging")).
    if (c === 'new') return [!!this.manufacturer.trim(), !!this.packaging.trim(), dcOk];
    if (c === 'new_no_pkg') return [pkgOk, false, dcOk];
    if (c === 'pulls') return [pkgOk, this.usage === 'boards' || this.usage === 'systems', false, !!this.part_condition.trim()];
    if (c === 'refurb') {
      const a = [
        this.refurbished_by === 'supplier' || this.refurbished_by === 'third_party',
        !!this.refurb_process.trim(),
        false,
      ];
      if (this.refurbished_by === 'third_party') a.push(this.cert_doc === 'yes' || this.cert_doc === 'no');
      return a;
    }
    return [];
  },
  meterTotal() { return this._items().length; },
  meterFilled() { return this._items().filter(Boolean).length; },
}));

/**
 * attachmentsPanel — Alpine.js component for the unified file-attachments panel.
 *
 * Owns the dropzone hover state, a friendly busy state during upload, and the
 * drop handler that assigns dropped files to the picker input and submits the
 * form. The form itself is plain HTMX (multipart POST → attachments:changed);
 * this factory only decorates it with interaction state.
 *
 * Called by: partials/shared/_attachments.html (attachments_panel macro)
 * Depends on: Alpine.js, HTMX. Error toasts are surfaced by the global
 *             htmx:responseError handler (reads body.error) — no per-panel wiring.
 */
Alpine.data('attachmentsPanel', () => ({
  dragging: false,
  busy: false,
  busyLabel: 'Uploading…',

  init() {
    // The dropzone form is this component's root (<div> wraps it); listen on the
    // root so both the upload form and the list container's requests are seen.
    // Only the multipart upload toggles the busy state.
    this.$el.addEventListener('htmx:beforeRequest', (e) => {
      if (e.target && e.target.tagName === 'FORM') this.busy = true;
    });
    this.$el.addEventListener('htmx:afterRequest', (e) => {
      if (e.target && e.target.tagName === 'FORM') this.busy = false;
    });
  },

  onDrop(evt) {
    this.dragging = false;
    const files = evt.dataTransfer && evt.dataTransfer.files;
    if (!files || !files.length) return;
    this.$refs.fileInput.files = files;
    this.$refs.fileInput.closest('form').requestSubmit();
  },
}));

/* ────────────────────────────────────────────────────────────────────────
   Cross-app tab alerts — in-tab spotlight for new / actionable rows.

   List rows carrying data-alert-new (stamped by _alert_macros.html) get an
   emerald accent rail; the page glides to the first, and each row is marked
   seen as it scrolls into view. FYI rows fade their rail and drain the badge;
   ACTION rows keep the rail (the work-state count owns it). A floating pill
   jumps between the still-unviewed rows. Reuses the proactive emerald palette.
   ──────────────────────────────────────────────────────────────────────── */
(() => {
  const PILL_ID = 'tab-alert-pill';

  const scopeEl = () => document.getElementById('main-content') || document;

  // Refs the client has already consumed this session — authoritative over the server's
  // eventually-consistent seen-state, so a list refresh that races an in-flight seen-ping
  // cannot resurrect a row and hijack the scroll.
  const consumedRefs = new Set();

  const refsOf = (row) =>
    (row.getAttribute('data-alert-refs') || '').split(',').map((s) => s.trim()).filter(Boolean);

  const pendingRows = () =>
    Array.from(scopeEl().querySelectorAll('[data-alert-new]:not([data-alert-consumed])'));

  const prefersReducedMotion = () =>
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const glideTo = (el) => {
    if (el) el.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
  };

  const ensurePill = () => {
    let pill = document.getElementById(PILL_ID);
    if (pill) return pill;
    pill = document.createElement('button');
    pill.id = PILL_ID;
    pill.type = 'button';
    pill.className = 'tab-alert-pill';
    pill.style.display = 'none';
    pill.addEventListener('click', () => glideTo(pendingRows()[0]));
    document.body.appendChild(pill);
    return pill;
  };

  const refreshPill = () => {
    const pill = ensurePill();
    const n = pendingRows().length;
    if (n === 0) { pill.style.display = 'none'; return; }
    pill.textContent = `${n} new ↓`;
    pill.style.display = '';
  };

  const markSeen = (kind, refs) => {
    if (!window.htmx || !refs.length) return;
    const url = `/v2/partials/alerts/${encodeURIComponent(kind)}/seen`;
    const body = { ref_ids: refs.join(',') };
    // One background ping per row (all its refs batched): no spinner (indicator: null);
    // htmx.ajax still applies the OOB nav-badge swap from the response.
    window.htmx.ajax('POST', url, { target: 'body', swap: 'none', indicator: null, values: body });
  };

  const consume = (row) => {
    if (row.dataset.alertConsumed) return;
    row.dataset.alertConsumed = '1';
    const kind = row.getAttribute('data-alert-kind');
    const refs = refsOf(row);
    refs.forEach((r) => consumedRefs.add(r));
    markSeen(kind, refs);
    if (row.getAttribute('data-alert-temperament') === 'fyi') {
      row.classList.remove('alert-rail-pulse');
      row.classList.add('alert-rail-fade');
      setTimeout(() => row.classList.remove('alert-rail', 'alert-rail-fade'), 700);
    }
    // ACTION rows keep .alert-rail — the work-state badge owns the count.
    refreshPill();
  };

  let observer = null;
  const getObserver = () => {
    if (observer) return observer;
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          consume(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.6 });
    return observer;
  };

  const spotlight = (root) => {
    const scope = root && root.querySelectorAll ? root : scopeEl();
    const rows = Array.from(scope.querySelectorAll('[data-alert-new]:not([data-alert-spotlit])'));
    if (!rows.length) { refreshPill(); return; }
    const obs = getObserver();
    let firstFresh = null;
    rows.forEach((row) => {
      row.dataset.alertSpotlit = '1';
      const refs = refsOf(row);
      // A row whose every ref we've already consumed is a refresh-resurrected row (the
      // server's seen-state hadn't caught up yet) — settle it silently: no rail, no glide.
      if (refs.length && refs.every((r) => consumedRefs.has(r))) {
        row.dataset.alertConsumed = '1';
        return;
      }
      row.classList.add('alert-rail', 'alert-rail-pulse');
      obs.observe(row);
      if (!firstFresh) firstFresh = row;
    });
    refreshPill();
    // Glide only to a genuinely-fresh row — never on a refresh that surfaced no new work.
    if (firstFresh) setTimeout(() => glideTo(firstFresh), 140);
  };

  document.body.addEventListener('htmx:afterSettle', (evt) => {
    spotlight(evt.detail ? evt.detail.elt : document);
  });
  document.addEventListener('DOMContentLoaded', () => spotlight(document));
})();

Alpine.start();
