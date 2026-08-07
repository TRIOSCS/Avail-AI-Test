/**
 * trouble_tickets.js — trouble-ticket capture & reporting: nav-history breadcrumb,
 * lazy screenshot capture (dynamic import('modern-screenshot') — its own Vite
 * chunk), context bundle, the report modal open/submit globals, and the admin
 * bulk-action helper. All entry points live on window.* because templates call
 * them inline (mobile_nav.html, trouble_report_form.html, tickets/workspace.html).
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs, window.postJSON (./globals.js), modern-screenshot (lazy).
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';

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
// painted out before capture. `kind` is 'bug' (default, Report a Problem) or
// 'feature' (Request a Feature) — it drives the form copy and the ticket_type sent
// on submit. Both kinds capture the same screenshot + context.
window.openTroubleReport = async function openTroubleReport(kind) {
    kind = (kind === 'feature') ? 'feature' : 'bug';
    window._ttKind = kind;
    await new Promise(function(r) {
        requestAnimationFrame(function() { requestAnimationFrame(r); });
    });
    window._ttScreenshot = await window.captureTroubleScreenshot();
    window._ttContext = window.collectTroubleContext();
    const url = '/api/trouble-tickets/form' + (kind === 'feature' ? '?type=feature' : '');
    window.dispatchEvent(new CustomEvent('open-modal', { detail: { url: url } }));
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
    window.postJSON('/api/trouble-tickets/submit', {
        description: desc,
        ticket_type: window._ttKind || 'bug',
        screenshot: window._ttScreenshot || null,
        page_url: window.location.href,
        user_agent: navigator.userAgent,
        viewport: window.innerWidth + 'x' + window.innerHeight,
        error_log: JSON.stringify(Alpine.store('errorLog').entries),
        network_log: JSON.stringify(Alpine.store('networkLog').entries),
        auto_captured_context: window._ttContext ? JSON.stringify(window._ttContext) : null,
    }).then(function(resp) {
        htmx.swap('#modal-content', resp.text, { swapStyle: 'innerHTML' });
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
    const payload = { ticket_ids: ids };
    if (status) payload.status = status;
    return window.postJSON('/api/trouble-tickets/' + kind, payload).then(function(r) {
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
