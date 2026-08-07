/**
 * htmx_wiring.js — app-wide htmx configuration and event wiring: htmx.config
 * defaults, the CSRF header injection, display-timezone auto-detect, the
 * responseError/timeout/sendError toasts, the server-driven showToast bridge,
 * the #sightings-detail stale-response guard + clickPending decrement, the
 * 401→login and 422→modal beforeSwap handlers, the page loading bar (with the
 * Alpine.initTree re-init allowlist), and the global keyboard shortcuts.
 *
 * Module-scope side effects — must stay a STATIC import so the listeners are
 * registered before the first htmx request.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs, ./globals.js (csrfToken, showToast), window.postForm.
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';
import { csrfToken, showToast } from './globals.js';

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

// ── Per-user display timezone auto-detect ───────────────────
// Once per page load, read the browser's IANA zone and, ONLY if it differs from the
// zone already stored on the user (rendered onto <body data-user-tz>), post it so
// timestamps render in the viewer's own timezone. The endpoint no-ops when unchanged;
// this guard avoids a POST on every navigation. Fire-and-forget: the response body is
// ignored (so the endpoint's HX-Trigger toast, which the profile <select> shows, stays
// silent here).
function syncDisplayTimezone() {
    let browserTz = '';
    try {
        browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    } catch (_) { /* Intl unavailable — skip */ }
    if (!browserTz) return;
    const storedTz = document.body.dataset.userTz || '';
    if (browserTz === storedTz) return;
    window.postForm('/v2/profile/timezone', { timezone: browserTz }).then((resp) => {
        // Reflect locally so a second navigation in this session doesn't re-post.
        if (resp.ok) document.body.dataset.userTz = browserTz;
    }).catch(() => { /* fire-and-forget — a failed detect just retries next load */ });
}
document.addEventListener('DOMContentLoaded', syncDisplayTimezone);

// ── HTMX error handler — show toast on failed requests ──────
htmx.on('htmx:responseError', (evt) => {
    const status = evt.detail.xhr && evt.detail.xhr.status;
    // 409 = conflict: the SERVER owns 409 messaging app-wide. Stale-edit guard 409s
    // (services/stale_guard.py) attach their own HX-Trigger showToast, and every other
    // HTTPException(409) gets one centrally in app/main.py's http_exception_handler
    // (plus HX-Reswap: none). The trigger is bridged below, so the generic error toast
    // here would double-toast. Skip it.
    if (status === 409) {
        return;
    }
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
    // (lead drawer close button;
    // rfq-affinity-section — affinity rows whose :checked/@change checkboxes bind to
    // the surrounding rfqVendorModal x-data scope, otherwise the checkboxes are inert
    // and ticked affinity vendors never enter selectedVendors / never get sent;
    // settings-content — the Settings tab body is lazy-swapped here and re-swapped by
    // every settings mutation (e.g. a dedup merge re-renders Data Ops), so its Alpine
    // directives — the Data Ops multi-select bar — must re-init or the checkboxes go
    // inert and selection state is lost after the first action;
    // proactive-contact-list — the Prepare page add-contact POST swaps the re-rendered
    // picker here, whose :checked/@change checkboxes bind to the surrounding prepare
    // x-data scope and whose new row carries an x-init auto-select; without re-init the
    // checkboxes go inert and the new contact never selects (Send stays disabled).
    if (t && typeof Alpine !== 'undefined' && typeof Alpine.initTree === 'function') {
        if (
            t.id === 'lead-drawer-content' ||
            t.id === 'rfq-affinity-section' ||
            t.id === 'settings-content' ||
            t.id === 'proactive-contact-list'
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
