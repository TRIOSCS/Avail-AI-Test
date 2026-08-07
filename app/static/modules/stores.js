/**
 * stores.js — global Alpine stores (toast, errorLog, networkLog, callOutcome,
 * shortlist, sightingSelection) plus the error/console/network capture that
 * feeds the trouble-ticket logs.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs, ./globals.js (showToast, pushCappedLog).
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';
import { showToast, pushCappedLog } from './globals.js';

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
        window.postJSON('/api/activity/' + id + '/call-outcome', { outcome: outcome, note: note }).then((resp) => {
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
