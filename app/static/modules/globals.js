/**
 * globals.js — platform layer for the AvailAI frontend bundle.
 *
 * Owns the vendor imports (htmx + Alpine + plugins + htmx extensions + styles),
 * Alpine plugin registration, the window.htmx/window.Alpine globals templates
 * call into, the shared helpers (csrfToken/showToast/pushCappedLog), and the
 * postJSON/postForm fire-and-forget POST wrappers (window.postJSON/postForm).
 *
 * MUST be the FIRST import in htmx_app.js: ESM import hoisting means the
 * window.* assignments here have to run before any other module evaluates.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry);
 *   the sibling modules import the exported helpers.
 * Depends on: htmx.org, alpinejs, all @alpinejs/* plugins, all htmx-ext-* packages.
 */

// ── Core ─────────────────────────────────────────────────────
import htmx from 'htmx.org';
import Alpine from 'alpinejs';

// ── Alpine.js Official Plugins ───────────────────────
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
// JSON-enc: encode request body as JSON instead of form-encoded
import 'htmx-ext-json-enc';
// Remove-me: auto-remove elements after a timeout (flash messages, temp alerts)
import 'htmx-ext-remove-me';
// Restored: trigger events when back-button restores a page from cache
import 'htmx-ext-restored';
// Idiomorph: smart DOM morphing algorithm by HTMX team (alternative swap strategy)
import 'idiomorph';
import 'idiomorph/dist/idiomorph-ext.esm.js';

// ── Styles ───────────────────────────────────────────────────
import '../styles.css';
import '../htmx_mobile.css';

// ── Register all Alpine plugins ──────────────────────────────
// Order matters: register plugins BEFORE Alpine.start()
Alpine.plugin(focus);      // x-trap (backwards compat) + x-focus
Alpine.plugin(persist);    // $persist
Alpine.plugin(intersect);  // x-intersect
Alpine.plugin(collapse);   // x-collapse
Alpine.plugin(morph);      // Alpine.morph()

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

// ── postJSON: canonical helper for fire-and-forget JSON POSTs ───────
// Purpose: single wrapper over htmx.ajax for the small set of JSON-POST call
// sites that need a response status/body but aren't swapping HTML into a
// visible target (trouble-ticket submit/bulk actions, call-outcome
// log). Replaces hand-rolled fetch() + manual CSRF header +
// JSON.stringify at each site — CSRF is already injected for every htmx
// request by the htmx:configRequest listener above.
//
// htmx's own ajax() helper returns a Promise that carries no response data
// (htmx resolves it with no value once the request settles — see htmx.org's
// issueAjaxRequest),
// so this creates a throwaway, unattached-to-any-feature <div> as the request's
// source/target (swap: 'none' — nothing is ever painted from it), listens once
// for the htmx:afterRequest event htmx fires ON THAT ELEMENT (never on
// document.body, since each call gets its own element — safe under
// concurrent in-flight calls), and resolves a fetch-Response-shaped object
// read off the real XMLHttpRequest.
//
// JSON encoding is done via the bundled json-enc extension (hx-ext="json-enc"),
// activated only on the throwaway element so it doesn't affect any other HTMX
// request on the page. The payload is passed through hx-vals (JSON.parse'd by
// htmx) rather than context.values (which htmx flattens to FormData strings)
// so numbers/null/booleans keep their real types in the JSON body — see
// htmx-ext-json-enc's encodeParameters, which restores hx-vals/hx-vars values
// verbatim over the stringified FormData ones.
function postJSON(url, body) {
    return new Promise((resolve, reject) => {
        const src = document.createElement('div');
        src.setAttribute('hx-ext', 'json-enc');
        src.setAttribute('hx-vals', JSON.stringify(body || {}));
        src.style.display = 'none';
        document.body.appendChild(src);
        const onAfterRequest = (evt) => {
            src.removeEventListener('htmx:afterRequest', onAfterRequest);
            src.remove();
            const xhr = evt.detail.xhr;
            // status 0 = the request never reached the server (network down, DNS
            // failure, aborted) — reject, matching fetch()'s reject-on-network-error
            // semantics, so existing .catch() blocks (network-error toast/fallback)
            // keep firing. Real HTTP error statuses (4xx/5xx) still resolve with
            // ok:false, exactly like fetch().
            if (xhr.status === 0) { reject(new Error('Network error')); return; }
            resolve({
                ok: xhr.status >= 200 && xhr.status < 300,
                status: xhr.status,
                json: () => JSON.parse(xhr.responseText || 'null'),
                text: xhr.responseText,
            });
        };
        src.addEventListener('htmx:afterRequest', onAfterRequest);
        htmx.ajax('POST', url, { source: src, target: src, swap: 'none', indicator: null });
    });
}
window.postJSON = postJSON;

// ── postForm: postJSON's form-urlencoded sibling ────────────────────
// Same fire-and-forget htmx.ajax + htmx:afterRequest wiring as postJSON, but
// WITHOUT the json-enc extension: a couple of endpoints (e.g. the timezone
// auto-detect below) take a FastAPI Form(...) parameter, not a JSON body —
// forcing json-enc there would send application/json and the server would
// never see the field (Form() only parses url-encoded/multipart). htmx.ajax's
// own default encoding for non-GET requests is already
// application/x-www-form-urlencoded, so this only needs to skip json-enc.
function postForm(url, values) {
    return new Promise((resolve, reject) => {
        const src = document.createElement('div');
        src.style.display = 'none';
        document.body.appendChild(src);
        const onAfterRequest = (evt) => {
            src.removeEventListener('htmx:afterRequest', onAfterRequest);
            src.remove();
            const xhr = evt.detail.xhr;
            if (xhr.status === 0) { reject(new Error('Network error')); return; }
            resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, text: xhr.responseText });
        };
        src.addEventListener('htmx:afterRequest', onAfterRequest);
        htmx.ajax('POST', url, { source: src, target: src, swap: 'none', indicator: null, values: values || {} });
    });
}
window.postForm = postForm;
export { csrfToken, showToast, pushCappedLog };
