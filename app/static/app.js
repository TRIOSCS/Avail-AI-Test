/* AVAIL v1.2.0 — CRM, offers, quotes, target pricing */

// ── Bootstrap: read server-rendered config from JSON block ────────────

const TEST_PATTERNS = [/QA\s+VALIDATION\s+TEST/i, /DELETE\s+ME/i, /\(clone\).*\(clone\)/i];
function validateRfqName(name) {
    for (const p of TEST_PATTERNS) {
        if (p.test(name)) {
            return 'This name matches a test-data pattern and cannot be saved in production.';
        }
    }
    return null;
}
window.validateRfqName = validateRfqName;

(function() {
    var el = document.getElementById('app-config');
    if (el) {
        try {
            var cfg = JSON.parse(el.textContent);
            window.__userName = cfg.userName || '';
            window.__userEmail = cfg.userEmail || '';
            window.__isAdmin = !!cfg.isAdmin;
            window.__isManager = !!cfg.isManager;
            if (cfg.userRole) window.userRole = cfg.userRole;
        } catch(e) { console.warn('Failed to parse app-config', e); }
    }
})();

// ── Error Buffer — captures last 20 console errors for bug reports ──────
(function() {
    var buf = [];
    window.__errorBuffer = buf;
    var MAX = 20;
    function push(entry) {
        buf.push(entry);
        if (buf.length > MAX) buf.shift();
    }
    window.onerror = function(msg, src, line, col) {
        push({msg: String(msg), src: src, line: line, col: col, ts: Date.now()});
    };
    var origWarn = console.warn, origErr = console.error;
    console.warn = function() {
        push({msg: '[warn] ' + Array.prototype.join.call(arguments, ' '), ts: Date.now()});
        origWarn.apply(console, arguments);
    };
    console.error = function() {
        push({msg: '[error] ' + Array.prototype.join.call(arguments, ' '), ts: Date.now()});
        origErr.apply(console, arguments);
    };
})();

// AI features enabled for all authenticated users

// ── Safe localStorage wrappers (private browsing / quota / disabled) ──
function safeGet(key, fallback) { try { return localStorage.getItem(key); } catch(e) { return fallback || null; } }
function safeSet(key, val) { try { localStorage.setItem(key, val); } catch(e) {} }
function safeRemove(key) { try { localStorage.removeItem(key); } catch(e) {} }
window.safeGet = safeGet;
window.safeSet = safeSet;
window.safeRemove = safeRemove;

// ── Mobile detection (cheap matchMedia flag for JS branches) ──────────
(function() {
    var mql = window.matchMedia('(max-width:768px) and (hover:none), (max-width:768px) and (pointer:coarse)');
    window.__isMobile = mql.matches;
    var handler = function(e) { window.__isMobile = e.matches; };
    if (mql.addEventListener) mql.addEventListener('change', handler);
    else if (mql.addListener) mql.addListener(handler);
})();

// ── DOM wait utility ─────────────────────────────────────────────────
// Waits for an element to appear in the DOM (replaces brittle setTimeout chains)
function waitForElement(selector, timeoutMs) {
    if (timeoutMs === undefined) timeoutMs = 2000;
    return new Promise(function(resolve) {
        var el = document.querySelector(selector);
        if (el) { resolve(el); return; }
        var observer = new MutationObserver(function() {
            var el = document.querySelector(selector);
            if (el) { observer.disconnect(); resolve(el); }
        });
        observer.observe(document.body, { childList: true, subtree: true });
        setTimeout(function() { observer.disconnect(); resolve(null); }, timeoutMs);
    });
}

// ── Responsive table utility (Phase 7) ────────────────────────────────
// On mobile: converts rows to .m-card with .m-kv key-value pairs
// On desktop: returns standard HTML table
// columns: [{key, label, format?}], rows: [obj], opts: {onclick?}
function renderResponsiveTable(columns, rows, opts) {
    opts = opts || {};
    if (window.__isMobile) {
        if (!rows.length) return '<p class="m-empty">' + (opts.emptyText || 'No data') + '</p>';
        return rows.map(function(row) {
            var kvHtml = columns.map(function(col) {
                var val = col.format ? col.format(row[col.key], row) : (row[col.key] != null ? row[col.key] : '—');
                return '<span class="m-kv-key">' + col.label + '</span><span class="m-kv-val">' + val + '</span>';
            }).join('');
            var clickAttr = opts.onclick ? ' onclick="' + opts.onclick.replace('{id}', row.id || '') + '"' : '';
            return '<div class="m-card"' + clickAttr + '><div class="m-kv">' + kvHtml + '</div></div>';
        }).join('');
    }
    // Desktop: standard table
    var thead = '<thead><tr>' + columns.map(function(c) { return '<th' + (c.minWidth ? ' style="min-width:' + c.minWidth + ';white-space:nowrap"' : '') + '>' + c.label + '</th>'; }).join('') + '</tr></thead>';
    var tbody = '<tbody>' + rows.map(function(row) {
        var clickAttr = opts.onclick ? ' onclick="' + opts.onclick.replace('{id}', row.id || '') + '" style="cursor:pointer"' : '';
        return '<tr' + clickAttr + '>' + columns.map(function(col) {
            var val = col.format ? col.format(row[col.key], row) : (row[col.key] != null ? row[col.key] : '—');
            return '<td' + (col.minWidth ? ' style="min-width:' + col.minWidth + ';white-space:nowrap"' : '') + '>' + val + '</td>';
        }).join('') + '</tr>';
    }).join('') + '</tbody>';
    return '<table class="tbl">' + thead + tbody + '</table>';
}
window.renderResponsiveTable = renderResponsiveTable;

// ── Early stubs (available before full init for onclick handlers) ──────

function validatePartRowInputs(qty, targetPrice) {
    if (!Number.isInteger(Number(qty)) || Number(qty) <= 0) {
        showToast('Quantity must be a positive whole number.', 'error');
        return false;
    }
    if (isNaN(Number(targetPrice)) || Number(targetPrice) <= 0) {
        showToast('Target price must be a positive value.', 'error');
        return false;
    }
    return true;
}
window.validatePartRowInputs = validatePartRowInputs;

function toggleMobileSidebar() {
    var sb = document.getElementById('sidebar');
    var ov = document.getElementById('sidebarOverlay');
    if (sb) sb.classList.toggle('mobile-open');
    if (ov) ov.classList.toggle('open');
}

// ── Mobile bottom-nav navigation + back stack ────────────────────────
var _mobileNavStack = [];

function mobileTabNav(page, btn) {
    var pop = document.getElementById('mobileMorePopover');
    if (pop) pop.classList.remove('open');
    document.querySelectorAll('.m-bottomnav-tab').forEach(function(t) {
        t.classList.toggle('active', t === btn);
    });
    _mobileNavStack = [page];
    if (page === 'offers') {
        showView('view-offers');
        if (typeof loadOfferFeed === 'function') loadOfferFeed();
        return;
    }
    if (page === 'alerts') {
        showView('view-alerts');
        if (typeof loadAlertsFeed === 'function') loadAlertsFeed();
        return;
    }
    var navMap = { reqs:'reqs', customers:'customers' };
    if (navMap[page]) {
        var navBtn = document.getElementById({reqs:'navReqs', customers:'navCustomers'}[page]);
        sidebarNav(navMap[page], navBtn);
    }
}

function mobileMoreNav(page) {
    var pop = document.getElementById('mobileMorePopover');
    if (pop) pop.classList.remove('open');
    // Reset tab highlight — "More" stays highlighted for sub-pages
    document.querySelectorAll('.m-bottomnav-tab').forEach(function(t) {
        t.classList.toggle('active', t.dataset.nav === 'more');
    });
    _mobileNavStack = [page];
    var navBtnMap = {vendors:'navVendors',materials:'navMaterials',buyplans:'navBuyPlans',settings:'navSettings'};
    var navBtn = document.getElementById(navBtnMap[page] || '');
    sidebarNav(page, navBtn);
}

function toggleMobileMore(btn) {
    var pop = document.getElementById('mobileMorePopover');
    if (pop) pop.classList.toggle('open');
}

function mobileBack() {
    if (_mobileNavStack.length > 1) {
        _mobileNavStack.pop();
        var prev = _mobileNavStack[_mobileNavStack.length - 1];
        mobileTabNav(prev, document.querySelector('.m-bottomnav-tab[data-nav="' + prev + '"]'));
    }
}

function _toggleMobileSearch() {
    var bar = document.getElementById('mobileSearchBar');
    if (!bar) return;
    bar.classList.toggle('hidden');
    if (!bar.classList.contains('hidden')) {
        var input = bar.querySelector('input');
        if (input) setTimeout(function() { input.focus(); }, 100);
    }
}

function _showMobileUserMenu() {
    confirmAction('Sign Out', 'Sign out of AvailAI?', function() {
        window.location.href = '/auth/logout';
    });
}

// Close More popover on any outside click
document.addEventListener('click', function(e) {
    var pop = document.getElementById('mobileMorePopover');
    if (pop && pop.classList.contains('open')) {
        var moreBtn = document.querySelector('.m-bottomnav-tab[data-nav="more"]');
        if (!pop.contains(e.target) && moreBtn !== e.target && !moreBtn.contains(e.target)) {
            pop.classList.remove('open');
        }
    }
});

// Sync active pill state between desktop #mainPills and mobile #mobilePills
function _syncMobilePills(clicked) {
    var view = clicked && clicked.dataset ? clicked.dataset.view : null;
    if (!view) return;
    ['mainPills', 'mobilePills'].forEach(function(id) {
        var cont = document.getElementById(id);
        if (!cont) return;
        cont.querySelectorAll('.fp').forEach(function(b) {
            b.classList.toggle('on', b.dataset.view === view);
        });
    });
}

// Close filter panel on outside click
document.addEventListener('click', function(e) {
    document.querySelectorAll('.filter-panel.open').forEach(function(p) {
        if (!p.closest('.filter-wrap').contains(e.target)) p.classList.remove('open');
    });
});

// Sync mobile <-> desktop search
document.addEventListener('DOMContentLoaded', function() {
    // Initialize sourcing search if the sourcing view is active (lazy — only on first visit)
    if (typeof initSourcingSearch === 'function') {
        var sourcingView = document.getElementById('view-sourcing');
        if (sourcingView && !sourcingView.classList.contains('hidden') && !sourcingView.dataset.searchInit) {
            sourcingView.dataset.searchInit = '1';
            initSourcingSearch();
        }
    }
    var ms = document.getElementById('mobileMainSearch');
    var ds = document.getElementById('mainSearch');
    if (ms && ds) {
        ms.addEventListener('input', function(e) { e.stopPropagation(); ds.value = ms.value; });
        ms.addEventListener('keydown', function(e) { e.stopPropagation(); });
        ms.addEventListener('keyup', function(e) { e.stopPropagation(); });
        ds.addEventListener('input', function(e) { e.stopPropagation(); ms.value = ds.value; });
        ds.addEventListener('keydown', function(e) { e.stopPropagation(); });
        ds.addEventListener('keyup', function(e) { e.stopPropagation(); });


        // Sync on viewport change (device rotation / resize across breakpoint)
        var mql = window.matchMedia('(max-width:768px)');
        var syncSearch = function() { var active = mql.matches ? ds.value : ms.value; ds.value = active; ms.value = active; };
        if (mql.addEventListener) mql.addEventListener('change', syncSearch);
        else if (mql.addListener) mql.addListener(syncSearch);
        // Also sync on window resize to catch edge cases (e.g. split-screen changes)
        window.addEventListener('resize', function() {
            if (window.innerWidth > 768) {
                ds.value = ms.value || ds.value;
            } else {
                ms.value = ds.value || ms.value;
            }
        });
    }
    // Initialize mobile user avatar
    var mobileAvatar = document.getElementById('mobileUserAvatar');
    if (mobileAvatar && window.__userName) {
        mobileAvatar.textContent = window.__userName.split(' ').map(function(w) { return w[0]; }).join('').substring(0, 2).toUpperCase();
    }
});

export let currentReqId = null;
export function setCurrentReqId(id) { currentReqId = id; }
let currentReqName = '';
let formIsDirty = false;
let searchResults = {};
let _sightingIndex = {};  // sightingId → {reqId, sighting} for O(1) lookups
let searchResultsCache = {};  // keyed by reqId
let selectedSightings = new Set();
const ACTIVE_RFQ_STATUSES = ['pending', 'active'];
let rfqVendorData = [];

function validatePartRows(parts) {
    for (const part of parts) {
        const qty = Number(part.qty);
        if (!Number.isInteger(qty) || qty < 1 || qty > 1000000) {
            showToast(`Invalid quantity "${part.qty}" for part ${part.part_number || part.mpn || ''}. Must be a whole number between 1 and 1,000,000.`, 'error');
            return false;
        }
    }
    return true;
}
let activeTabCache = {};  // reqId → tab name
let _vendorListData = [];   // cached vendor list for client-side filtering
let _vendorTierFilter = 'all';  // all|proven|developing|caution|new
let expandedGroups = new Set();  // reqIds that are expanded (default: all expanded on load)
let _ddReqCache = {};  // drill-down requirements cache: rfqId → [requirements]
let _addRowActive = {};  // rfqId → true when inline add row is visible
let _ddActFilter = {};   // rfqId → 'all'|'email'|'phone'|'notes' for activity filter
let _ddSightingsCache = {};      // reqId -> sightings API response
let _ddSelectedSightings = {};   // reqId -> Set of sighting IDs
var _ddTierState = {};           // tier expand/collapse state: `${reqId}-${rId}-${tier}` → bool
let _partExpandState = {};       // `${reqId}-${requirementId}` → true if part detail is expanded
let _partActiveTab = {};         // `${reqId}-${requirementId}` → 'offers'|'notes'|'tasks'
let _partDetailCache = {};       // `${reqId}-${requirementId}-${tab}` → cached data
const CONDITION_OPTIONS = ['New', 'ETN', 'Factory Refurbished', 'Pulls'];

function _rebuildSightingIndex() {
    _sightingIndex = {};
    for (const reqId of Object.keys(searchResults)) {
        for (const s of (searchResults[reqId].sightings || [])) {
            if (s.id != null) _sightingIndex[s.id] = { reqId, sighting: s };
        }
    }
}

// ── Shared Helpers ──────────────────────────────────────────────────────

// Canonical 2-decimal currency formatter
function formatCurrency(value) {
    if (value === null || value === undefined || value === '') return '—';
    return '$' + parseFloat(value).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}
window.formatCurrency = formatCurrency;

const _apiFetchInflight = {}; // URL+method → Promise (dedup guard)
const _apiFetchCooldown = {}; // URL+method → timestamp (POST/PUT/DELETE double-click guard)
export async function apiFetch(url, opts = {}) {
    // CSRF: include double-submit cookie value as header
    const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1];
    if (csrf) opts.headers = {...(opts.headers || {}), 'x-csrftoken': csrf};
    if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
        opts.headers = {'Content-Type': 'application/json', ...(opts.headers || {})};
        opts.body = JSON.stringify(opts.body);
    }
    const method = (opts.method || 'GET').toUpperCase();
    // Request deduplication for GET requests — return in-flight promise if identical
    const dedupeKey = method === 'GET' ? method + ':' + url : null;
    if (dedupeKey && _apiFetchInflight[dedupeKey]) return _apiFetchInflight[dedupeKey];
    // Double-click protection for mutating requests (1000ms cooldown per URL+method)
    if (method !== 'GET') {
        const cooldownKey = method + ':' + url;
        const last = _apiFetchCooldown[cooldownKey];
        if (last && Date.now() - last < 1000) return Promise.reject(new Error('Duplicate request blocked'));
        _apiFetchCooldown[cooldownKey] = Date.now();
    }
    const doFetch = async () => {
        if (!navigator.onLine) {
            throw Object.assign(new Error('You appear to be offline'), {status: 0});
        }
        const maxRetries = method === 'GET' ? 2 : 0;
        let lastErr;
        for (let attempt = 0; attempt <= maxRetries; attempt++) {
            if (attempt > 0) {
                await new Promise(r => setTimeout(r, Math.pow(2, attempt - 1) * 1000));
            }
            const res = await fetch(url, opts);
            if (!res.ok) {
                const rawMsg = await res.text().catch(() => res.statusText);
                lastErr = Object.assign(new Error(rawMsg.length > 200 ? rawMsg.slice(0, 200) : rawMsg), {status: res.status});
                // Session expired — redirect to login
                if (res.status === 401) {
                    showToast('Session expired — redirecting to login…', 'error');
                    setTimeout(() => { window.location.href = '/auth/login'; }, 1500);
                    throw lastErr;
                }
                if (res.status === 409) throw lastErr;
                // Rate limit: extract retry-after and surface to caller
                if (res.status === 429) {
                    const retryAfter = parseInt(res.headers.get('retry-after') || '60', 10);
                    lastErr.retryAfter = retryAfter;
                    lastErr.isRateLimit = true;
                    throw lastErr;
                }
                if (res.status >= 500 && attempt < maxRetries) continue;
                throw lastErr;
            }
            const ct = res.headers.get('content-type') || '';
            return ct.includes('json') ? res.json() : res.text();
        }
        throw lastErr;
    };
    if (dedupeKey) {
        const p = doFetch().finally(() => { delete _apiFetchInflight[dedupeKey]; });
        _apiFetchInflight[dedupeKey] = p;
        return p;
    }
    return doFetch();
}

/** Extract a user-friendly error message from caught exceptions. */
export function friendlyError(e, fallback) {
    if (!e) return fallback || 'Something went wrong';
    const msg = e.message || '';
    // Try to parse JSON error responses from the API
    try {
        const parsed = JSON.parse(msg);
        if (parsed.error) return parsed.error;
        if (parsed.detail) return typeof parsed.detail === 'string' ? parsed.detail : fallback || 'Something went wrong';
    } catch (_) { /* not JSON */ }
    // Filter out raw technical messages
    if (msg.includes('<!DOCTYPE') || msg.includes('<html') || msg.length > 200) return fallback || 'Something went wrong';
    if (msg === 'Failed to fetch') return 'Could not reach the server — check your connection';
    if (msg === 'Duplicate request blocked') return 'Please wait a moment before trying again';
    if (msg && msg.length > 0 && msg.length <= 200) return msg;
    return fallback || 'Something went wrong';
}

export function debounce(fn, ms = 300) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// Debounced input handlers — client-side filters at 150ms, API calls at 300ms
const debouncedRenderReqTable = debounce(() => renderRequirementsTable(), 150);
const debouncedRenderSources = debounce(() => renderSources(), 150);
const debouncedRenderActivity = debounce(() => renderActivityCards(), 150);
const debouncedLoadCustomers = debounce(() => window.loadCustomers(), 300);
const debouncedFilterVendors = debounce(() => filterVendorList(), 150);
const debouncedLoadMaterials = debounce(() => loadMaterialList(), 300);
const debouncedFilterSites = debounce((v) => window.filterSiteTypeahead(v), 150);

// ── Utilities ───────────────────────────────────────────────────────────
export function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
export function escAttr(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
export function sanitizeRichHtml(rawHtml) {
    if (!rawHtml) return '';
    const input = String(rawHtml);
    if (typeof window === 'undefined' || typeof DOMParser === 'undefined') {
        return esc(input).replace(/\n/g, '<br>');
    }
    const parser = new DOMParser();
    const doc = parser.parseFromString(`<div>${input}</div>`, 'text/html');
    const root = doc.body.firstElementChild || doc.body;
    const allowedTags = new Set([
        'A', 'B', 'BLOCKQUOTE', 'BR', 'CODE', 'DIV', 'EM', 'I', 'LI', 'OL',
        'P', 'PRE', 'SPAN', 'STRONG', 'TABLE', 'TBODY', 'TD', 'TH', 'THEAD', 'TR', 'UL',
    ]);
    const allowedAttrs = new Set(['href', 'title', 'colspan', 'rowspan', 'align']);

    for (const node of Array.from(root.querySelectorAll('*'))) {
        if (!allowedTags.has(node.tagName)) {
            node.replaceWith(doc.createTextNode(node.textContent || ''));
            continue;
        }
        for (const attr of Array.from(node.attributes)) {
            const name = attr.name.toLowerCase();
            if (name.startsWith('on') || name === 'style') {
                node.removeAttribute(attr.name);
                continue;
            }
            if (name === 'href') {
                const href = (attr.value || '').trim();
                if (!/^(https?:|mailto:|tel:)/i.test(href)) {
                    node.removeAttribute(attr.name);
                } else {
                    node.setAttribute('rel', 'noopener noreferrer');
                    node.setAttribute('target', '_blank');
                }
                continue;
            }
            if (!allowedAttrs.has(name)) {
                node.removeAttribute(attr.name);
            }
        }
    }
    return root.innerHTML;
}
window.skeletonRows = function(n) {
    let h = '';
    for (let i = 0; i < n; i++) h += '<div class="skeleton-row"><div class="skeleton-cell skeleton-cell-lg"></div><div class="skeleton-cell skeleton-cell-md"></div><div class="skeleton-cell skeleton-cell-sm"></div><div class="skeleton-cell skeleton-cell-md"></div></div>';
    return h;
};
export function logCatchError(ctx, err) { if (err) console.warn('[' + ctx + ']', err); }

/** Unified loading spinner HTML */
export function stateLoading(msg = 'Loading\u2026') {
    return `<div class="spinner-row"><div class="spinner"></div>${esc(msg)}</div>`;
}
/** Unified empty state HTML */
export function stateEmpty(msg, hint) {
    return `<div class="state-empty"><div class="state-empty-icon">\u{1F4CB}</div>${esc(msg)}${hint ? `<div class="state-empty-hint">${hint}</div>` : ''}</div>`;
}
/** Unified error state HTML */
export function stateError(msg, hint) {
    return `<div class="state-error">${esc(msg)}${hint ? `<div class="state-error-hint">${hint}</div>` : ''}</div>`;
}

// ── Phone Formatting & Click-to-Call ─────────────────────────────────

/**
 * Normalize phone to E.164. Returns null if unparseable.
 * Mirrors Python: app/utils/phone_utils.py:format_phone_e164
 */
function toE164(raw) {
    if (!raw) return null;
    var cleaned = raw.trim().replace(/\s*(ext|x|#)\s*\.?\s*\d*$/i, '');
    if (/[a-zA-Z]/.test(cleaned)) return null;
    var hasPlus = cleaned.charAt(0) === '+';
    var digits = cleaned.replace(/\D/g, '');
    if (!digits || digits.length < 7) return null;
    if (digits.length === 10) return '+1' + digits;
    if (digits.length === 11 && digits.charAt(0) === '1') return '+' + digits;
    if (hasPlus && digits.length >= 7) return '+' + digits;
    if (digits.length >= 12) return '+' + digits;
    return null;
}
window.toE164 = toE164;

/**
 * Format phone for display. US: (415) 555-1234, else raw.
 * Mirrors Python: app/utils/phone_utils.py:format_phone_display
 */
function formatPhoneDisplay(raw) {
    if (!raw) return '';
    var e = toE164(raw);
    if (!e) return raw.trim();
    var d = e.replace(/^\+/, '');
    if (d.length === 11 && d.charAt(0) === '1') {
        var local = d.substring(1);
        return '(' + local.substring(0,3) + ') ' + local.substring(3,6) + '-' + local.substring(6);
    }
    return e;
}
window.formatPhoneDisplay = formatPhoneDisplay;

/**
 * Build a click-to-call <a> tag with background activity logging.
 * Returns plain text for unparseable numbers.
 * @param {string} raw - raw phone string
 * @param {object} context - {vendor_card_id, company_id, customer_site_id, requirement_id}
 * @returns {string} HTML string
 */
function phoneLink(raw, context) {
    var e = toE164(raw);
    if (!e) return esc(raw || '');
    var display = formatPhoneDisplay(raw);
    var ctx = JSON.stringify(context || {}).replace(/"/g, '&quot;');
    return '<a href="tel:' + escAttr(e) + '" class="phone-link" onclick="logCallInitiated(this)" data-ctx="' + ctx + '" data-phone="' + escAttr(raw) + '">' + esc(display) + '</a>';
}
window.phoneLink = phoneLink;

/**
 * Fire-and-forget POST to /api/activity/call-initiated.
 * Does NOT preventDefault — the tel: link fires normally.
 */
function logCallInitiated(el) {
    var raw = el.getAttribute('data-phone') || '';
    var ctx = {};
    try { ctx = JSON.parse(el.getAttribute('data-ctx') || '{}'); } catch(e) {}
    var body = { phone_number: raw };
    if (ctx.vendor_card_id) body.vendor_card_id = ctx.vendor_card_id;
    if (ctx.company_id) body.company_id = ctx.company_id;
    if (ctx.customer_site_id) body.customer_site_id = ctx.customer_site_id;
    if (ctx.requirement_id) body.requirement_id = ctx.requirement_id;
    if (ctx.origin) body.origin = ctx.origin;
    fetch('/api/activity/call-initiated', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
        keepalive: true
    }).catch(function(e) { console.warn('logCallInitiated:', e); });
}
window.logCallInitiated = logCallInitiated;

var _modalStack = [];
export function openModal(id, focusId) {
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.add('open');
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    // Auto-set aria-labelledby from h2 inside modal
    var h2 = el.querySelector('.modal > h2');
    if (h2) {
        if (!h2.id) h2.id = id + '-title';
        el.setAttribute('aria-labelledby', h2.id);
    }
    _modalStack.push({id: id, returnFocus: document.activeElement});
    if (focusId) setTimeout(function() { var f = document.getElementById(focusId); if (f) f.focus(); }, 100);
    else setTimeout(function() {
        var first = el.querySelector('input:not([type=hidden]),select,textarea,button:not(.close-btn)');
        if (first) first.focus();
    }, 100);
}

export async function guardBtn(btn, loadingText, action) {
    if (!btn || btn.disabled) return;
    var orig = btn.textContent;
    btn.disabled = true;
    if (loadingText) btn.textContent = loadingText;
    var lockedEls = [];
    var form = btn.closest('form');
    if (form) {
        form.querySelectorAll('input,select,textarea,button').forEach(function(el) {
            if (!el.disabled) { el.disabled = true; lockedEls.push(el); }
        });
    }
    try { return await action(); }
    finally {
        btn.disabled = false;
        btn.textContent = orig;
        lockedEls.forEach(function(el) { el.disabled = false; });
    }
}
// Lightweight self-guard for async onclick handlers: call _selfGuard(ev) at top,
// returns false if button already busy. Automatically re-enables when function returns.
function _selfGuard(ev) {
    var btn = ev && (ev.target?.closest('button') || ev.target?.closest('a'));
    if (!btn) return {ok:true};
    if (btn.dataset.busy) return {ok:false};
    btn.dataset.busy = '1'; btn.style.opacity = '0.5'; btn.style.pointerEvents = 'none';
    return {ok:true, done:()=>{ delete btn.dataset.busy; btn.style.opacity=''; btn.style.pointerEvents=''; }};
}

function _timeAgo(iso) {
    if (!iso) return '';
    const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}
export function fmtDate(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'numeric', day: 'numeric' });
}
export function fmtDateTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
function stars(avg, count) {
    if (avg === null || avg === undefined) return '<span class="stars-none">☆</span>';
    const full = Math.floor(avg);
    const half = avg - full >= 0.5 ? 1 : 0;
    let s = '<span class="stars">';
    for (let i = 0; i < full; i++) s += '★';
    if (half) s += '½';
    s += `</span><span class="stars-num">${avg}</span>`;
    if (count > 0) s += `<span class="stars-count">(${count})</span>`;
    return s;
}

// ── v2 Visual Helpers ───────────────────────────────────────────────────
export function engRing(score, size = 44) {
    const r = (size / 2) - 4;
    const circ = 2 * Math.PI * r;
    const offset = circ - (Math.max(0, Math.min(100, score)) / 100 * circ);
    const tier = score >= 70 ? 'green' : score >= 40 ? 'amber' : 'red';
    return `<svg class="eng-ring eng-ring-${tier}" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
        <circle class="eng-ring-bg" cx="${size/2}" cy="${size/2}" r="${r}"/>
        <circle class="eng-ring-fg" cx="${size/2}" cy="${size/2}" r="${r}"
            stroke-dasharray="${circ.toFixed(1)}" stroke-dashoffset="${offset.toFixed(1)}"
            transform="rotate(-90 ${size/2} ${size/2})"/>
        <text class="eng-ring-text" x="${size/2}" y="${size/2}">${Math.round(score)}</text>
    </svg>`;
}
export function healthDot(color, title = '') {
    return `<span class="health-dot health-dot-${color}" ${title ? `title="${title}"` : ''}></span>`;
}
export function statCard(label, value, opts = {}) {
    const trend = opts.trend
        ? `<span class="stat-card-trend ${opts.trendDir || ''}">${opts.trend}</span>`
        : '';
    return `<div class="stat-card">
        <span class="stat-card-value">${opts.prefix || ''}${value}</span>
        <span class="stat-card-label">${label}</span>
        ${trend}
    </div>`;
}
export function daysSince(dateStr) {
    if (!dateStr) return 999;
    const d = new Date(dateStr);
    if (isNaN(d)) return 999;
    return Math.floor((Date.now() - d.getTime()) / 86400000);
}
export function recencyColor(days, thresholds = [7, 21]) {
    if (days > 900) return 'muted';
    if (days <= thresholds[0]) return 'green';
    if (days <= thresholds[1]) return 'amber';
    return 'red';
}

/* ── v2 Extended Helpers ─────────────────────────────────────────────── */

const AVATAR_COLORS = [
    '#3b6ea8','#7c3aed','#059669','#d97706','#dc2626',
    '#0891b2','#9333ea','#c026d3','#4f46e5','#0d9488'
];
function _avatarColor(name) {
    let h = 0;
    for (let i = 0; i < (name || '').length; i++) h = ((h << 5) - h + name.charCodeAt(i)) | 0;
    return AVATAR_COLORS[Math.abs(h) % AVATAR_COLORS.length];
}
export function ownerAvatar(name, size = 'md') {
    if (!name) return '';
    const initials = name.split(/\s+/).map(w => w[0]).join('').toUpperCase().slice(0, 2);
    return `<div class="owner-avatar owner-avatar-${size}" style="background:${_avatarColor(name)}" title="${esc(name)}">${initials}</div>`;
}

export function factorBar(label, value) {
    const color = value >= 70 ? 'green' : value >= 40 ? 'amber' : 'red';
    return `<div class="factor-bar-inline">
        <span class="factor-bar-label">${esc(label)}</span>
        <div class="factor-bar-track"><div class="factor-bar-fill factor-bar-fill-${color}" style="width:${Math.min(100, Math.max(0, value))}%"></div></div>
        <span class="factor-bar-value">${Math.round(value)}</span>
    </div>`;
}

export function relationshipHealthBar(lastContactedDays, windowDays = 30) {
    const elapsed = Math.min(lastContactedDays, windowDays);
    const remaining = windowDays - elapsed;
    const pct = (elapsed / windowDays) * 100;
    const color = elapsed <= 7 ? 'green' : elapsed <= 21 ? 'amber' : 'red';
    const textColor = elapsed > 21 ? 'color:var(--red)' : elapsed > 7 ? 'color:var(--amber)' : '';
    return `<div class="rel-health">
        <div class="rel-health-header">
            <span class="rel-health-label">Relationship Health</span>
            <span class="rel-health-remaining" style="${textColor}">${remaining <= 0 ? 'Requires contact' : remaining + 'd remaining'}</span>
        </div>
        <div class="rel-health-track"><div class="rel-health-fill" style="width:${pct.toFixed(1)}%;background:var(--${color})"></div></div>
        <div class="rel-health-meta"><span>${elapsed}d since last contact</span><span>${windowDays}d window</span></div>
    </div>`;
}

const CONTACT_STATUS = {
    champion:    { label:'Champion',    color:'#16a34a', bg:'#dcfce7' },
    active:      { label:'Active',      color:'#2563eb', bg:'#dbeafe' },
    quiet:       { label:'Quiet',       color:'#d97706', bg:'#fef3c7' },
    new:         { label:'New',         color:'#64748b', bg:'#e2e8f0' },
    inactive:    { label:'Inactive',    color:'#dc2626', bg:'#fee2e2' },
};
const CONTACT_STATUS_ORDER = ['champion','active','quiet','new','inactive'];

export function contactStatusBar(contacts, statusOverrides = {}) {
    if (!contacts || !contacts.length) return '';
    const counts = {};
    CONTACT_STATUS_ORDER.forEach(s => counts[s] = 0);
    contacts.forEach(c => {
        const st = statusOverrides[c.id] || c.status || 'new';
        counts[st] = (counts[st] || 0) + 1;
    });
    const total = contacts.length;
    const segments = CONTACT_STATUS_ORDER
        .filter(s => counts[s] > 0)
        .map(s => `<div class="cs-bar-seg" style="width:${(counts[s]/total*100).toFixed(1)}%;background:${CONTACT_STATUS[s].color}"></div>`)
        .join('');
    const legend = CONTACT_STATUS_ORDER
        .filter(s => counts[s] > 0)
        .map(s => `<span style="color:${CONTACT_STATUS[s].color}">${counts[s]} ${CONTACT_STATUS[s].label}</span>`)
        .join('');
    return `<div class="cs-bar-wrap">
        <div class="cs-bar-header"><span class="cs-bar-label">Contact Qualification</span><span class="cs-bar-total">${total} total</span></div>
        <div class="cs-bar-track">${segments}</div>
        <div class="cs-bar-legend">${legend}</div>
    </div>`;
}

export function statusPill(status, size = 'sm') {
    const cfg = CONTACT_STATUS[status] || CONTACT_STATUS.new;
    return `<span class="status-pill status-pill-${size} status-pill-${status}">${cfg.label}</span>`;
}

export function activityIcon(type) {
    const map = {
        email_sent: '📤', email_received: '📥', phone: '📞',
        note: '📝', quote: '📄', offer: '🏷️', meeting: '🤝',
    };
    return `<span class="act-icon">${map[type] || '📋'}</span>`;
}

export function getRelativeTime(dateStr) {
    if (!dateStr) return '—';
    const d = new Date(dateStr);
    if (isNaN(d)) return '—';
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return 'just now';
    const min = Math.floor(sec / 60);
    if (min < 60) return min + 'm ago';
    const hrs = Math.floor(min / 60);
    if (hrs < 24) return hrs + 'h ago';
    const days = Math.floor(hrs / 24);
    if (days < 30) return days + 'd ago';
    const months = Math.floor(days / 30);
    return months + 'mo ago';
}

export function filterChip(label, value, isActive, count) {
    const countHtml = count != null ? ` <span class="chip-count">(${count})</span>` : '';
    return `<span class="chip${isActive ? ' on' : ''}" data-value="${esc(String(value))}">${esc(label)}${countHtml}</span>`;
}

// ── Name Autocomplete ───────────────────────────────────────────────────
export function initNameAutocomplete(inputId, listId, hiddenId, opts = {}) {
    const input = document.getElementById(inputId);
    const list  = document.getElementById(listId);
    if (!input || !list) return;
    if (input.dataset.autocompleteInit) return;
    input.dataset.autocompleteInit = '1';
    const minLen = opts.minLen || 2;
    const filterType = opts.types || 'all';
    const websiteId = opts.websiteId || null;
    let _matched = false;

    function showWebsite(show) {
        if (!websiteId) return;
        const el = document.getElementById(websiteId);
        const row = el?.closest('.ac-website-row') || el;
        if (row) { if (show) { row.classList.remove('u-hidden'); row.style.display = ''; } else { row.classList.add('u-hidden'); row.style.display = ''; } }
    }

    const doSearch = debounce(async function(query) {
        if (query.length < minLen) { list.classList.remove('show'); return; }
        try {
            const results = await apiFetch('/api/autocomplete/names?q=' + encodeURIComponent(query) + '&limit=8');
            const filtered = filterType === 'all' ? results : results.filter(r => r.type === filterType);
            if (!filtered.length) {
                list.innerHTML = '<div class="site-typeahead-item" style="color:var(--muted)">New — enter website for enrichment</div>';
                _matched = false;
                showWebsite(true);
            } else {
                list.innerHTML = filtered.map(r =>
                    '<div class="site-typeahead-item" data-id="' + r.id + '" data-type="' + r.type + '" data-name="' + escAttr(r.name) + '">'
                    + esc(r.name)
                    + ' <span class="ac-badge ac-' + r.type + '">' + r.type + '</span>'
                    + '</div>'
                ).join('');
                _matched = false;
                showWebsite(true);
            }
            list.classList.add('show');
        } catch (e) { logCatchError('autocomplete', e); list.classList.remove('show'); }
    }, 250);

    input.addEventListener('input', function() {
        _matched = false;
        if (hiddenId) { const h = document.getElementById(hiddenId); if (h) h.value = ''; }
        showWebsite(true);
        doSearch(input.value.trim());
    });
    input.addEventListener('focus', function() {
        if (input.value.trim().length >= minLen) doSearch(input.value.trim());
    });
    list.addEventListener('click', function(e) {
        const item = e.target.closest('.site-typeahead-item');
        if (!item || !item.dataset.name) return;
        input.value = item.dataset.name;
        _matched = true;
        if (hiddenId) { const h = document.getElementById(hiddenId); if (h) h.value = item.dataset.type + ':' + item.dataset.id; }
        list.classList.remove('show');
        showWebsite(false);
    });
    document.addEventListener('click', function(e) {
        if (!e.target.closest('#' + inputId) && !e.target.closest('#' + listId)) list.classList.remove('show');
    });
    input.addEventListener('keydown', function(e) {
        if (!list.classList.contains('show')) return;
        const items = list.querySelectorAll('.site-typeahead-item[data-name]');
        if (!items.length) return;
        let idx = Array.from(items).findIndex(el => el.classList.contains('active'));
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            items.forEach(el => el.classList.remove('active'));
            idx = (idx + 1) % items.length;
            items[idx].classList.add('active');
            items[idx].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            items.forEach(el => el.classList.remove('active'));
            idx = idx <= 0 ? items.length - 1 : idx - 1;
            items[idx].classList.add('active');
            items[idx].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Enter' && idx >= 0) {
            e.preventDefault();
            items[idx].click();
        } else if (e.key === 'Escape') {
            list.classList.remove('show');
        }
    });
}

// ── Init ────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    // Wire scroll-end detection on static table wraps so CSS fade-out hint disappears
    document.querySelectorAll('.crm-table-wrap').forEach(el => {
        el.addEventListener('scroll', () => {
            const atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 2;
            el.classList.toggle('scrolled-end', atEnd);
        });
    });
    initNameAutocomplete('stockVendorName', 'stockVendorNameList', null, { types: 'vendor', websiteId: 'stockVendorWebsite' });
    // Enforce minimum date of today on the BID DUE field
    const _bidDueInput = document.getElementById('nrDeadline');
    if (_bidDueInput) {
        const _today = new Date().toISOString().split('T')[0];
        _bidDueInput.setAttribute('min', _today);
        _bidDueInput.addEventListener('change', function() {
            if (_bidDueInput.value && _bidDueInput.value < _today) {
                _bidDueInput.setCustomValidity('BID DUE date cannot be in the past.');
                _bidDueInput.reportValidity();
            } else {
                _bidDueInput.setCustomValidity('');
            }
        });
    }
    // Route based on URL hash (supports bookmarks + page refresh)
    const initHash = location.hash.replace('#', '');
    var initDrillId = null;
    var initBaseHash = initHash;
    if (initHash.startsWith('rfqs/')) {
        initDrillId = parseInt(initHash.split('/')[1]);
        initBaseHash = 'rfqs';
    }
    const initView = _hashToView[initBaseHash];
    safeSet('_lastActivityTs', String(Date.now()));
    const effectiveView = initView;
    if (effectiveView && effectiveView !== 'view-list') {
        _navFromPopstate = true;
        try {
        const initRoutes = {
            'view-vendors': () => showVendors(),
            'view-materials': () => showMaterials(),
            'view-customers': () => window.showCustomers(),
            'view-buyplans': () => window.showBuyPlans(),
            'view-proactive': () => window.showProactiveOffers(),
            'view-settings': () => {
                var settingsTab = (initBaseHash === 'apihealth') ? initBaseHash : undefined;
                window.showSettings(settingsTab);
            },
            'view-suggested': () => window.showSuggested(),
        };
        if (initRoutes[effectiveView]) initRoutes[effectiveView]();
        const sidebarMap = {'view-vendors':'navVendors','view-materials':'navMaterials','view-customers':'navCustomers','view-buyplans':'navBuyPlans','view-proactive':'navProactive','view-settings':'navSettings','view-suggested':'navProspecting'};
        const navBtn = document.getElementById(sidebarMap[effectiveView]);
        if (navBtn) navHighlight(navBtn);
        } catch(e) { console.error('init route error:', e); }
        finally { _navFromPopstate = false; }
    }
    // Set initial section gradient color from active nav button
    var _initActiveBtn = document.querySelector('.sb-nav-btn.active');
    if (_initActiveBtn) {
        var _initSection = _initActiveBtn.closest('[data-section]');
        var _initGradient = document.querySelector('.sb-top-gradient');
        if (_initSection && _initGradient) _initGradient.dataset.section = _initSection.dataset.section;
    }
    // Sync pill state with saved view preference
    const _savedView = _currentMainView;
    ['mainPills', 'mobilePills'].forEach(id => {
        const cont = document.getElementById(id);
        if (cont) cont.querySelectorAll('.fp').forEach(b => b.classList.toggle('on', b.dataset.view === _savedView));
    });
    await loadRequisitions();
    // Restore drill-down from URL hash (e.g. #rfqs/123) or localStorage fallback
    // Only restore when actually on the requisition list view (not dashboard/materials/etc.)
    if ((!initView && !effectiveView) || initView === 'view-list' || effectiveView === 'view-list') {
        var restoreId = initDrillId;
        if (!restoreId) {
            const lastId = parseInt(safeGet('lastReqId', '0'));
            if (lastId) restoreId = lastId;
        }
        if (restoreId) {
            const found = _reqListData.find(r => r.id === restoreId);
            if (found) setTimeout(() => toggleDrillDown(restoreId), 300);
        }
    }
    checkM365Status();
    const dz = document.getElementById('dropZone');
    if (dz) {
        dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
        dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
        dz.addEventListener('drop', e => {
            e.preventDefault(); dz.classList.remove('drag');
            if (e.dataTransfer.files.length) {
                const fi = document.getElementById('fileInput');
                if (fi) fi.files = e.dataTransfer.files;
                showFileReady('fileInput','uploadReady','uploadFileName');
            }
        });
    }
});

// ── M365 Connection Status ───────────────────────────────────────────────
// Global user state
window.userRole = 'buyer';  // Default until auth check
window.userName = '';
window.userEmail = '';

async function checkM365Status() {
    try {
        const d = await apiFetch('/auth/status');
        const dot = document.getElementById('m365Dot');
        const label = document.getElementById('m365Label');
        const wrap = document.getElementById('m365Status');
        if (!dot) return;

        // Store user info globally
        if (d.user_role) window.userRole = d.user_role;
        if (d.user_name) window.userName = d.user_name;
        if (d.user_email) window.userEmail = d.user_email;
        if (d.user_id) window.userId = d.user_id;

        // Apply role-based UI visibility
        applyRoleGating();

        if (d.connected) {
            const connectedCount = (d.users || []).filter(u => u.status === 'connected').length;
            dot.className = 'm365-dot green';
            label.textContent = `M365 · ${connectedCount} user${connectedCount !== 1 ? 's' : ''}`;
            // Build tooltip with user details
            const tips = (d.users || []).map(u => {
                const icon = u.status === 'connected' ? '●' : u.status === 'expired' ? '○' : '✕';
                const scan = u.last_inbox_scan ? `scanned ${fmtRelative(u.last_inbox_scan)}` : 'never scanned';
                return `${icon} ${u.name} — ${scan}`;
            });
            wrap.title = tips.join('\n');
        } else {
            dot.className = 'm365-dot red';
            label.textContent = 'M365 Disconnected';
            wrap.title = 'Click Logout and log in again to reconnect';
            wrap.style.cursor = 'pointer';
            wrap.onclick = () => { window.location.href = '/auth/login'; };
        }
    } catch(e) {
        // Silent fail — indicator stays gray
    }
}

// Refresh M365 status every 5 min
const _m365Timer = setInterval(checkM365Status, 300000);
window.addEventListener('beforeunload', (e) => {
    clearInterval(_m365Timer);
    if (formIsDirty) {
        e.preventDefault();
        e.returnValue = '';
    }
});

// ── Mobile Offer Feed (stub) ────────────────────────────────────────────
let _offerFeedFilter = 'pending';
function _setOfferFeedFilter(filter, btn) {
    _offerFeedFilter = filter;
    document.querySelectorAll('#offerFeedTabs .m-tab-pill').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    loadOfferFeed();
}
async function loadOfferFeed() {
    const listEl = document.getElementById('offerFeedList');
    const summaryEl = document.getElementById('offerFeedSummary');
    if (!listEl) return;
    listEl.innerHTML = '<div style="text-align:center;padding:24px;color:var(--muted);font-size:12px">Loading offers\u2026</div>';
    try {
        const data = await apiFetch('/api/offers/review-queue');
        const allOffers = data.offers || data || [];
        let filtered = allOffers;
        if (_offerFeedFilter === 'pending') filtered = allOffers.filter(o => o.status === 'pending_review' || o.status === 'pending');
        else if (_offerFeedFilter === 'accepted') filtered = allOffers.filter(o => o.status === 'active' || o.status === 'won' || o.status === 'accepted');
        if (summaryEl) {
            const pending = allOffers.filter(o => o.status === 'pending_review' || o.status === 'pending').length;
            summaryEl.innerHTML = '<span style="font-size:12px">' + allOffers.length + ' total \u00b7 ' + pending + ' pending review</span>';
        }
        if (!filtered.length) { listEl.innerHTML = '<div style="text-align:center;padding:24px;color:var(--muted);font-size:13px">No offers match this filter</div>'; return; }
        let html = '';
        for (const o of filtered) {
            const price = o.unit_price != null ? '$' + parseFloat(o.unit_price).toFixed(4) : '\u2014';
            const isPending = o.status === 'pending_review' || o.status === 'pending';
            const borderColor = isPending ? 'var(--amber)' : 'var(--border)';
            let priceColor = 'var(--teal)';
            if (o.target_price != null && o.unit_price != null) {
                const pctD = ((o.unit_price - o.target_price) / o.target_price) * 100;
                priceColor = pctD <= 0 ? 'var(--green)' : pctD <= 15 ? 'var(--amber)' : 'var(--red)';
            }
            html += '<div class="m-card" style="cursor:pointer;margin-bottom:6px;border-left:3px solid ' + borderColor + '" onclick="goToReq(' + (o.requisition_id || 0) + ')">';
            html += '<div class="m-card-header"><span style="font-weight:600;font-size:13px">' + esc(o.vendor_name || 'Unknown') + '</span>';
            if (isPending) html += '<span class="m-chip m-chip-amber" style="font-size:10px">PENDING</span>';
            html += '</div><div class="m-card-body" style="margin-top:6px;gap:12px">';
            html += '<div style="display:flex;flex-direction:column;gap:2px"><span style="font-size:11px;color:var(--muted)">Unit Price</span>';
            html += '<span style="font-size:16px;font-weight:700;color:' + priceColor + '">' + price + '</span></div>';
            html += '<div style="display:flex;flex-direction:column;gap:2px"><span style="font-size:11px;color:var(--muted)">Qty</span>';
            html += '<span style="font-size:13px;font-weight:600">' + (o.qty_available != null ? Number(o.qty_available).toLocaleString() : '\u2014') + '</span></div>';
            if (o.mpn) { html += '<div style="display:flex;flex-direction:column;gap:2px"><span style="font-size:11px;color:var(--muted)">MPN</span><span class="mono" style="font-size:12px">' + esc(o.mpn) + '</span></div>'; }
            html += '</div>';
            const dp = [];
            if (o.lead_time) dp.push('Lead: ' + esc(o.lead_time));
            if (o.condition) dp.push('Cond: ' + esc(o.condition));
            if (o.manufacturer) dp.push('Mfr: ' + esc(o.manufacturer));
            if (o.source) dp.push('Source: ' + esc(o.source));
            if (o.created_at) dp.push(_timeAgo(o.created_at));
            if (dp.length) html += '<div style="font-size:11px;color:var(--muted);margin-top:4px;line-height:1.5">' + dp.join(' | ') + '</div>';
            html += '</div>';
        }
        listEl.innerHTML = html;
    } catch(e) { listEl.innerHTML = '<div style="text-align:center;padding:24px;color:var(--red);font-size:12px">Failed to load offers</div>'; }
}
window.loadOfferFeed = loadOfferFeed;

function switchEnrichmentTab(tabId, btn) {
    document.querySelectorAll('.enrichment-tab-content').forEach(function(el) {
        el.classList.remove('active');
    });
    var panel = document.getElementById('enrichment-' + tabId);
    if (panel) panel.classList.add('active');
    document.querySelectorAll('.enrichment-tab-btn').forEach(function(b) {
        b.classList.remove('active');
    });
    if (btn) btn.classList.add('active');
}
window.switchEnrichmentTab = switchEnrichmentTab;

// ── API Health Polling ──────────────────────────────────────────────────
window._apiHealthErrors = [];
async function pollApiHealth() {
    try {
        const data = await apiFetch('/api/system/alerts');
        const alerts = data.alerts || [];
        window._apiHealthAlerts = alerts;
        window._apiHealthErrors = alerts; // backward compat

        // Update sidebar badge
        const badge = document.getElementById('apiHealthBadge');
        if (badge) {
            if (alerts.length > 0) {
                badge.textContent = alerts.length;
                badge.title = alerts.map(a => a.source_name || a.message || 'Unknown').join(', ');
                badge.classList.remove('u-hidden');
                badge.style.display = 'inline-block';
            } else {
                badge.classList.add('u-hidden');
                badge.style.display = '';
                badge.title = '';
            }
        }

        // Update subbar icon (backward compat)
        const icon = document.getElementById('subbarHealthWarn');
        if (icon) icon.style.display = alerts.length > 0 ? 'inline-flex' : 'none';
    } catch(e) { /* silent */ }
}
pollApiHealth();
const _healthTimer = setInterval(pollApiHealth, 60000);
window.addEventListener('beforeunload', () => clearInterval(_healthTimer));

// Health tooltip for subbar warning icon
let _healthTooltipEl = null;
function showHealthTooltip(evt) {
    const errors = window._apiHealthErrors || [];
    if (!errors.length) return;
    if (_healthTooltipEl) _healthTooltipEl.remove();
    const tip = document.createElement('div');
    tip.style.cssText = 'position:fixed;z-index:9999;background:#1e293b;color:#f1f5f9;font-size:11px;padding:8px 12px;border-radius:6px;max-width:320px;box-shadow:0 4px 12px rgba(0,0,0,.25);pointer-events:none';
    // All values escaped via esc() to prevent XSS
    tip.innerHTML = '<b style="color:#f59e0b">API Issues</b><br>' + errors.map(a => esc(a.source || a.name || 'Unknown') + ': ' + esc(a.status || a.error || 'error')).join('<br>');  // nosec: all dynamic values escaped via esc()
    document.body.appendChild(tip);
    const rect = (evt.target || evt.currentTarget).getBoundingClientRect();
    tip.style.top = (rect.bottom + 6) + 'px';
    tip.style.left = Math.max(8, rect.left - tip.offsetWidth / 2) + 'px';
    _healthTooltipEl = tip;
}
function hideHealthTooltip() {
    if (_healthTooltipEl) { _healthTooltipEl.remove(); _healthTooltipEl = null; }
}
window.showHealthTooltip = showHealthTooltip;
window.hideHealthTooltip = hideHealthTooltip;

// ── Role-Based UI Gating ────────────────────────────────────────────────
function applyRoleGating() {
    const role = window.userRole;
    const isAdmin = window.__isAdmin;

    // Elements with data-role="buyer" visible for buyer/trader/manager/admin
    const canBuy = ['buyer','trader','manager','admin'].includes(role) || isAdmin;
    document.querySelectorAll('[data-role="buyer"]').forEach(el => {
        el.style.display = canBuy ? '' : 'none';
    });

    // Role badge (hidden but used for gating)
    const roleBadge = document.getElementById('roleBadge');
    if (roleBadge) {
        const roleLabels = {buyer:'Buyer', sales:'Sales', trader:'Trader', manager:'Manager', admin:'Admin'};
        roleBadge.textContent = roleLabels[role] || role;
        roleBadge.className = `role-badge role-${role}`;
        roleBadge.style.display = 'none';
    }

    // ── Opportunity Management ──
    const bpNav = document.getElementById('navBuyPlans');
    if (bpNav) bpNav.style.display = '';

    // Proactive: visible to all
    const pNav = document.getElementById('navProactive');
    if (pNav) {
        pNav.style.display = '';
        if (['sales','trader'].includes(role) || isAdmin) refreshProactiveBadge();
    }

    // ── CRM Section — Role-based visibility ──
    const navVendors = document.getElementById('navVendors');
    const navCustomers = document.getElementById('navCustomers');

    // Sales: Customers only — no Vendors
    // Buyers: Vendors only — no Customers
    // Traders/Admin/Manager: all visible
    if (role === 'sales') {
        if (navVendors) navVendors.style.display = 'none';
    } else if (role === 'buyer') {
        if (navCustomers) navCustomers.style.display = 'none';
    }

    // ── Prospecting: sales/trader/manager/admin only ──
    const navProspecting = document.getElementById('navProspecting');
    if (navProspecting) {
        const canProspect = isAdmin || ['sales','trader','manager','admin'].includes(role);
        navProspecting.style.display = canProspect ? '' : 'none';
    }

    // ── Settings: admin only ──
    const navSettings = document.getElementById('navSettings');
    if (navSettings) { if (isAdmin) navSettings.classList.remove('u-hidden'); else navSettings.classList.add('u-hidden'); }

    // Apollo tab in account drawer: admin only
    document.querySelectorAll('.apollo-admin-only').forEach(el => { el.style.display = isAdmin ? '' : 'none'; });

    // "My Accounts" toggle: admin/manager/trader
    const myAccountsBtn = document.getElementById('myAccountsBtn');
    if (myAccountsBtn) {
        const canSeeMyAccounts = isAdmin || ['manager','trader','admin'].includes(role);
        if (!canSeeMyAccounts) myAccountsBtn.style.display = 'none';
    }
}
function isBuyer() { return ['buyer','trader','manager','admin'].includes(window.userRole) || window.__isAdmin; }

export async function refreshProactiveBadge() {
    try {
        const data = await apiFetch('/api/proactive/count');
        const badge = document.getElementById('proactiveBadge');
        if (badge) {
            const c = data.count || 0;
            if (c > 0) { badge.textContent = c > 99 ? '99+' : c; badge.style.display = ''; }
            else { badge.style.display = 'none'; }
        }
    } catch (e) { logCatchError('proactiveBadge', e); }
}

// ── Navigation ──────────────────────────────────────────────────────────
const ALL_VIEWS = ['view-list', 'view-vendors', 'view-strategic', 'view-materials', 'view-customers', 'view-buyplans', 'view-proactive', 'view-settings', 'view-suggested', 'view-offers', 'view-alerts'];

// Hash-based routing for browser back/forward
const _viewToHash = {'view-list':'rfqs','view-vendors':'vendors','view-strategic':'strategic','view-materials':'materials','view-customers':'customers','view-buyplans':'buyplans','view-proactive':'proactive','view-settings':'settings','view-suggested':'suggested','view-offers':'offers','view-alerts':'alerts'};
const _hashToView = Object.fromEntries(Object.entries(_viewToHash).map(([k,v])=>[v,k]));
_hashToView['apihealth'] = 'view-settings'; // apihealth moved into settings
_hashToView['prospecting'] = 'view-suggested'; // old prospecting view removed
let _navFromPopstate = false;

let _lastPushedHash = '';
function _pushNav(viewId, reqId) {
    if (_navFromPopstate) return;
    var hashStr = _viewToHash[viewId];
    if (!hashStr) { console.warn('_pushNav: unknown viewId', viewId); hashStr = 'rfqs'; }
    if (reqId && viewId === 'view-list') hashStr = 'rfqs/' + reqId;
    var hash = '#' + hashStr;
    if (hash === _lastPushedHash) return;
    _lastPushedHash = hash;
    if (!location.hash || location.hash === '#') {
        history.replaceState({view: viewId, reqId: reqId || null}, '', hash);
    } else {
        history.pushState({view: viewId, reqId: reqId || null}, '', hash);
    }
}

window.addEventListener('popstate', (e) => {
    const hash = location.hash.replace('#','');
    // Skip approve-token hashes (handled by crm.js)
    if (hash.startsWith('approve-token/')) return;
    // Parse drill-down hash: rfqs/123
    var drillId = null;
    var baseHash = hash;
    if (hash.startsWith('rfqs/')) {
        drillId = parseInt(hash.split('/')[1]);
        baseHash = 'rfqs';
    }
    const viewId = _hashToView[baseHash] || 'view-list';
    _navFromPopstate = true;
    try {
    // Close any open modals first
    document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open'));
    // Route to the correct view
    const routes = {
        'view-list': () => {
            showView('view-list');
            setMainPill('active');
            _reqFullyLoaded = false;
            _collapseAllDrillDowns();
            loadRequisitions(); // re-fetch requirements list
            if (drillId) setTimeout(() => toggleDrillDown(drillId), 100);
        },
        'view-vendors': () => showVendors(),
        'view-materials': () => showMaterials(),
        'view-customers': () => window.showCustomers(),
        'view-buyplans': () => window.showBuyPlans(),
        'view-proactive': () => window.showProactiveOffers(),
        'view-settings': () => {
            var settingsTab = (baseHash === 'apihealth') ? baseHash : undefined;
            window.showSettings(settingsTab);
        },
        'view-suggested': () => window.showSuggested(),
    };
    if (routes[viewId]) routes[viewId]();
    // Highlight correct sidebar button
    const sidebarMap = {'view-list':'navReqs','view-vendors':'navVendors','view-materials':'navMaterials','view-customers':'navCustomers','view-buyplans':'navBuyPlans','view-proactive':'navProactive','view-settings':'navSettings','view-suggested':'navProspecting'};
    const navBtn = document.getElementById(sidebarMap[viewId]);
    if (navBtn) navHighlight(navBtn);
    } catch(e) { console.error('popstate error:', e); }
    finally { _navFromPopstate = false; }
});

const _viewScrollPos = {};  // viewId → scrollTop
let _currentViewId = 'view-list';

/** Navigate to a requisition's drill-down view by ID. */
function goToReq(reqId) {
    if (!reqId) return;
    showView('requisitions');
    toggleDrillDown(reqId);
}

export function showView(viewId) {
    // Save scroll position for the view we're leaving
    var scroller = document.querySelector('.main-scroll');
    if (scroller && _currentViewId) _viewScrollPos[_currentViewId] = scroller.scrollTop;
    _currentViewId = viewId;
    try { _pushNav(viewId); } catch(e) { console.warn('pushNav:', e); }
    for (const id of ALL_VIEWS) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (id === viewId) {
            el.classList.remove('hidden', 'u-hidden');
            el.style.display = '';
        } else {
            el.classList.add('u-hidden');
            el.style.display = 'none';
        }
    }
    // Restore scroll position for the view we're entering
    if (scroller) {
        var saved = _viewScrollPos[viewId];
        scroller.scrollTop = saved || 0;
    }
    // Clean up background polling when navigating away from settings/enrichment
    if (typeof _bfPollInterval !== 'undefined' && _bfPollInterval) {
        clearInterval(_bfPollInterval);
        _bfPollInterval = null;
    }
    // Toggle body class so CSS can adjust sidebar for settings view
    const isSettings = viewId === 'view-settings';
    document.body.classList.toggle('on-settings', isSettings);
    // Close any open CRM drawers, reset split-pane state, and cancel in-flight fetches
    if (typeof closeCustDrawer === 'function') try { closeCustDrawer(); } catch(e) {}
    if (typeof closeVendorDrawer === 'function') try { closeVendorDrawer(); } catch(e) {}
    if (typeof _abortAllCrmFetches === 'function') try { _abortAllCrmFetches(); } catch(e) {}
    // Clear top breadcrumb when switching views (CRM views will re-set it)
    const topBc = document.getElementById('topBreadcrumb');
    if (topBc) topBc.style.display = 'none';
    // Hide topcontrols on settings; show pills/search/filters only on list view
    const topcontrols = document.getElementById('topcontrols');
    if (topcontrols) {
        topcontrols.style.display = isSettings ? 'none' : '';
        const isListView = viewId === 'view-list';
        topcontrols.querySelectorAll('.fpills, .filter-wrap').forEach(el => {
            el.style.display = isListView ? '' : 'none';
        });
    }
    // Mirror visibility for mobile toolbar — only show on list view
    const mobileToolbar = document.getElementById('mobileToolbar');
    if (mobileToolbar) {
        const isListView = viewId === 'view-list';
        mobileToolbar.style.display = (isSettings || !isListView) ? 'none' : '';
    }
    // Show intake bar on views where data entry is relevant
    const intakeViews = ['view-list', 'view-materials', 'view-vendors', 'view-buyplans'];
    const intakeBar = document.getElementById('intakeBar');
    if (intakeBar) intakeBar.style.display = intakeViews.includes(viewId) ? '' : 'none';
}

function showList() {
    showView('view-list');
    currentReqId = null;
    safeRemove('lastReqId'); safeRemove('lastReqName');
    const mainSearch = document.getElementById('mainSearch');
    if (mainSearch) mainSearch.value = '';
    _serverSearchActive = false;
    // Reset to consistent state matching the active main pill
    if (_currentMainView !== 'archive') _reqStatusFilter = 'all';
    loadRequisitions();
}

// showDetail — redirects to inline drill-down (detail page removed)
function showDetail(id, name, tab) {
    showView('view-list');
    currentReqId = id;
    currentReqName = name;
    safeSet('lastReqId', id); safeSet('lastReqName', name || '');
    setTimeout(() => toggleDrillDown(id), 200);
}

function showVendors() {
    showView('view-vendors');
    const viewEl = document.getElementById('view-vendors');
    if (viewEl) { viewEl.classList.remove('u-hidden'); viewEl.style.display = 'flex'; }
    currentReqId = null;
    if (window._setTopViewLabel) window._setTopViewLabel('Vendors');
    loadVendorList();
}

function showMaterials() {
    showView('view-materials');
    navHighlight(document.getElementById('navMaterials'));
    currentReqId = null;
    const imp = document.getElementById('stockImportArea');
    if (imp) imp.classList.add('u-hidden');
    loadMaterialList();
}



let _dashPeriod = '30d';
let _dashScope = 'my';           // always 'my' — team averages shown inline
let _dashUserId = null;          // specific user to view in CC — null = current user
let _buyerScope = 'my';          // always 'my' — team averages shown inline
let _dashPerspective = null;     // 'sales' or 'purchasing' — null = auto from role

function setDashPeriod(period, btn) {
    _dashPeriod = period;
    document.querySelectorAll('#dashPeriodPills .chip').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    loadDashboard();
}

function setDashScope() {}   // no-op — kept for backward compat
function setBuyerScope() {}  // no-op — kept for backward compat

function setDashUserFilter(val) {
    if (val === '' || val === String(window.userId)) {
        _dashUserId = null;
    } else {
        _dashUserId = parseInt(val);
    }
    loadDashboard();
}

async function _populateDashUserSelect() {
    const sel = document.getElementById('dashUserSelect');
    if (!sel) return;
    let users = window._userFilterList;
    if (!users) {
        try { users = await apiFetch('/api/users/list'); } catch(e) { users = []; }
        window._userFilterList = users;
    }
    const myId = window.userId;
    sel.innerHTML = '<option value="">My Work</option>' +
        users.filter(u => u.id !== myId).map(u =>
            '<option value="' + u.id + '"' +
            (_dashUserId === u.id ? ' selected' : '') +
            '>' + esc(u.name) + '</option>').join('');
}

function setDashPerspective(p, btn) {
    _dashPerspective = p;
    document.querySelectorAll('#ccPerspectivePills .cc-persp-btn').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    loadDashboard();
}

function _isMultiRole() {
    return ['trader','manager','admin'].includes(window.userRole) || window.__isAdmin;
}

function _effectivePerspective() {
    if (_dashPerspective) return _dashPerspective;
    // Auto: buyer→purchasing, sales→sales, multi-role→purchasing
    return window.userRole === 'sales' ? 'sales' : 'purchasing';
}

// [Scorecard removed]
// ── Modals ──────────────────────────────────────────────────────────────
function openNewReqModal() {
    openModal('newReqModal', 'nrName');
}
export function closeModal(id) {
    var el = document.getElementById(id);
    if (el) el.classList.remove('open');
    var entry = _modalStack.pop();
    if (entry && entry.returnFocus && entry.returnFocus.focus) {
        try { entry.returnFocus.focus(); } catch(e) {}
    }
}

/* ── Confirm / Prompt replacements ─────────────────────────────────── */
function confirmAction(title, message, onConfirm, opts) {
    opts = opts || {};
    var id = '_confirmModal';
    var existing = document.getElementById(id);
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.id = id;
    overlay.className = 'modal-bg open';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:10000;display:flex;align-items:center;justify-content:center';

    var box = document.createElement('div');
    box.style.cssText = 'background:var(--white,#fff);border-radius:10px;padding:24px;max-width:420px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.25)';

    var h = document.createElement('h3');
    h.style.cssText = 'margin:0 0 8px;font-size:16px';
    h.textContent = title;

    var p = document.createElement('p');
    p.style.cssText = 'margin:0 0 20px;font-size:13px;color:var(--text2,#555)';
    p.textContent = message;

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';

    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn';
    cancelBtn.textContent = opts.cancelLabel || 'Cancel';
    var escHandler = function(e) { if (e.key === 'Escape') closeConfirm(); };
    var closeConfirm = function() {
        overlay.remove();
        document.removeEventListener('keydown', escHandler);
    };
    cancelBtn.onclick = function() { closeConfirm(); if (opts.onCancel) opts.onCancel(); };

    var confirmBtn = document.createElement('button');
    confirmBtn.className = 'btn ' + (opts.confirmClass || 'btn-primary');
    confirmBtn.textContent = opts.confirmLabel || 'Confirm';
    confirmBtn.onclick = function() { closeConfirm(); onConfirm(); };

    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(confirmBtn);
    box.appendChild(h);
    box.appendChild(p);
    box.appendChild(btnRow);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // Close on Escape
    document.addEventListener('keydown', escHandler);
    confirmBtn.focus();
}

function promptInput(title, label, onSubmit, opts) {
    opts = opts || {};
    var id = '_promptModal';
    var existing = document.getElementById(id);
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.id = id;
    overlay.className = 'modal-bg open';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:10000;display:flex;align-items:center;justify-content:center';

    var box = document.createElement('div');
    box.style.cssText = 'background:var(--white,#fff);border-radius:10px;padding:24px;max-width:420px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.25)';

    var h = document.createElement('h3');
    h.style.cssText = 'margin:0 0 8px;font-size:16px';
    h.textContent = title;

    var lbl = document.createElement('label');
    lbl.style.cssText = 'display:block;font-size:13px;color:var(--text2,#555);margin-bottom:6px';
    lbl.textContent = label;

    var inp = document.createElement('input');
    inp.type = opts.inputType || 'text';
    inp.className = 'form-input';
    inp.style.cssText = 'width:100%;margin-bottom:16px';
    if (opts.placeholder) inp.placeholder = opts.placeholder;
    if (opts.defaultValue) inp.value = opts.defaultValue;

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';

    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn';
    cancelBtn.textContent = opts.cancelLabel || 'Cancel';
    cancelBtn.onclick = function() { overlay.remove(); if (opts.onCancel) opts.onCancel(); };

    var submitBtn = document.createElement('button');
    submitBtn.className = 'btn btn-primary';
    submitBtn.textContent = opts.submitLabel || 'Submit';
    submitBtn.onclick = function() {
        var val = inp.value;
        if (opts.required && !val.trim()) { inp.classList.add('field-error'); return; }
        overlay.remove();
        onSubmit(val);
    };

    inp.addEventListener('keydown', function(e) { if (e.key === 'Enter') submitBtn.click(); });

    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(submitBtn);
    box.appendChild(h);
    box.appendChild(lbl);
    box.appendChild(inp);
    box.appendChild(btnRow);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    var escHandler = function(e) { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', escHandler); } };
    document.addEventListener('keydown', escHandler);
    inp.focus();
}

window.confirmAction = confirmAction;
window.promptInput = promptInput;

export function showToast(msg, type = 'info', durationOrOpts = 3000) {
    // Support: showToast(msg, type, duration) or showToast(msg, type, { duration, action: { label, fn } })
    let duration = typeof durationOrOpts === 'number' ? durationOrOpts : (durationOrOpts?.duration ?? 3000);
    const action = typeof durationOrOpts === 'object' ? durationOrOpts?.action : null;

    // Error toasts with no explicit duration persist longer (10s) so user can read/act
    if (type === 'error' && typeof durationOrOpts !== 'number' && !durationOrOpts?.duration) duration = 10000;

    let container = document.getElementById('toastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toastContainer';
        container.setAttribute('aria-live', 'polite');
        container.setAttribute('role', 'status');
        container.style.cssText = 'position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    const colors = { info: 'var(--teal)', success: 'var(--green)', error: 'var(--red)', warn: 'var(--amber)' };
    toast.style.cssText = `background:var(--bg2);border-left:4px solid ${colors[type]||colors.info};color:var(--text);padding:10px 16px;border-radius:6px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.25);max-width:400px;opacity:0;transition:opacity .2s;display:flex;align-items:center;gap:10px`;
    // Safe: all callers use esc() for user-controlled values before passing to showToast
    let html = `<span style="flex:1">${msg}</span>`;
    if (action && action.label) {
        html += `<button class="toast-action-btn" style="background:none;border:1px solid ${colors[type]||colors.info};color:${colors[type]||colors.info};padding:2px 8px;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap">${esc(action.label)}</button>`;
    }
    html += `<button style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0 2px;line-height:1" title="Dismiss">\u00d7</button>`;
    toast.innerHTML = html;
    // Wire action button
    if (action && action.fn) {
        const actionBtn = toast.querySelector('.toast-action-btn');
        if (actionBtn) actionBtn.addEventListener('click', () => { toast.remove(); action.fn(); });
    }
    // Wire dismiss button
    toast.querySelector('button:last-child').addEventListener('click', () => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 200); });
    container.appendChild(toast);
    requestAnimationFrame(() => toast.style.opacity = '1');
    if (duration > 0) {
        setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, duration);
    }
}

// Dismiss all toasts on Escape key
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        const container = document.getElementById('toastContainer');
        if (container) container.querySelectorAll('[role]').forEach(t => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); });
    }
});

const _statusLabels = {draft:'Draft',active:'Active',sourcing:'Active',closed:'Closed',offers:'Offers',quoting:'Quoting',quoted:'Quoted',reopened:'Reopened',won:'Won',lost:'Lost',archived:'Archived'};
function updateDetailStatus(status) {
    const chip = document.getElementById('detailStatus');
    if (!chip) return;
    chip.className = 'status-chip status-' + status;
    chip.textContent = _statusLabels[status] || status;
    chip.classList.remove('pulse');
    void chip.offsetWidth;
    chip.classList.add('pulse');
}
export function notifyStatusChange(data) {
    if (!data || !data.status_changed) return;
    updateDetailStatus(data.req_status);
    const reqInfo = _reqListData.find(r => r.id === currentReqId);
    if (reqInfo) reqInfo.status = data.req_status;
}
function _refreshReqRow(reqId) {
    const reqInfo = _reqListData.find(r => r.id === reqId);
    if (!reqInfo) return;
    const oldRow = document.querySelector(`tr[onclick*="toggleDrillDown(${reqId})"]`);
    if (!oldRow) return;
    const tmp = document.createElement('tbody');
    tmp.innerHTML = _renderReqRow(reqInfo);
    const newRow = tmp.firstElementChild;
    if (newRow) oldRow.replaceWith(newRow);
}

// ── Requisitions ────────────────────────────────────────────────────────
let _reqCustomerMap = {};  // id → customer_display
let _reqListData = [];     // cached list for client-side filtering
let _reqStatusFilter = 'all';
let _reqListSort = 'newest';
let _myReqsOnly = false;   // "My Reqs" toggle for non-sales roles
let _filterUserId = null;  // User dropdown filter — null = all, id = specific user
let _serverSearchActive = false; // True when server-side search returned filtered results
// Main view: 'reqs' (Pipeline), 'deals', 'archive'. Legacy 'sales'/'sourcing'/'purchasing' → 'reqs'.
let _currentMainView = localStorage.getItem('avail_main_view') || 'reqs';
if (_currentMainView === 'sales' || _currentMainView === 'sourcing' || _currentMainView === 'purchasing') _currentMainView = 'reqs';
let _archiveGroupsOpen = new Set();  // company_id or customer_display keys that are expanded



const debouncedReqListSearch = debounce(() => {
    const q = (document.getElementById('reqListFilter')?.value || '').trim();
    if (q.length >= 2) loadRequisitions(q);
    else if (q.length === 0) loadRequisitions();
    else renderReqList();  // Short input: client-side only
}, 300);


let _reqAbort = null;  // AbortController for in-flight requisition searches
let _reqSearchSeq = 0; // Sequence counter to discard stale responses

let _archiveHasMore = false;
let _archivePageSize = 75;
let _archivePage = 1;
let _archiveTotal = 0;
let _reqFullyLoaded = false; // true once all 200 reqs loaded

export async function loadRequisitions(query = '', append = false) {
    // Cancel any in-flight request
    if (_reqAbort) { try { _reqAbort.abort(); } catch(e){} }
    _reqAbort = new AbortController();
    const signal = _reqAbort.signal;
    const thisSeq = ++_reqSearchSeq;
    try {
        const isArchive = _currentMainView === 'archive';
        const status = isArchive ? '&status=archive' : '';
        const offset = isArchive ? (_archivePage - 1) * _archivePageSize : (append ? _reqListData.length : 0);
        // Fast initial paint: load 50 first, then fetch remaining in background
        const isInitial = !query && !append && !isArchive && !_reqFullyLoaded;
        const limit = isArchive ? _archivePageSize : (isInitial ? 50 : 200);
        const url = query
            ? `/api/requisitions?q=${encodeURIComponent(query)}${status}`
            : `/api/requisitions?limit=${limit}&offset=${offset}${status}`;
        _serverSearchActive = !!query;
        // Show spinner on search buttons
        document.querySelectorAll('.search-btn').forEach(el => el.classList.add('loading'));
        const resp = await apiFetch(url, { signal });
        // Discard stale response if a newer request was fired
        if (thisSeq !== _reqSearchSeq) return;
        const items = resp.requisitions || resp;
        if (append) {
            _reqListData = _reqListData.concat(items);
        } else {
            _reqListData = items;
            // Fresh data from server — clear drill-down caches
            _ddReqCache = {};
            _ddSightingsCache = {};
            _ddSelectedSightings = {};
            _ddTierState = {};
            _partExpandState = {};
            _partActiveTab = {};
            _partDetailCache = {};
            for (const k of Object.keys(_ddTabCache)) delete _ddTabCache[k];
        }
        _archiveHasMore = _currentMainView === 'archive' && items.length >= limit;
        if (_currentMainView === 'archive') _archiveTotal = resp.total || items.length;
        _reqListData.forEach(r => { if (r.customer_display) _reqCustomerMap[r.id] = r.customer_display; });
        renderReqList();
        // Background: fetch remaining reqs if we only loaded the first 50
        if (isInitial && items.length >= 50) {
            _reqFullyLoaded = true;
            const bgView = _currentMainView;
            apiFetch(`/api/requisitions?limit=200&offset=0${status}`, { signal }).then(full => {
                if (thisSeq !== _reqSearchSeq) return; // stale — newer request fired
                if (_currentMainView !== bgView) return; // stale — user switched tabs
                const fullItems = full.requisitions || full;
                if (Array.isArray(fullItems) && fullItems.length > _reqListData.length) {
                    _reqListData = fullItems;
                    _reqListData.forEach(r => { if (r.customer_display) _reqCustomerMap[r.id] = r.customer_display; });
                    renderReqList();
                }
            }).catch(e => { if (e.name !== 'AbortError') console.warn('req list fetch error:', e); });
        } else if (!isInitial && !query) {
            _reqFullyLoaded = true;
        }
    } catch (e) {
        if (e.name === 'AbortError') return;
        logCatchError('loadRequisitions', e); showToast('Failed to load requisitions', 'error', { action: { label: 'Retry', fn: () => loadRequisitions() } });
    } finally {
        if (thisSeq === _reqSearchSeq) {
            document.querySelectorAll('.search-btn').forEach(el => el.classList.remove('loading'));
        }
    }
}

// v7 table sort state
let _reqSortCol = null;
let _reqSortDir = 'asc';

// Column visibility — persisted in localStorage
const _defaultHiddenCols = {};
let _hiddenCols = JSON.parse(localStorage.getItem('reqHiddenCols') || '{}');

function _isColHidden(col) { return !!_hiddenCols[col]; }
function toggleColVisibility(col) {
    _hiddenCols[col] = !_hiddenCols[col];
    if (!_hiddenCols[col]) delete _hiddenCols[col];
    localStorage.setItem('reqHiddenCols', JSON.stringify(_hiddenCols));
    renderReqList();
}
function _toggleColGear() {
    const wrap = document.getElementById('colGearWrap');
    if (!wrap) return;
    if (wrap.innerHTML) { wrap.innerHTML = ''; return; }
    wrap.innerHTML = _colGearDropdown();
    // Close on outside click
    const close = (e) => { if (!wrap.contains(e.target)) { wrap.innerHTML = ''; document.removeEventListener('click', close); } };
    setTimeout(() => document.addEventListener('click', close), 0);
}

function _applyColVisCSS() {
    let style = document.getElementById('colVisStyle');
    if (!style) { style = document.createElement('style'); style.id = 'colVisStyle'; document.head.appendChild(style); }
    const v = _currentMainView;
    // Map column keys to 1-based nth-child positions per view (reqs/deals = pipeline columns)
    let colMap;
    if (v === 'archive') colMap = {reqs:3,offers:4,status:5,matches:6,sales:7,age:8};
    else colMap = {reqs:3,sourced:4,sent:5,resp:6,offers:7,sales:8,age:9};
    const rules = [];
    for (const [k, nth] of Object.entries(colMap)) {
        if (_isColHidden(k)) rules.push(`#reqList > table > thead > tr > th:nth-child(${nth}), #reqList > table > tbody > tr.rrow > td:nth-child(${nth}) { display: none; }`);
    }
    style.textContent = rules.join('\n');
}

function _colGearDropdown() {
    const v = _currentMainView;
    let cols;
    if (v === 'archive') cols = [{k:'reqs',l:'Parts'},{k:'offers',l:'Offers'},{k:'status',l:'Outcome'},{k:'matches',l:'Matches'},{k:'sales',l:'Sales'},{k:'age',l:'Age'}];
    else cols = [{k:'reqs',l:'Parts'},{k:'sourced',l:'Sourced'},{k:'sent',l:'RFQs Sent'},{k:'resp',l:'Response'},{k:'offers',l:'Offers'},{k:'sales',l:'Sales'},{k:'age',l:'Age'}];
    let html = '<div class="col-gear-dd" id="colGearDropdown" onclick="event.stopPropagation()">';
    html += '<div style="font-size:10px;font-weight:600;color:var(--muted);padding:4px 8px;text-transform:uppercase">Columns</div>';
    for (const c of cols) {
        const checked = !_isColHidden(c.k) ? 'checked' : '';
        html += `<label style="display:flex;align-items:center;gap:6px;padding:3px 8px;font-size:12px;cursor:pointer"><input type="checkbox" ${checked} onchange="toggleColVisibility('${c.k}')">${c.l}</label>`;
    }
    html += '</div>';
    return html;
}

function _sortArrow(col) {
    if (_reqSortCol !== col) return '\u21c5';
    return _reqSortDir === 'asc' ? '\u25b2' : '\u25bc';
}

function sortReqList(col) {
    if (_reqSortCol === col) {
        if (_reqSortDir === 'asc') _reqSortDir = 'desc';
        else { _reqSortCol = null; _reqSortDir = 'asc'; }
    } else {
        _reqSortCol = col;
        _reqSortDir = 'asc';
    }
    renderReqList();
}

// ── Drill-Down Sub-Tab State ────────────────────────────────────────────
const _ddTabCache = {};   // reqId → { sightings: data, activity: data, offers: data, ... }
window._ddTabCache = _ddTabCache; // Expose for cross-module cache invalidation
const _ddActiveTab = {};  // reqId → current sub-tab name

function _ddSubTabs(mainView) {
    if (mainView === 'archive' || _reqStatusFilter === 'archive') return ['workspace', 'quote', 'activity'];
    // Unified view: workspace is the primary part-centric view
    return ['workspace', 'quote', 'activity'];
}

function _ddDefaultTab(mainView) {
    // All views default to the part-centric workspace
    return 'workspace';
}

function _ddTabLabel(tab) {
    const map = {
        workspace: 'Parts',
        sourcing: 'Sourcing',
        offers: 'Offers',
        quote: 'Quote',
        activity: 'Activity',
        // Legacy tab names still supported for backward compatibility
        details: 'Details', sightings: 'Sightings', parts: 'Parts',
        quotes: 'Quotes', buyplans: 'Buy Plans', files: 'Files', tasks: 'Tasks'
    };
    return map[tab] || tab;
}

async function expandToSubTab(reqId, tabName) {
    if (window.__isMobile) {
        _ddActiveTab[reqId] = tabName;
        _openMobileDrillDown(reqId);
        return;
    }
    let drow = document.getElementById('d-' + reqId);
    if (!drow) {
        drow = await waitForElement('#d-' + reqId, 2000);
        if (!drow) return;
    }
    if (!drow.classList.contains('open')) {
        await toggleDrillDown(reqId);
        // Wait for drill-down animation to complete
        await new Promise(function(r) { setTimeout(r, 350); });
    }
    _switchDdTab(reqId, tabName);
}

// ── Drill-down summary dashboard — at-a-glance stats per requisition ──
// Shows key metrics (parts, sourced, offers, RFQs sent, quote status) above sub-tabs.
// Called from _renderReqRow() and _openMobileDrillDown().
function _renderDdSummary(reqId) {
    const r = _reqListData.find(x => x.id === reqId);
    if (!r) return '';
    const total = r.requirement_count || 0;
    const sourced = r.sourced_count || 0;
    const offers = r.offer_count || 0;
    const sent = r.rfq_sent_count || 0;
    const respPct = sent > 0 ? Math.round(((r.reply_count || 0) / sent) * 100) : 0;
    const srcPct = total > 0 ? Math.round((sourced / total) * 100) : 0;
    const srcColor = srcPct >= 80 ? 'var(--green)' : srcPct >= 40 ? 'var(--amber)' : 'var(--red)';

    // Quote status badge
    let qBadge = '<span style="color:var(--muted)">\u2014</span>';
    const qs = r.quote_status;
    if (qs === 'won') qBadge = '<span style="color:var(--green);font-weight:600">Won</span>';
    else if (qs === 'lost') qBadge = '<span style="color:var(--red)">Lost</span>';
    else if (qs === 'sent') qBadge = '<span style="color:var(--blue)">Sent</span>';
    else if (qs === 'revised') qBadge = '<span style="color:var(--amber)">Revised</span>';
    else if (qs === 'draft') qBadge = '<span style="color:var(--muted)">Draft</span>';

    return `<div class="dd-summary">
        <div class="dd-stat dd-stat-link" onclick="event.stopPropagation();expandToSubTab(${reqId},'parts')"><span class="dd-stat-val">${total}</span><span class="dd-stat-label">Parts</span></div>
        <div class="dd-stat dd-stat-link" onclick="event.stopPropagation();expandToSubTab(${reqId},'sightings')" title="${sourced} of ${total} parts have supplier sightings"><span class="dd-stat-val" style="color:${srcColor}">${sourced}/${total}</span><span class="dd-stat-label">Sourced</span><div class="dd-stat-bar"><div class="dd-stat-bar-fill" style="width:${srcPct}%;background:${srcColor}"></div></div></div>
        <div class="dd-stat dd-stat-link" onclick="event.stopPropagation();expandToSubTab(${reqId},'offers')"><span class="dd-stat-val">${offers}</span><span class="dd-stat-label">Offers</span></div>
        <div class="dd-stat dd-stat-link" onclick="event.stopPropagation();expandToSubTab(${reqId},'activity')"><span class="dd-stat-val">${sent}</span><span class="dd-stat-label">RFQs Sent</span></div>
        <div class="dd-stat"><span class="dd-stat-val">${respPct}%</span><span class="dd-stat-label">Response</span></div>
        <div class="dd-stat dd-stat-link" onclick="event.stopPropagation();expandToSubTab(${reqId},'quotes')"><span class="dd-stat-val" style="font-size:12px">${qBadge}</span><span class="dd-stat-label">Quote</span></div>
    </div>`;
}

function _renderDdTabPills(reqId) {
    const tabs = _ddSubTabs(_currentMainView);
    const active = _ddActiveTab[reqId] || _ddDefaultTab(_currentMainView);
    const pills = tabs.map(t =>
        `<button class="dd-tab${t === active ? ' on' : ''}" data-tab="${t}" onclick="event.stopPropagation();_switchDdTab(${reqId},'${t}')">${_ddTabLabel(t)}</button>`
    ).join('');
    return pills + `<button class="dd-tab-refresh" onclick="event.stopPropagation();ddRefreshTab(${reqId})" title="Refresh">\u21bb</button>`;
}

async function ddRefreshTab(reqId) {
    const tabName = _ddActiveTab[reqId] || _ddDefaultTab(_currentMainView);
    // Clear cached data for this tab
    if (_ddTabCache[reqId]) delete _ddTabCache[reqId][tabName];
    if (tabName === 'sightings') delete _ddSightingsCache[reqId];
    const drow = document.getElementById('d-' + reqId);
    const panel = drow?.querySelector('.dd-panel');
    if (panel) await _loadDdSubTab(reqId, tabName, panel);
}

async function _switchDdTab(reqId, tabName) {
    _ddActiveTab[reqId] = tabName;
    delete _addRowActive[reqId];
    const drow = document.getElementById('d-' + reqId);
    if (!drow) return;
    // Clear new-offers flash when salesperson views offers
    if (tabName === 'offers') {
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo && reqInfo.has_new_offers) {
            reqInfo.has_new_offers = false;
            // Update the row button to stop flashing
            const row = document.getElementById('r-' + reqId);
            if (row) {
                const flashBtn = row.querySelector('.btn-flash');
                if (flashBtn) flashBtn.classList.remove('btn-flash');
            }
            // Also clear the new-offers dot
            const dot = row?.querySelector('.new-offers-dot');
            if (dot) dot.remove();
            // Persist dismissal server-side
            apiFetch(`/api/requisitions/${reqId}/dismiss-new-offers`, { method: 'POST' }).catch(e => console.warn('dismiss offers error:', e));
        }
    }
    // Update pill state
    drow.querySelectorAll('.dd-tab').forEach(t => t.classList.toggle('on', t.dataset.tab === tabName));
    const panel = drow.querySelector('.dd-panel');
    if (!panel) return;
    await _loadDdSubTab(reqId, tabName, panel);
}

async function _loadDdSubTab(reqId, tabName, panel) {
    if (!_ddTabCache[reqId]) _ddTabCache[reqId] = {};
    const cached = _ddTabCache[reqId][tabName];
    // Reset panel styles when switching away from workspace
    if (tabName !== 'workspace') {
        panel.style.maxHeight = '';
        panel.style.padding = '';
    }
    if (cached) { _renderDdTab(reqId, tabName, cached, panel); return; }

    panel.innerHTML = '<span style="font-size:11px;color:var(--muted)">Loading\u2026</span>';
    try {
        let data;
        switch (tabName) {
            case 'workspace':
                // Part-centric RFQ workspace — renders its own layout
                panel.style.maxHeight = 'none';
                panel.style.padding = '0';
                await rfqOpenWorkspace(reqId, panel);
                _ddTabCache[reqId][tabName] = true;
                return;
            case 'sourcing':
                // Parts list only — sightings available on Sourcing page
                {
                    const parts = _ddReqCache[reqId] || await apiFetch(`/api/requisitions/${reqId}/requirements`);
                    _ddReqCache[reqId] = parts;
                    data = { parts };
                }
                break;
            case 'details':
            case 'parts':
                data = await apiFetch(`/api/requisitions/${reqId}/requirements`);
                _ddReqCache[reqId] = data;
                break;
            case 'sightings':
                data = _ddSightingsCache[reqId] || await apiFetch(`/api/requisitions/${reqId}/sightings`);
                _ddSightingsCache[reqId] = data;
                if (!_ddSelectedSightings[reqId]) _ddSelectedSightings[reqId] = new Set();
                break;
            case 'activity':
                // Activity timeline only — tasks have their own tab
                data = await apiFetch(`/api/requisitions/${reqId}/activity`);
                break;
            case 'offers':
                data = await apiFetch(`/api/requisitions/${reqId}/offers`);
                break;
            case 'quotes':
                try {
                    data = await apiFetch(`/api/requisitions/${reqId}/quotes`);
                } catch (qErr) {
                    panel.innerHTML = '<span style="font-size:11px;color:var(--red)">Failed to load quotes — ' + esc(friendlyError(qErr, 'please try again')) + '</span>';
                    return;
                }
                if (!data || data.error) {
                    panel.innerHTML = '<span style="font-size:11px;color:var(--red)">Failed to load quotes' + (data && data.error ? ' — ' + esc(data.error) : '') + '</span>';
                    return;
                }
                break;
            case 'tasks':
                data = await apiFetch(`/api/requisitions/${reqId}/tasks`);
                break;
            case 'files':
                data = await apiFetch(`/api/requisitions/${reqId}/attachments`);
                break;
        }
        _ddTabCache[reqId][tabName] = data;
        _renderDdTab(reqId, tabName, data, panel);
    } catch(e) {
        panel.innerHTML = '<span style="font-size:11px;color:var(--red)">Failed to load</span>';
    }
}

function _renderDdTab(reqId, tabName, data, panel) {
    // Mobile: use card-based renderers for touch-friendly display
    if (window.__isMobile) {
        switch (tabName) {
            case 'details': _renderDdDetails(reqId, panel); break;
            case 'parts': _renderMobilePartsList(data || _ddReqCache[reqId] || [], reqId, panel); break;
            case 'sightings':
                if (data && !_ddSightingsCache[reqId]) _ddSightingsCache[reqId] = data;
                _renderSourcingDrillDown(reqId, panel);
                break;
            case 'activity': _renderMobileActivityList(reqId, data, panel); break;
            case 'offers': _renderMobileOffersList(data, reqId, panel); break;
            case 'quotes': _renderMobileQuotesList(data, reqId, panel); break;
            case 'buyplans': _renderMobileBuyPlansList(data, reqId, panel); break;
            case 'tasks': _renderDdTasks(reqId, data, panel); break;
            case 'files': _renderDdFiles(reqId, data, panel); break;
            default: panel.textContent = '';
        }
        return;
    }
    switch (tabName) {
        case 'workspace':
            // Already rendered by rfqOpenWorkspace in _loadDdSubTab
            return;
        case 'sourcing':
            // Parts list — full width, no sightings
            if (data && data.parts) {
                _ddReqCache[reqId] = data.parts;
            }
            _renderDrillDownTable(reqId, panel);
            break;
        case 'details': _renderDdDetails(reqId, panel); break;
        case 'parts':
            _renderSplitPartsOffers(reqId, data, panel);
            break;
        case 'sightings':
            if (data && !_ddSightingsCache[reqId]) _ddSightingsCache[reqId] = data;
            _renderSourcingDrillDown(reqId, panel);
            break;
        case 'activity':
            // Activity timeline only — tasks have their own tab now
            {
                const actData = (data && data.activity !== undefined) ? data.activity : data;
                _renderDdActivity(reqId, actData, panel);
                _autoPollReplies(reqId, actData, panel);
            }
            break;
        case 'offers': _renderDdOffers(reqId, data, panel); break;
        case 'quotes': _renderDdQuotes(reqId, data, panel); break;
        case 'tasks': _renderDdTasks(reqId, data, panel); break;
        case 'files': _renderDdFiles(reqId, data, panel); break;
        default: panel.textContent = '';
    }
}

function _renderDdActivity(reqId, data, panel) {
    const vendors = data.vendors || [];
    if (!vendors.length) {
        panel.innerHTML = `<div style="display:flex;align-items:center;gap:12px"><span style="font-size:11px;color:var(--muted)">No activity yet</span><button class="btn btn-ghost btn-sm" style="font-size:10px" onclick="event.stopPropagation();checkForReplies(${reqId},this)" title="Scan your inbox for vendor email replies to RFQs sent for this requisition">&#x21bb; Check for Replies</button><span style="font-size:10px;color:var(--muted);font-style:italic">Scans your inbox for vendor responses</span></div>`;
        return;
    }
    // Summary stats
    let totalContacts = 0, totalReplies = 0, totalCalls = 0, totalEmails = 0, totalNotes = 0;
    for (const v of vendors) {
        totalContacts += (v.contacts || []).length;
        totalReplies += (v.responses || []).length;
        for (const a of (v.activities || [])) {
            if (a.channel === 'phone') totalCalls++;
            else if (a.activity_type === 'note') totalNotes++;
            else if (a.channel === 'email') totalEmails++;
        }
    }
    const af = _ddActFilter[reqId] || 'all';
    let html = `<div style="display:flex;gap:16px;margin-bottom:8px;font-size:11px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:2;background:var(--bg2,var(--bg1));padding:6px 0">
        <span><b>${totalContacts}</b> RFQs sent</span>
        <span><b>${totalReplies}</b> replies</span>
        <span><b>${totalCalls}</b> calls</span>
        <span><b>${totalNotes}</b> notes</span>
        <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 8px" onclick="event.stopPropagation();checkForReplies(${reqId},this)" title="Scan your inbox for vendor email replies to RFQs sent for this requisition">&#x21bb; Check Inbox</button>
        <div class="fpills fpills-sm" style="margin-left:auto">
            <button class="fp fp-sm${af==='all'?' on':''}" onclick="event.stopPropagation();_ddActFilter[${reqId}]='all';_renderDdActivity(${reqId},_ddTabCache[${reqId}]?.activity,this.closest('.dd-panel'))">All</button>
            <button class="fp fp-sm${af==='email'?' on':''}" onclick="event.stopPropagation();_ddActFilter[${reqId}]='email';_renderDdActivity(${reqId},_ddTabCache[${reqId}]?.activity,this.closest('.dd-panel'))">✉ Email</button>
            <button class="fp fp-sm${af==='phone'?' on':''}" onclick="event.stopPropagation();_ddActFilter[${reqId}]='phone';_renderDdActivity(${reqId},_ddTabCache[${reqId}]?.activity,this.closest('.dd-panel'))">📞 Phone</button>
            <button class="fp fp-sm${af==='notes'?' on':''}" onclick="event.stopPropagation();_ddActFilter[${reqId}]='notes';_renderDdActivity(${reqId},_ddTabCache[${reqId}]?.activity,this.closest('.dd-panel'))">📝 Notes</button>
        </div>
    </div>`;
    // Apply filter
    let filteredVendors = vendors;
    if (af === 'email') filteredVendors = vendors.filter(v => (v.contacts||[]).some(c => c.contact_type === 'email') || (v.responses||[]).length);
    else if (af === 'phone') filteredVendors = vendors.filter(v => (v.activities||[]).some(a => a.channel === 'phone'));
    else if (af === 'notes') filteredVendors = vendors.filter(v => (v.activities||[]).some(a => a.activity_type === 'note'));
    let msgIdx = 0;
    html += '<div style="max-height:500px;overflow-y:auto">';
    for (const v of filteredVendors) {
        const contacts = v.contacts || [];
        const responses = v.responses || [];
        const activities = v.activities || [];
        const hasReply = responses.length > 0;
        const dotColor = hasReply ? 'var(--green)' : 'var(--amber)';
        html += `<div class="act-vendor-card">`;
        // Per-part status summary from parsed responses
        let partStatusHtml = '';
        const parsedResponses = responses.filter(r => r.parsed_data && r.parsed_data.parts && r.parsed_data.parts.length);
        if (parsedResponses.length) {
            const statusCounts = {};
            for (const r of parsedResponses) for (const p of r.parsed_data.parts) {
                const s = (p.status || 'unknown').replace('_', ' ');
                statusCounts[s] = (statusCounts[s] || 0) + 1;
            }
            const statusColors = {quoted:'var(--green)', 'no stock':'var(--red)', 'counter offer':'var(--amber)', 'follow up':'var(--amber)'};
            const pills = Object.entries(statusCounts).map(([s,c]) => {
                const clr = statusColors[s] || 'var(--muted)';
                return `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:${clr}15;color:${clr};text-transform:capitalize">${c} ${s}</span>`;
            }).join(' ');
            partStatusHtml = `<div style="display:flex;gap:4px;margin-top:2px;flex-wrap:wrap">${pills}</div>`;
        }
        html += `<div style="font-size:12px;font-weight:700;display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap"><span style="width:7px;height:7px;border-radius:50%;background:${dotColor};display:inline-block"></span>${esc(v.vendor_name)} <span style="font-weight:400;color:var(--muted);font-size:11px">${contacts.length} sent, ${responses.length} replied</span>${partStatusHtml}</div>`;
        // Build timeline with email bodies
        const timeline = [];
        for (const c of contacts) timeline.push({type:'sent', date: c.created_at, subject: c.subject || '', body: c.body || '', text: `${c.contact_type} to ${c.vendor_contact || 'vendor'}`, user: c.user_name, parts: c.parts_included || []});
        for (const r of responses) timeline.push({type:'reply', date: r.received_at, subject: r.subject || '', body: r.body || '', text: r.vendor_email || 'vendor', status: r.status, confidence: r.confidence, classification: r.classification, parsed_data: r.parsed_data, response_id: r.id, vendor_name: v.vendor_name});
        for (const a of activities) timeline.push({type:'activity', date: a.created_at, subject: '', body: a.notes || '', text: `${a.channel || a.activity_type}: ${a.notes || ''}`.trim(), user: a.user_name});
        timeline.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
        if (timeline.length) {
            html += '<div class="act-thread">';
            for (const t of timeline) {
                const ago = t.date ? fmtRelative(t.date) : '';
                const isSent = t.type === 'sent';
                const isReply = t.type === 'reply';
                const isActivity = t.type === 'activity';
                const icon = isSent ? '\u2709' : isReply ? '\u21a9\ufe0f' : '\u260e';
                const headerColor = isReply ? 'var(--green)' : 'var(--text2)';
                const mid = 'actMsg-' + reqId + '-' + (msgIdx++);
                const hasBody = !!(t.body || t.subject);
                // Confidence badge for parsed replies
                let confBadge = '';
                if (isReply && t.confidence != null) {
                    const pct = Math.round(t.confidence * 100);
                    const cc = pct >= 80 ? 'var(--green)' : pct >= 50 ? 'var(--amber)' : 'var(--red)';
                    const label = pct >= 80 ? '\u2713 High' : pct >= 50 ? '\u26a0 Review' : '\u26a0 Low';
                    confBadge = ` <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:${cc}20;color:${cc}" title="Parse confidence: ${pct}%">${label}</span>`;
                }
                // Classification badge
                let classBadge = '';
                if (isReply && t.classification && t.classification !== 'unknown') {
                    const classColors = {quote:'var(--green)',decline:'var(--red)',partial:'var(--amber)',info:'var(--blue)'};
                    const clc = classColors[t.classification] || 'var(--muted)';
                    classBadge = ` <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:${clc}20;color:${clc}">${t.classification}</span>`;
                }
                html += `<div class="act-msg${isReply ? ' act-msg-reply' : isSent ? ' act-msg-sent' : ''}">`;
                html += `<div class="act-msg-header" ${hasBody ? `onclick="document.getElementById('${mid}').classList.toggle('act-body-open')" style="cursor:pointer"` : ''}>`;
                html += `<span style="color:${headerColor}">${icon}</span> `;
                if (isSent) html += `<b style="color:${headerColor}">RFQ sent</b> to ${esc(t.text)}`;
                else if (isReply) html += `<b style="color:${headerColor}">Reply</b> from ${esc(t.text)}${confBadge}${classBadge}`;
                else html += `<span style="color:${headerColor}">${esc(t.text)}</span>`;
                html += ` <span class="act-msg-time">${ago}${t.user ? ' · ' + esc(t.user) : ''}</span>`;
                if (hasBody) html += ` <span class="act-expand-hint">\u25b6</span>`;
                html += `</div>`;
                if (hasBody) {
                    // Subject line + body preview, collapsed by default
                    let bodyHtml = '';
                    if (t.subject) bodyHtml += `<div style="font-weight:600;margin-bottom:4px">${esc(t.subject)}</div>`;
                    if (isSent && t.parts && t.parts.length) bodyHtml += `<div style="color:var(--muted);margin-bottom:4px">Parts: ${t.parts.map(p => esc(typeof p === 'object' ? (p.mpn || p.part_number || JSON.stringify(p)) : p)).join(', ')}</div>`;
                    // Show AI-parsed summary for replies
                    if (isReply && t.parsed_data) {
                        bodyHtml += _renderParsedSummary(t.parsed_data, reqId, t.response_id, t.vendor_name);
                    } else if (isReply && !t.parsed_data && t.response_id) {
                        bodyHtml += `<div style="margin-bottom:6px"><button class="btn btn-sm" style="font-size:10px;padding:2px 8px;background:var(--bg3);color:var(--teal);border:1px solid var(--teal)" onclick="event.stopPropagation();aiParseReply(${reqId},${t.response_id},'${escAttr(t.vendor_name||'')}',this)">Parse with AI</button></div>`;
                    }
                    bodyHtml += `<div class="act-body-text">${_formatEmailBody(t.body)}</div>`;
                    html += `<div class="act-body" id="${mid}">${bodyHtml}</div>`;
                }
                html += `</div>`;
            }
            html += '</div>';
        }
        html += '</div>';
    }
    html += '</div>';
    panel.innerHTML = html;
}

async function checkForReplies(reqId, btn) {
    const origText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '&#x21bb; Checking\u2026';
    try {
        await apiFetch(`/api/requisitions/${reqId}/poll`, { method: 'POST' });
        // Clear cached activity data so it re-fetches
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].activity;
        const panel = btn.closest('.dd-panel');
        if (panel) await _loadDdSubTab(reqId, 'activity', panel);
        showToast('Inbox checked for replies', 'info');
    } catch (e) {
        showToast('Couldn\'t check inbox — ' + friendlyError(e, 'please try again'), 'error', { action: { label: 'Retry', fn: () => checkForReplies() } });
    } finally {
        btn.disabled = false;
        btn.innerHTML = origText;
    }
}

function _renderParsedSummary(pd, reqId, responseId, vendorName) {
    if (!pd || typeof pd !== 'object') return '';
    const parts = pd.parts || [];
    const notes = pd.vendor_notes || '';
    if (!parts.length && !notes) return '';
    const quotedParts = parts.filter(p => p.status === 'quoted' && p.unit_price != null);
    let html = '<div class="parsed-summary">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><span style="font-size:10px;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.4px">AI-Parsed Response</span>';
    html += '<span style="display:flex;gap:4px">';
    if (reqId && responseId) {
        html += `<button class="btn btn-sm" style="font-size:10px;padding:2px 8px;background:var(--bg3);color:var(--teal);border:1px solid var(--teal)" onclick="event.stopPropagation();aiParseReply(${reqId},${responseId},'${escAttr(vendorName||'')}',this)" title="Re-parse email with AI">Re-parse</button>`;
    }
    if (quotedParts.length && reqId && responseId) {
        html += `<button class="btn btn-g btn-sm" style="font-size:10px;padding:2px 8px" onclick="event.stopPropagation();_acceptParsedOffers(${reqId},${responseId},this)" title="Create draft offers from parsed data">Accept ${quotedParts.length} Offer${quotedParts.length > 1 ? 's' : ''}</button>`;
    }
    html += '</span>';
    html += '</div>';
    if (parts.length) {
        html += '<table class="parsed-parts-tbl"><thead><tr><th>MPN</th><th>Status</th><th>Qty</th><th>Price</th><th>Lead Time</th><th>Condition</th><th>Date Code</th><th>MOQ</th><th>Notes</th></tr></thead><tbody>';
        for (const p of parts) {
            const statusColors = {quoted:'var(--green)', no_stock:'var(--red)', follow_up:'var(--amber)'};
            const sc = statusColors[p.status] || 'var(--muted)';
            const priceStr = p.unit_price != null ? `${p.currency || '$'}${parseFloat(p.unit_price).toFixed(4)}` : '\u2014';
            html += `<tr>
                <td class="mono" style="font-weight:600">${esc(p.mpn || '\u2014')}</td>
                <td><span style="color:${sc};font-weight:600;font-size:10px;text-transform:uppercase">${esc((p.status || '').replace('_', ' '))}</span></td>
                <td>${p.qty_available != null ? Number(p.qty_available).toLocaleString() : '\u2014'}</td>
                <td style="color:var(--teal);font-weight:600">${priceStr}</td>
                <td>${esc(p.lead_time || '\u2014')}</td>
                <td>${esc(p.condition || '\u2014')}</td>
                <td style="font-size:10px">${esc(p.date_code || '\u2014')}</td>
                <td>${p.moq != null ? Number(p.moq).toLocaleString() : '\u2014'}</td>
                <td style="font-size:10px">${esc(p.notes || '\u2014')}</td>
            </tr>`;
        }
        html += '</tbody></table>';
    }
    if (notes) {
        html += `<div style="font-size:11px;color:var(--text2);margin-top:4px;font-style:italic">${esc(notes)}</div>`;
    }
    html += '</div>';
    return html;
}

async function _acceptParsedOffers(reqId, responseId, btn) {
    btn.disabled = true;
    btn.textContent = 'Saving\u2026';
    try {
        // Get the activity data to find the parsed offers
        const actData = _ddTabCache[reqId]?.activity;
        if (!actData) throw new Error('Activity data not cached');
        let parsedOffers = [];
        for (const v of (actData.vendors || [])) {
            for (const r of (v.responses || [])) {
                if (r.id === responseId && r.parsed_data && r.parsed_data.parts) {
                    for (const p of r.parsed_data.parts) {
                        if (p.status === 'quoted' && p.unit_price != null) {
                            parsedOffers.push({
                                vendor_name: v.vendor_name,
                                mpn: p.mpn || '',
                                manufacturer: p.manufacturer || null,
                                qty_available: p.qty_available || null,
                                unit_price: p.unit_price,
                                currency: p.currency || 'USD',
                                lead_time: p.lead_time || null,
                                date_code: p.date_code || null,
                                condition: p.condition || null,
                                packaging: p.packaging || null,
                                moq: p.moq || null,
                                notes: p.notes || null,
                            });
                        }
                    }
                }
            }
        }
        if (!parsedOffers.length) { showToast('No quoted parts to save', 'warning'); return; }
        const result = await apiFetch('/api/ai/save-parsed-offers', {
            method: 'POST',
            body: { response_id: responseId, offers: parsedOffers, requisition_id: reqId }
        });
        showToast(`Created ${result.created} draft offer(s) — review in Offers tab`, 'success');
        btn.textContent = 'Saved';
        btn.style.background = 'var(--green)';
        // Refresh offers cache
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].offers;
        // Update list counts
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) {
            reqInfo.offer_count = (reqInfo.offer_count || 0) + result.created;
            reqInfo.has_new_offers = true;
        }
        renderReqList();
    } catch (e) {
        showToast('Couldn\'t save offers — ' + friendlyError(e, 'please try again'), 'error');
        btn.disabled = false;
        btn.textContent = 'Accept';
    }
}

const _autoPollTimestamps = {};  // reqId → last poll timestamp
async function _autoPollReplies(reqId, currentData, panel) {
    // Auto-poll inbox for replies when activity tab opens.
    // Throttle: at most once per 60 seconds per requisition.
    const now = Date.now();
    if (_autoPollTimestamps[reqId] && now - _autoPollTimestamps[reqId] < 60000) return;
    _autoPollTimestamps[reqId] = now;
    // Only poll if there are sent contacts (something to check replies for)
    const vendors = (currentData && currentData.vendors) || [];
    const hasSent = vendors.some(v => (v.contacts || []).length > 0);
    if (!hasSent) return;
    if (_pollAbort) try { _pollAbort.abort(); } catch(e){}
    _pollAbort = new AbortController();
    try {
        const result = await apiFetch(`/api/requisitions/${reqId}/poll`, { method: 'POST', signal: _pollAbort.signal });
        if (currentReqId !== reqId) return; // Stale — user navigated away
        const newCount = (result.responses || []).length;
        if (newCount > 0) {
            // New replies found — refresh activity tab
            if (_ddTabCache[reqId]) delete _ddTabCache[reqId].activity;
            const freshData = await apiFetch(`/api/requisitions/${reqId}/activity`, { signal: _pollAbort.signal });
            if (currentReqId !== reqId) return; // Stale check after second fetch
            if (_ddTabCache[reqId]) _ddTabCache[reqId].activity = freshData;
            _renderDdActivity(reqId, freshData, panel);
        }
    } catch (e) {
        // Silent — auto-poll failures and aborts shouldn't disrupt the UI
    }
}

function _formatEmailBody(text) {
    if (!text) return '';
    let cleaned = text;
    // If body is HTML (from Graph API), convert to plain text
    if (/<[a-z][\s\S]*>/i.test(cleaned)) {
        // Replace <br>, </p>, </div>, </tr>, </li> with newlines
        cleaned = cleaned.replace(/<br\s*\/?>/gi, '\n');
        cleaned = cleaned.replace(/<\/(?:p|div|tr|li|h[1-6])>/gi, '\n');
        // Remove <style> and <head> blocks entirely
        cleaned = cleaned.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
        cleaned = cleaned.replace(/<head[^>]*>[\s\S]*?<\/head>/gi, '');
        // Strip all remaining HTML tags
        cleaned = cleaned.replace(/<[^>]+>/g, ' ');
        // Decode common HTML entities
        cleaned = cleaned.replace(/&nbsp;/gi, ' ');
        cleaned = cleaned.replace(/&amp;/g, '&');
        cleaned = cleaned.replace(/&lt;/g, '<');
        cleaned = cleaned.replace(/&gt;/g, '>');
        cleaned = cleaned.replace(/&quot;/g, '"');
        cleaned = cleaned.replace(/&#39;/g, "'");
        cleaned = cleaned.replace(/&rsquo;/g, "\u2019");
        cleaned = cleaned.replace(/&ldquo;|&rdquo;/g, '"');
        cleaned = cleaned.replace(/&mdash;/g, '\u2014');
        cleaned = cleaned.replace(/&ndash;/g, '\u2013');
        cleaned = cleaned.replace(/&#\d+;/g, '');
    }
    // Remove quoted original message (common patterns)
    // "On Mon, Jan 1, 2026 at 10:00 AM Name <email> wrote:" and everything after
    cleaned = cleaned.replace(/\n\s*On\s+.{10,80}\s+wrote:\s*\n[\s\S]*/i, '');
    // "From: ... Sent: ... To: ... Subject: ..." block and everything after
    cleaned = cleaned.replace(/\n\s*-{2,}\s*(?:Original Message|Forwarded Message)\s*-{2,}\s*\n[\s\S]*/i, '');
    cleaned = cleaned.replace(/\n\s*From:\s+\S+.*\n\s*Sent:\s+.*\n[\s\S]*/i, '');
    cleaned = cleaned.replace(/\n\s*From:\s+\S+.*\n\s*Date:\s+.*\n[\s\S]*/i, '');
    // "> " quoted lines block at the end
    cleaned = cleaned.replace(/(\n\s*>.*){3,}[\s\S]*$/, '');
    // Remove email disclaimers / confidentiality notices
    cleaned = cleaned.replace(/\n\s*(?:This email and any attachments|Confidentiality notice|DISCLAIMER|This message is intended|This communication is confidential)[\s\S]*/i, '');
    // Remove common signature separators and everything after
    cleaned = cleaned.replace(/\n\s*-{2,}\s*\n(?:(?:Sent from|Get Outlook)[\s\S]*)?$/i, '');
    // Collapse excessive whitespace
    cleaned = cleaned.replace(/[^\S\n]+/g, ' ');  // horizontal whitespace
    cleaned = cleaned.replace(/\n{3,}/g, '\n\n'); // 3+ newlines → 2
    cleaned = cleaned.trim();
    if (!cleaned) return '<span style="color:var(--muted);font-style:italic">Empty reply</span>';
    // Escape for display and format
    let safe = esc(cleaned);
    safe = safe.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener" style="color:var(--teal)">$1</a>');
    safe = safe.replace(/\n/g, '<br>');
    return safe;
}

let _ddSelectedOffers = {};   // reqId → Set of offer IDs
let _ddQuoteData = {};        // reqId → quote object for in-memory editing
let _ddHistoryExpanded = {};  // "reqId-requirementId" → bool


// ---------------------------------------------------------------------------
// Knowledge Ledger: AI Insights Card (collapsible, top of parts tab)
// ---------------------------------------------------------------------------

async function _renderInsightsCard(reqId, container) {
    var collapsed = localStorage.getItem('insights_collapsed') === '1';
    var wrap = document.createElement('div');
    wrap.className = 'insights-card';
    wrap.id = 'insights-' + reqId;

    var header = document.createElement('div');
    header.className = 'insights-header';
    header.onclick = function() { _toggleInsightsCard(reqId); };

    var title = document.createElement('span');
    title.style.cssText = 'font-weight:600;font-size:12px';
    title.textContent = 'AI Insights';
    header.appendChild(title);

    var controls = document.createElement('span');
    controls.style.cssText = 'display:flex;gap:6px;align-items:center';

    var refreshBtn = document.createElement('button');
    refreshBtn.className = 'btn btn-ghost btn-sm';
    refreshBtn.style.fontSize = '10px';
    refreshBtn.textContent = '\u21bb Refresh';
    refreshBtn.title = 'Regenerate insights';
    refreshBtn.onclick = function(e) { e.stopPropagation(); _refreshInsights(reqId); };
    controls.appendChild(refreshBtn);

    var toggle = document.createElement('span');
    toggle.className = 'insights-toggle';
    toggle.textContent = collapsed ? '\u25b6' : '\u25bc';
    controls.appendChild(toggle);
    header.appendChild(controls);
    wrap.appendChild(header);

    var body = document.createElement('div');
    body.className = 'insights-body';
    body.style.display = collapsed ? 'none' : '';
    var loadingSpan = document.createElement('span');
    loadingSpan.style.cssText = 'font-size:11px;color:var(--muted)';
    loadingSpan.textContent = 'Loading\u2026';
    body.appendChild(loadingSpan);
    wrap.appendChild(body);

    container.prepend(wrap);

    try {
        var data = await apiFetch('/api/requisitions/' + reqId + '/insights');
        body.textContent = '';
        if (!data.insights || !data.insights.length) {
            var emptySpan = document.createElement('span');
            emptySpan.style.cssText = 'font-size:11px;color:var(--muted)';
            emptySpan.textContent = 'No insights yet. Click Refresh to generate.';
            body.appendChild(emptySpan);
            return;
        }
        for (var i = 0; i < data.insights.length; i++) {
            var ins = data.insights[i];
            var item = document.createElement('div');
            item.className = 'insight-item' + (ins.is_expired ? ' insight-expired' : '');
            var text = document.createElement('span');
            text.style.fontSize = '11px';
            text.textContent = ins.content;
            item.appendChild(text);
            if (ins.is_expired) {
                var badge = document.createElement('span');
                badge.style.cssText = 'font-size:9px;color:var(--amber);margin-left:4px';
                badge.textContent = '(may be outdated)';
                item.appendChild(badge);
            }
            body.appendChild(item);
        }
        if (data.has_expired) {
            var warn = document.createElement('div');
            warn.style.cssText = 'font-size:10px;color:var(--amber);margin-top:4px';
            warn.textContent = 'Some insights based on outdated data';
            body.appendChild(warn);
        }
    } catch (e) {
        body.textContent = '';
        var errSpan = document.createElement('span');
        errSpan.style.cssText = 'font-size:11px;color:var(--red)';
        errSpan.textContent = 'Failed to load insights';
        body.appendChild(errSpan);
    }
}

function _toggleInsightsCard(reqId) {
    var card = document.getElementById('insights-' + reqId);
    if (!card) return;
    var body = card.querySelector('.insights-body');
    var toggle = card.querySelector('.insights-toggle');
    var hidden = body.style.display === 'none';
    body.style.display = hidden ? '' : 'none';
    toggle.textContent = hidden ? '\u25bc' : '\u25b6';
    localStorage.setItem('insights_collapsed', hidden ? '0' : '1');
}

// Wrap a promise with a timeout (ms). Rejects with 'timeout' error if exceeded.
function _withTimeout(promise, ms) {
    return new Promise(function(resolve, reject) {
        var timer = setTimeout(function() {
            reject(new Error('timeout'));
        }, ms);
        promise.then(function(v) { clearTimeout(timer); resolve(v); },
                     function(e) { clearTimeout(timer); reject(e); });
    });
}

// Show a retry-able error message inside an insights body element.
function _showInsightsError(body, message, retryFn) {
    body.textContent = '';
    var errSpan = document.createElement('span');
    errSpan.style.cssText = 'font-size:11px;color:var(--red);cursor:pointer';
    errSpan.textContent = message;
    errSpan.title = 'Click to retry';
    if (retryFn) errSpan.onclick = retryFn;
    body.appendChild(errSpan);
}

var _INSIGHTS_TIMEOUT_MS = 30000;

async function _refreshInsights(reqId) {
    var card = document.getElementById('insights-' + reqId);
    if (!card) return;
    var body = card.querySelector('.insights-body');
    body.textContent = '';
    var loading = document.createElement('span');
    loading.style.cssText = 'font-size:11px;color:var(--muted)';
    loading.textContent = 'Generating\u2026';
    body.appendChild(loading);
    try {
        var data = await _withTimeout(
            apiFetch('/api/requisitions/' + reqId + '/insights/refresh', { method: 'POST' }),
            _INSIGHTS_TIMEOUT_MS
        );
        body.textContent = '';
        var insights = data.insights || [];
        for (var i = 0; i < insights.length; i++) {
            var item = document.createElement('div');
            item.className = 'insight-item';
            var text = document.createElement('span');
            text.style.fontSize = '11px';
            text.textContent = insights[i].content;
            item.appendChild(text);
            body.appendChild(item);
        }
        if (!insights.length) {
            var emptySpan = document.createElement('span');
            emptySpan.style.cssText = 'font-size:11px;color:var(--muted)';
            emptySpan.textContent = 'No insights generated.';
            body.appendChild(emptySpan);
        }
    } catch (e) {
        var msg = e.message === 'timeout'
            ? 'Summary unavailable \u2014 click to retry'
            : 'Could not generate summary \u2014 click to retry';
        _showInsightsError(body, msg, function() { _refreshInsights(reqId); });
    }
}

// ---------------------------------------------------------------------------
// Reusable Entity Insights Card (vendors, dashboard, materials)
// ---------------------------------------------------------------------------

async function _renderEntityInsightsCard(entityType, entityId, container, opts) {
    var title = (opts && opts.title) || 'AI Insights';
    var queryParam = (opts && opts.queryParam) || '';
    var storageKey = 'sprinkle_collapsed_' + entityType;
    var collapsed = localStorage.getItem(storageKey) === '1';

    var wrap = document.createElement('div');
    wrap.className = 'insights-card';
    wrap.id = 'sprinkle-' + entityType + '-' + entityId;

    var hdr = document.createElement('div');
    hdr.className = 'insights-header';
    hdr.onclick = function() {
        var b = wrap.querySelector('.insights-body');
        var t = wrap.querySelector('.insights-toggle');
        if (b.style.display === 'none') {
            b.style.display = '';
            t.textContent = '\u25bc';
            localStorage.removeItem(storageKey);
        } else {
            b.style.display = 'none';
            t.textContent = '\u25b6';
            localStorage.setItem(storageKey, '1');
        }
    };

    var titleSpan = document.createElement('span');
    titleSpan.style.cssText = 'font-weight:600;font-size:12px';
    titleSpan.textContent = title;
    hdr.appendChild(titleSpan);

    var controls = document.createElement('span');
    controls.style.cssText = 'display:flex;gap:6px;align-items:center';

    var refreshBtn = document.createElement('button');
    refreshBtn.className = 'btn btn-ghost btn-sm';
    refreshBtn.style.fontSize = '10px';
    refreshBtn.textContent = '\u21bb Refresh';
    refreshBtn.onclick = function(e) {
        e.stopPropagation();
        _refreshEntityInsights(entityType, entityId, queryParam);
    };
    controls.appendChild(refreshBtn);

    var toggle = document.createElement('span');
    toggle.className = 'insights-toggle';
    toggle.textContent = collapsed ? '\u25b6' : '\u25bc';
    controls.appendChild(toggle);
    hdr.appendChild(controls);
    wrap.appendChild(hdr);

    var body = document.createElement('div');
    body.className = 'insights-body';
    body.style.display = collapsed ? 'none' : '';
    var loading = document.createElement('span');
    loading.style.cssText = 'font-size:11px;color:var(--muted)';
    loading.textContent = 'Loading\u2026';
    body.appendChild(loading);
    wrap.appendChild(body);

    container.prepend(wrap);

    var url;
    if (entityType === 'materials') {
        url = '/api/materials/insights' + queryParam;
    } else {
        url = '/api/' + entityType + '/' + entityId + '/insights';
    }

    try {
        var data = await _withTimeout(apiFetch(url), _INSIGHTS_TIMEOUT_MS);
        _populateInsightsBody(body, data);
    } catch (e) {
        var msg = e.message === 'timeout'
            ? 'Summary unavailable \u2014 click to retry'
            : 'Could not generate summary \u2014 click to retry';
        _showInsightsError(body, msg, function() {
            _refreshEntityInsights(entityType, entityId, queryParam);
        });
    }
}

async function _refreshEntityInsights(entityType, entityId, queryParam) {
    var wrap = document.getElementById('sprinkle-' + entityType + '-' + entityId);
    if (!wrap) return;
    var body = wrap.querySelector('.insights-body');
    body.textContent = '';
    var loading = document.createElement('span');
    loading.style.cssText = 'font-size:11px;color:var(--muted)';
    loading.textContent = 'Regenerating\u2026';
    body.appendChild(loading);

    var url;
    if (entityType === 'materials') {
        url = '/api/materials/insights/refresh' + (queryParam || '');
    } else {
        url = '/api/' + entityType + '/' + entityId + '/insights/refresh';
    }

    try {
        var data = await _withTimeout(apiFetch(url, { method: 'POST' }), _INSIGHTS_TIMEOUT_MS);
        _populateInsightsBody(body, data);
    } catch (e) {
        var msg = e.message === 'timeout'
            ? 'Summary unavailable \u2014 click to retry'
            : 'Could not generate summary \u2014 click to retry';
        _showInsightsError(body, msg, function() {
            _refreshEntityInsights(entityType, entityId, queryParam);
        });
    }
}

function _populateInsightsBody(body, data) {
    body.textContent = '';
    if (!data.insights || !data.insights.length) {
        var empty = document.createElement('span');
        empty.style.cssText = 'font-size:11px;color:var(--muted)';
        empty.textContent = 'No insights yet. Click Refresh to generate.';
        body.appendChild(empty);
        return;
    }
    for (var i = 0; i < data.insights.length; i++) {
        var ins = data.insights[i];
        var item = document.createElement('div');
        item.className = 'insight-item' + (ins.is_expired ? ' insight-expired' : '');
        var text = document.createElement('span');
        text.style.fontSize = '11px';
        text.textContent = ins.content;
        item.appendChild(text);
        if (ins.is_expired) {
            var badge = document.createElement('span');
            badge.style.cssText = 'font-size:9px;color:var(--amber);margin-left:4px';
            badge.textContent = '(may be outdated)';
            item.appendChild(badge);
        }
        body.appendChild(item);
    }
    if (data.has_expired) {
        var warn = document.createElement('div');
        warn.style.cssText = 'font-size:10px;color:var(--amber);margin-top:4px';
        warn.textContent = 'Some insights based on outdated data';
        body.appendChild(warn);
    }
}

// ---------------------------------------------------------------------------
// Simple Task Checklist (flat list with checkboxes)
// ---------------------------------------------------------------------------

function _renderDdTasks(reqId, tasks, panel) {
    tasks = tasks || [];
    var pending = tasks.filter(function(t) { return t.status !== 'done'; });
    var done = tasks.filter(function(t) { return t.status === 'done'; });
    // Show pending first, then done at the bottom
    var sorted = pending.concat(done);

    // Header bar with count + add button
    var header = document.createElement('div');
    header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px';
    var label = document.createElement('span');
    label.style.cssText = 'font-size:11px;color:var(--muted)';
    label.textContent = pending.length + ' open' + (done.length ? ', ' + done.length + ' done' : '');
    header.appendChild(label);

    var addBtn = document.createElement('button');
    addBtn.className = 'btn btn-sm';
    addBtn.textContent = '+ Add Task';
    addBtn.onclick = function() { _showInlineTaskForm(reqId, panel); };
    header.appendChild(addBtn);

    panel.textContent = '';
    panel.appendChild(header);

    if (!sorted.length) {
        var empty = document.createElement('div');
        empty.style.cssText = 'font-size:12px;color:var(--muted);text-align:center;padding:20px 0';
        empty.textContent = 'No tasks yet. Click "+ Add Task" to create one.';
        panel.appendChild(empty);
        return;
    }

    // Task list container
    var list = document.createElement('div');
    list.className = 'task-checklist';
    list.id = 'taskList-' + reqId;
    for (var i = 0; i < sorted.length; i++) {
        list.appendChild(_renderTaskCheckItem(sorted[i], reqId));
    }
    panel.appendChild(list);
}

function _renderTaskCheckItem(task, reqId) {
    var isDone = task.status === 'done';
    var row = document.createElement('div');
    row.className = 'task-check-item' + (isDone ? ' task-done' : '');

    // Priority indicator
    var priColors = { 1: 'var(--green, #22c55e)', 2: 'var(--amber, #f59e0b)', 3: 'var(--red, #ef4444)' };
    row.style.borderLeftColor = priColors[task.priority] || priColors[2];

    // Checkbox
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = isDone;
    cb.className = 'task-checkbox';
    cb.onchange = function() { _toggleTaskStatus(reqId, task.id, cb.checked ? 'done' : 'todo'); };
    row.appendChild(cb);

    // Title
    var title = document.createElement('span');
    title.className = 'task-check-title';
    title.textContent = task.title;
    row.appendChild(title);

    // Due date
    if (task.due_at) {
        var dueSpan = document.createElement('span');
        dueSpan.className = 'task-check-due';
        var dueDate = new Date(task.due_at);
        if (dueDate < new Date() && !isDone) dueSpan.classList.add('task-overdue');
        dueSpan.textContent = _shortDate(task.due_at);
        row.appendChild(dueSpan);
    }

    // Delete button
    var delBtn = document.createElement('button');
    delBtn.className = 'btn btn-ghost btn-sm task-check-del';
    delBtn.textContent = '\u2715';
    delBtn.title = 'Delete';
    delBtn.onclick = function(e) { e.stopPropagation(); _deleteTask(reqId, task.id); };
    row.appendChild(delBtn);

    return row;
}

function _shortDate(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return months[d.getMonth()] + ' ' + d.getDate();
}

async function _toggleTaskStatus(reqId, taskId, newStatus) {
    try {
        await apiFetch('/api/requisitions/' + reqId + '/tasks/' + taskId + '/status', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: newStatus }),
        });
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].tasks;
        var drow = document.getElementById('d-' + reqId);
        var panel = drow ? drow.querySelector('.dd-panel') : null;
        if (panel) await _loadDdSubTab(reqId, 'tasks', panel);
        if (typeof showToast === 'function') showToast(newStatus === 'done' ? 'Task completed' : 'Task reopened', 'success');
    } catch (e) {
        if (typeof showToast === 'function') showToast('Failed to update task', 'error');
    }
}

function _deleteTask(reqId, taskId) {
    confirmAction('Delete Task', 'Are you sure you want to delete this task?', async function() {
        try {
            await apiFetch('/api/requisitions/' + reqId + '/tasks/' + taskId, { method: 'DELETE' });
            if (_ddTabCache[reqId]) delete _ddTabCache[reqId].tasks;
            var drow = document.getElementById('d-' + reqId);
            var panel = drow ? drow.querySelector('.dd-panel') : null;
            if (panel) await _loadDdSubTab(reqId, 'tasks', panel);
            if (typeof showToast === 'function') showToast('Task deleted', 'success');
        } catch (e) {
            if (typeof showToast === 'function') showToast('Failed to delete task', 'error');
        }
    }, { confirmLabel: 'Delete', danger: true });
}

function _showInlineTaskForm(reqId, panel) {
    if (document.getElementById('taskForm-' + reqId)) return;

    var form = document.createElement('div');
    form.id = 'taskForm-' + reqId;
    form.className = 'task-inline-form';

    form.innerHTML =
        '<input id="taskTitle-' + reqId + '" type="text" placeholder="Task title..." class="task-input">' +
        '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' +
            '<select id="taskType-' + reqId + '" class="task-select">' +
                '<option value="sourcing">Sourcing</option>' +
                '<option value="sales">Sales</option>' +
                '<option value="general">General</option>' +
            '</select>' +
            '<select id="taskPriority-' + reqId + '" class="task-select">' +
                '<option value="1">Low</option>' +
                '<option value="2" selected>Medium</option>' +
                '<option value="3">High</option>' +
            '</select>' +
            '<input id="taskDue-' + reqId + '" type="date" class="task-select" style="width:auto">' +
            '<button class="btn btn-sm" onclick="_submitNewTask(' + reqId + ')">Add</button>' +
            '<button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'taskForm-' + reqId + '\').remove()">Cancel</button>' +
        '</div>';

    var list = document.getElementById('taskList-' + reqId);
    if (list) panel.insertBefore(form, list);
    else panel.appendChild(form);
    document.getElementById('taskTitle-' + reqId).focus();
}

async function _submitNewTask(reqId) {
    var titleEl = document.getElementById('taskTitle-' + reqId);
    var typeEl = document.getElementById('taskType-' + reqId);
    var priEl = document.getElementById('taskPriority-' + reqId);
    var dueEl = document.getElementById('taskDue-' + reqId);
    if (!titleEl || !titleEl.value.trim()) return;

    var body = {
        title: titleEl.value.trim(),
        task_type: typeEl.value,
        priority: parseInt(priEl.value),
    };
    if (dueEl.value) body.due_at = new Date(dueEl.value).toISOString();

    try {
        await apiFetch('/api/requisitions/' + reqId + '/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].tasks;
        var drow = document.getElementById('d-' + reqId);
        var panel = drow ? drow.querySelector('.dd-panel') : null;
        if (panel) await _loadDdSubTab(reqId, 'tasks', panel);
        if (typeof showToast === 'function') showToast('Task created', 'success');
    } catch (e) {
        if (typeof showToast === 'function') showToast('Failed to create task', 'error');
    }
}

// ---------------------------------------------------------------------------
// My Tasks Sidebar Widget
// ---------------------------------------------------------------------------

window.toggleMyTasksSidebar = toggleMyTasksSidebar;
function toggleMyTasksSidebar() {
    var sidebar = document.getElementById('myTasksSidebar');
    if (!sidebar) return;
    var isOpen = sidebar.classList.toggle('open');
    document.body.classList.toggle('tasks-open', isOpen);
    if (isOpen) loadMyTasks();
}

async function loadMyTasks() {
    var list = document.getElementById('myTasksList');
    if (!list) return;
    list.innerHTML = '<span style="font-size:11px;color:var(--muted);padding:20px;text-align:center;display:block">Loading tasks...</span>';
    try {
        var [tasksRes, summaryRes] = await Promise.allSettled([
            apiFetch('/api/tasks/mine'),
            apiFetch('/api/tasks/mine/summary')
        ]);
        var tasks = (tasksRes.status === 'fulfilled' && Array.isArray(tasksRes.value)) ? tasksRes.value : [];
        // Filter out completed auto-generated tasks (noise)
        tasks = tasks.filter(function(t) { return !(t.source === 'auto' && t.status === 'done'); });
        var summary = (summaryRes.status === 'fulfilled' && summaryRes.value && typeof summaryRes.value === 'object') ? summaryRes.value : {};

        // Update badge
        var badge = document.getElementById('myTasksBadge');
        var pending = (summary.todo || 0) + (summary.in_progress || 0);
        if (badge) {
            badge.textContent = pending;
            badge.style.display = pending > 0 ? 'flex' : 'none';
        }

        if (!tasks.length) {
            list.innerHTML = '<span style="font-size:11px;color:var(--muted);padding:20px;text-align:center;display:block">No tasks assigned to you</span>';
            return;
        }

        // Sort: tasks with due dates first (earliest first), then no-date tasks
        tasks.sort(function(a, b) {
            if (!a.due_at && !b.due_at) return 0;
            if (!a.due_at) return 1;
            if (!b.due_at) return -1;
            return new Date(a.due_at) - new Date(b.due_at);
        });

        list.innerHTML = '';
        for (var i = 0; i < tasks.length; i++) {
            list.appendChild(_renderMyTaskItem(tasks[i]));
        }
    } catch (e) {
        list.innerHTML = '<span style="font-size:11px;color:var(--red);padding:20px;text-align:center;display:block">Failed to load tasks</span>';
    }
}

function _renderMyTaskItem(task) {
    var isDone = task.status === 'done';
    var item = document.createElement('div');
    item.className = 'my-task-item' + (isDone ? ' task-done' : '');
    if (task.priority === 3) item.classList.add('pri-high');
    else if (task.priority === 2) item.classList.add('pri-med');
    else item.classList.add('pri-low');

    // Checkbox to toggle done
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = isDone;
    cb.className = 'task-checkbox';
    cb.onclick = function(e) { e.stopPropagation(); };
    cb.onchange = function() {
        var newStatus = cb.checked ? 'done' : 'todo';
        apiFetch('/api/requisitions/' + task.requisition_id + '/tasks/' + task.id + '/status', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: newStatus }),
        }).then(function() {
            loadMyTasks();
            if (typeof showToast === 'function') showToast(newStatus === 'done' ? 'Task completed' : 'Task reopened', 'success');
        }).catch(function() {
            cb.checked = !cb.checked;
            if (typeof showToast === 'function') showToast('Failed to update task', 'error');
        });
    };
    item.appendChild(cb);

    // Content wrapper (clickable to navigate)
    var content = document.createElement('div');
    content.style.cssText = 'flex:1;min-width:0;cursor:pointer';
    content.onclick = function() {
        toggleMyTasksSidebar();
        expandToSubTab(task.requisition_id, 'tasks');
    };

    var title = document.createElement('div');
    title.className = 'my-task-item-title';
    title.textContent = task.title;
    content.appendChild(title);

    var meta = document.createElement('div');
    meta.className = 'my-task-item-meta';
    var reqName = document.createElement('span');
    reqName.className = 'my-task-item-req';
    reqName.textContent = task.requisition_name || 'Req #' + task.requisition_id;
    meta.appendChild(reqName);
    if (task.due_at) {
        var dateSpan = document.createElement('span');
        dateSpan.className = 'task-check-due';
        var dueDate = new Date(task.due_at);
        if (dueDate < new Date() && !isDone) dateSpan.classList.add('task-overdue');
        dateSpan.textContent = _shortDate(task.due_at);
        meta.appendChild(dateSpan);
    }
    content.appendChild(meta);
    item.appendChild(content);

    return item;
}

// Load badge count on page load — sidebar stays closed until user clicks toggle
(function() {
    setTimeout(function() {
        var sidebar = document.getElementById('myTasksSidebar');
        if (!sidebar) return;
        // Ensure closed and clear stale preference
        sidebar.classList.remove('open');
        document.body.classList.remove('tasks-open');
        try { localStorage.removeItem('myTasksOpen'); } catch(e) {}
        // Load badge count only
        (async function() {
            try {
                var summary = await apiFetch('/api/tasks/mine/summary');
                var badge = document.getElementById('myTasksBadge');
                var pending = (summary.todo || 0) + (summary.in_progress || 0);
                if (badge) {
                    badge.textContent = pending;
                    badge.style.display = pending > 0 ? 'flex' : 'none';
                }
            } catch(e) { /* silently fail */ }
        })();
    }, 500);
})();

// ---------------------------------------------------------------------------
// Strategic Vendors Panel
// ---------------------------------------------------------------------------

function showStrategicVendors() {
    showView('view-strategic');
    loadStrategicVendors();
}

async function loadStrategicVendors() {
    var list = document.getElementById('strategicVendorList');
    if (!list) return;
    list.innerHTML = '<span style="font-size:12px;color:var(--muted);padding:20px;text-align:center;display:block">Loading...</span>';
    try {
        var data = await apiFetch('/api/strategic-vendors/mine');
        var slots = document.getElementById('strategicSlots');
        var badge = document.getElementById('strategicBadge');
        var claimBtn = document.getElementById('strategicClaimBtn');
        if (slots) slots.textContent = data.count + '/' + data.max + ' slots';
        if (badge) {
            badge.textContent = data.count;
            badge.style.display = data.count > 0 ? 'inline-flex' : 'none';
        }
        if (claimBtn) claimBtn.disabled = data.slots_remaining <= 0;

        if (!data.vendors.length) {
            list.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)"><p style="font-size:14px;margin-bottom:8px">No strategic vendors claimed yet</p><p style="font-size:12px">Claim up to 10 vendors to track their response activity</p></div>';
            return;
        }

        list.innerHTML = '';
        for (var i = 0; i < data.vendors.length; i++) {
            list.appendChild(_renderStrategicCard(data.vendors[i]));
        }
    } catch (e) {
        list.innerHTML = '<span style="font-size:12px;color:var(--red);padding:20px;text-align:center;display:block">Failed to load strategic vendors</span>';
    }
}

function _renderStrategicCard(sv) {
    var card = document.createElement('div');
    card.className = 'strategic-card';
    var isUrgent = sv.days_remaining <= 7;
    var daysClass = isUrgent ? 'strategic-days-urgent' : 'strategic-days-ok';

    card.innerHTML =
        '<div class="strategic-card-main">' +
            '<div class="strategic-card-name" onclick="openVendorDrawer(' + sv.vendor_card_id + ')" style="cursor:pointer;color:var(--blue)">' + esc(sv.vendor_name || 'Unknown') + '</div>' +
            '<div class="strategic-card-meta">' +
                '<span class="' + daysClass + '">' + sv.days_remaining + 'd left</span>' +
                (sv.vendor_score != null ? ' <span style="color:var(--muted);font-size:11px">Score: ' + Math.round(sv.vendor_score) + '</span>' : '') +
                (sv.last_offer_at ? ' <span style="color:var(--muted);font-size:11px">Last offer: ' + _shortDate(sv.last_offer_at) + '</span>' : ' <span style="color:var(--muted);font-size:11px">No offers yet</span>') +
            '</div>' +
        '</div>' +
        '<button class="btn btn-ghost btn-sm strategic-drop-btn" onclick="dropStrategicVendor(' + sv.vendor_card_id + ',\'' + esc(sv.vendor_name || '') + '\')">Drop</button>';

    // Progress bar for TTL
    var progress = document.createElement('div');
    progress.className = 'strategic-progress';
    var pct = Math.min(100, Math.max(0, (sv.days_remaining / 39) * 100));
    progress.innerHTML = '<div class="strategic-progress-bar" style="width:' + pct + '%;background:' + (isUrgent ? 'var(--red)' : 'var(--blue)') + '"></div>';
    card.appendChild(progress);

    return card;
}

function dropStrategicVendor(vendorCardId, vendorName) {
    confirmAction('Drop Vendor', 'Drop ' + vendorName + ' from your strategic list? They\u2019ll return to the open pool.', async function() {
        try {
            await apiFetch('/api/strategic-vendors/drop/' + vendorCardId, { method: 'DELETE' });
            showToast(vendorName + ' dropped', 'success');
            loadStrategicVendors();
        } catch (e) {
            showToast('Failed to drop vendor', 'error');
        }
    });
}

function openStrategicClaimModal() {
    var modal = document.getElementById('strategicClaimModal');
    if (modal) modal.style.display = 'flex';
    var input = document.getElementById('strategicClaimSearch');
    if (input) { input.value = ''; input.focus(); }
    searchOpenPoolVendors();
}

function closeStrategicClaimModal() {
    var modal = document.getElementById('strategicClaimModal');
    if (modal) modal.style.display = 'none';
}

var _strategicSearchDebounce = null;
function searchOpenPoolVendors() {
    clearTimeout(_strategicSearchDebounce);
    _strategicSearchDebounce = setTimeout(async function() {
        var input = document.getElementById('strategicClaimSearch');
        var results = document.getElementById('strategicClaimResults');
        if (!results) return;
        var q = input ? input.value.trim() : '';
        results.innerHTML = '<span style="font-size:12px;color:var(--muted);display:block;padding:12px">Searching...</span>';
        try {
            var params = '?limit=20';
            if (q) params += '&search=' + encodeURIComponent(q);
            var data = await apiFetch('/api/strategic-vendors/open-pool' + params);
            if (!data.vendors.length) {
                results.innerHTML = '<span style="font-size:12px;color:var(--muted);display:block;padding:12px">No vendors found</span>';
                return;
            }
            results.innerHTML = '';
            for (var i = 0; i < data.vendors.length; i++) {
                var v = data.vendors[i];
                var row = document.createElement('div');
                row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer';
                row.innerHTML = '<span style="font-size:13px">' + esc(v.display_name) + (v.vendor_score != null ? ' <small style="color:var(--muted)">(score: ' + Math.round(v.vendor_score) + ')</small>' : '') + '</span>' +
                    '<button class="btn btn-primary btn-sm" onclick="claimStrategicVendor(' + v.id + ',\'' + esc(v.display_name).replace(/'/g, "\\'") + '\')">Claim</button>';
                results.appendChild(row);
            }
        } catch (e) {
            results.innerHTML = '<span style="font-size:12px;color:var(--red);display:block;padding:12px">Search failed</span>';
        }
    }, 300);
}

async function claimStrategicVendor(vendorCardId, vendorName) {
    try {
        await apiFetch('/api/strategic-vendors/claim/' + vendorCardId, { method: 'POST' });
        showToast(vendorName + ' claimed as strategic vendor', 'success');
        closeStrategicClaimModal();
        loadStrategicVendors();
    } catch (e) {
        var msg = 'Failed to claim vendor';
        try { msg = (await e.json ? e.json() : e).detail || msg; } catch(ex) {}
        showToast(msg, 'error');
    }
}

// ---------------------------------------------------------------------------
// Knowledge Ledger: Q&A Tab (legacy — kept for backward compat)
// ---------------------------------------------------------------------------

function _renderDdQA(reqId, entries, panel) {
    var filterBar = document.createElement('div');
    filterBar.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px';

    var filterGroup = document.createElement('div');
    filterGroup.style.display = 'flex';
    filterGroup.style.gap = '4px';
    var filters = ['all', 'question', 'note', 'fact'];
    var filterLabels = {all: 'All', question: 'Questions', note: 'Notes', fact: 'Facts'};
    for (var fi = 0; fi < filters.length; fi++) {
        var fbtn = document.createElement('button');
        fbtn.className = 'btn btn-ghost btn-sm qa-filter' + (filters[fi] === 'all' ? ' active' : '');
        fbtn.textContent = filterLabels[filters[fi]];
        fbtn.dataset.filter = filters[fi];
        fbtn.onclick = (function(f, b) { return function() { _filterQA(reqId, f, b); }; })(filters[fi], fbtn);
        filterGroup.appendChild(fbtn);
    }
    filterBar.appendChild(filterGroup);

    var askBtn = document.createElement('button');
    askBtn.className = 'btn btn-sm';
    askBtn.textContent = 'Ask Question';
    askBtn.onclick = function() { _openAskQuestionModal(reqId); };
    filterBar.appendChild(askBtn);

    panel.textContent = '';
    panel.appendChild(filterBar);

    if (!entries || !entries.length) {
        var emptyDiv = document.createElement('div');
        emptyDiv.style.cssText = 'font-size:11px;color:var(--muted);padding:20px 0;text-align:center';
        emptyDiv.textContent = 'No knowledge entries yet. Ask a question or add a note.';
        panel.appendChild(emptyDiv);
        return;
    }

    var list = document.createElement('div');
    list.id = 'qa-list-' + reqId;
    for (var j = 0; j < entries.length; j++) {
        list.appendChild(_renderQAEntry(entries[j], reqId));
    }
    panel.appendChild(list);
}

function _renderQAEntry(e, reqId) {
    var wrapper = document.createElement('div');
    wrapper.className = 'qa-entry' + (e.source === 'system' ? ' qa-auto' : '');
    wrapper.dataset.type = e.entry_type;
    if (e.is_expired) wrapper.style.opacity = '0.6';

    var topRow = document.createElement('div');
    topRow.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start';

    var contentDiv = document.createElement('div');
    var icon = '';
    if (e.entry_type === 'question') icon = '\u2753 ';
    else if (e.entry_type === 'fact') icon = '\ud83d\udcca ';
    else if (e.entry_type === 'note') icon = '\ud83d\udcdd ';

    var contentSpan = document.createElement('span');
    contentSpan.style.cssText = 'font-size:12px;font-weight:600';
    contentSpan.textContent = icon + e.content;
    contentDiv.appendChild(contentSpan);

    if (e.is_expired) {
        var expBadge = document.createElement('span');
        expBadge.style.cssText = 'font-size:9px;color:var(--amber);margin-left:4px';
        expBadge.textContent = '(may be outdated)';
        contentDiv.appendChild(expBadge);
    }
    topRow.appendChild(contentDiv);

    var badgeArea = document.createElement('span');
    if (e.entry_type === 'question') {
        var statusBadge = document.createElement('span');
        statusBadge.className = 'qa-badge ' + (e.is_resolved ? 'qa-resolved' : 'qa-pending');
        statusBadge.textContent = e.is_resolved ? 'Resolved' : 'Awaiting answer';
        badgeArea.appendChild(statusBadge);
    }
    if (e.source === 'system') {
        var autoBadge = document.createElement('span');
        autoBadge.className = 'qa-badge qa-auto';
        autoBadge.textContent = 'auto';
        autoBadge.style.marginLeft = '4px';
        badgeArea.appendChild(autoBadge);
    }
    topRow.appendChild(badgeArea);
    wrapper.appendChild(topRow);

    var meta = document.createElement('div');
    meta.style.cssText = 'font-size:10px;color:var(--muted);margin-top:2px';
    meta.textContent = (e.creator_name || 'System') + ' \u00b7 ' + _timeAgo(e.created_at);
    wrapper.appendChild(meta);

    if (e.answers && e.answers.length) {
        for (var k = 0; k < e.answers.length; k++) {
            var a = e.answers[k];
            var ansDiv = document.createElement('div');
            ansDiv.className = 'qa-answer';
            var ansText = document.createElement('span');
            ansText.style.fontSize = '11px';
            ansText.textContent = a.content;
            ansDiv.appendChild(ansText);
            var ansMeta = document.createElement('div');
            ansMeta.style.cssText = 'font-size:10px;color:var(--muted);margin-top:2px';
            ansMeta.textContent = (a.creator_name || 'Unknown') + ' \u00b7 ' + _timeAgo(a.created_at);
            ansDiv.appendChild(ansMeta);
            wrapper.appendChild(ansDiv);
        }
    }

    if (e.entry_type === 'question' && !e.is_resolved) {
        var ansRow = document.createElement('div');
        ansRow.style.marginTop = '4px';
        var ansBtn = document.createElement('button');
        ansBtn.className = 'btn btn-ghost btn-sm';
        ansBtn.style.fontSize = '10px';
        ansBtn.textContent = 'Answer';
        ansBtn.onclick = (function(rId, eId) { return function() { _openAnswerModal(rId, eId); }; })(reqId, e.id);
        ansRow.appendChild(ansBtn);
        wrapper.appendChild(ansRow);
    }

    return wrapper;
}

function _filterQA(reqId, type, btn) {
    var list = document.getElementById('qa-list-' + reqId);
    if (!list) return;
    var entries = list.querySelectorAll('.qa-entry');
    for (var i = 0; i < entries.length; i++) {
        entries[i].style.display = (type === 'all' || entries[i].dataset.type === type) ? '' : 'none';
    }
    var allBtns = btn.parentNode.querySelectorAll('.qa-filter');
    for (var j = 0; j < allBtns.length; j++) allBtns[j].classList.remove('active');
    btn.classList.add('active');
}

// ---------------------------------------------------------------------------
// Q&A Modals: Ask Question + Answer
// ---------------------------------------------------------------------------

async function _openAskQuestionModal(reqId) {
    var buyers = [];
    try {
        var users = await apiFetch('/api/users');
        buyers = (users || []).filter(function(u) { return u.role === 'buyer' || u.role === 'admin'; });
    } catch (e) { /* fallback: empty list */ }

    var quota = { used: 0, limit: 10, remaining: 10, allowed: true };
    try {
        quota = await apiFetch('/api/knowledge/quota');
    } catch (e) { /* fallback: no limit shown */ }

    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'askQuestionModal';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.style.maxWidth = '480px';

    var h3 = document.createElement('h3');
    h3.style.cssText = 'margin:0 0 12px';
    h3.textContent = 'Ask a Question';
    box.appendChild(h3);

    var ta = document.createElement('textarea');
    ta.id = 'qaQuestionText';
    ta.rows = 4;
    ta.style.cssText = 'width:100%;resize:vertical;font-size:12px';
    ta.placeholder = 'Type your question...';
    box.appendChild(ta);

    var selectWrap = document.createElement('div');
    selectWrap.style.marginTop = '8px';
    var label = document.createElement('label');
    label.style.cssText = 'font-size:11px;font-weight:600';
    label.textContent = 'Assign to buyers:';
    selectWrap.appendChild(label);
    var sel = document.createElement('select');
    sel.id = 'qaAssignBuyers';
    sel.multiple = true;
    sel.style.cssText = 'width:100%;height:80px;font-size:11px';
    for (var i = 0; i < buyers.length; i++) {
        var opt = document.createElement('option');
        opt.value = buyers[i].id;
        opt.textContent = buyers[i].display_name || buyers[i].email;
        sel.appendChild(opt);
    }
    selectWrap.appendChild(sel);
    var hint = document.createElement('span');
    hint.style.cssText = 'font-size:9px;color:var(--muted)';
    hint.textContent = 'Hold Ctrl/Cmd to select multiple';
    selectWrap.appendChild(hint);
    box.appendChild(selectWrap);

    var quotaDiv = document.createElement('div');
    quotaDiv.style.cssText = 'margin-top:8px;font-size:11px;color:var(--muted)';
    if (quota.allowed) {
        quotaDiv.textContent = quota.remaining + '/' + quota.limit + ' questions remaining today';
    } else {
        quotaDiv.textContent = 'Daily question limit reached (' + quota.limit + '/' + quota.limit + '). Try again tomorrow.';
        quotaDiv.style.color = 'var(--danger, #e74c3c)';
    }
    box.appendChild(quotaDiv);

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:12px';
    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = function() { document.getElementById('askQuestionModal').remove(); };
    btnRow.appendChild(cancelBtn);
    var submitBtn = document.createElement('button');
    submitBtn.className = 'btn';
    submitBtn.textContent = 'Post Question';
    submitBtn.onclick = function() { _submitQuestion(reqId); };
    if (!quota.allowed) {
        submitBtn.disabled = true;
        submitBtn.style.opacity = '0.5';
    }
    btnRow.appendChild(submitBtn);
    box.appendChild(btnRow);

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    ta.focus();
}

async function _submitQuestion(reqId) {
    var text = document.getElementById('qaQuestionText');
    var sel = document.getElementById('qaAssignBuyers');
    if (!text || !text.value.trim()) return;
    var buyerIds = Array.from(sel.selectedOptions).map(function(o) { return parseInt(o.value); });
    if (!buyerIds.length) { alert('Select at least one buyer'); return; }

    try {
        await apiFetch('/api/knowledge/question', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                content: text.value.trim(),
                assigned_to_ids: buyerIds,
                requisition_id: reqId,
            }),
        });
        document.getElementById('askQuestionModal').remove();
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].qa;
        var drow = document.getElementById('d-' + reqId);
        var panel = drow ? drow.querySelector('.dd-panel') : null;
        if (panel) await _loadDdSubTab(reqId, 'qa', panel);
    } catch (e) {
        alert('Failed to post question');
    }
}

async function _openAnswerModal(reqId, entryId) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'answerModal';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.style.maxWidth = '480px';

    var h3 = document.createElement('h3');
    h3.style.cssText = 'margin:0 0 12px';
    h3.textContent = 'Post Answer';
    box.appendChild(h3);

    var ta = document.createElement('textarea');
    ta.id = 'qaAnswerText';
    ta.rows = 4;
    ta.style.cssText = 'width:100%;resize:vertical;font-size:12px';
    ta.placeholder = 'Type your answer...';
    box.appendChild(ta);

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:12px';
    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = function() { document.getElementById('answerModal').remove(); };
    btnRow.appendChild(cancelBtn);
    var submitBtn = document.createElement('button');
    submitBtn.className = 'btn';
    submitBtn.textContent = 'Post Answer';
    submitBtn.onclick = function() { _submitAnswer(reqId, entryId); };
    btnRow.appendChild(submitBtn);
    box.appendChild(btnRow);

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    ta.focus();
}

async function _submitAnswer(reqId, entryId) {
    var text = document.getElementById('qaAnswerText');
    if (!text || !text.value.trim()) return;

    try {
        await apiFetch('/api/knowledge/' + entryId + '/answer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: text.value.trim() }),
        });
        document.getElementById('answerModal').remove();
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].qa;
        var drow = document.getElementById('d-' + reqId);
        var panel = drow ? drow.querySelector('.dd-panel') : null;
        if (panel) await _loadDdSubTab(reqId, 'qa', panel);
    } catch (e) {
        alert('Failed to post answer');
    }
}

function _renderDdOffers(reqId, data, panel) {
    const groups = data.groups || data || [];
    // Count total offers and pending
    let totalOffers = 0, pendingCount = 0;
    if (Array.isArray(groups)) {
        for (const g of groups) {
            totalOffers += (g.offers || []).length;
            pendingCount += (g.offers || []).filter(o => o.status === 'pending_review').length;
        }
    }
    if (!totalOffers) { panel.innerHTML = '<span style="font-size:11px;color:var(--muted)">No offers yet — use <b>+ Log Offer</b> above to record a vendor offer, or send RFQs from the <b>Sightings</b> tab to request quotes</span>'; return; }
    if (!_ddSelectedOffers[reqId]) _ddSelectedOffers[reqId] = new Set();
    const sel = _ddSelectedOffers[reqId];

    // Summary bar + prominent Build Quote CTA
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:6px">
        <span style="font-size:11px"><b>${totalOffers}</b> offer${totalOffers !== 1 ? 's' : ''}${sel.size > 0 ? ` &middot; <b>${sel.size}</b> selected` : ''}${pendingCount > 0 ? ` &middot; <span class="badge" style="background:var(--amber-light);color:var(--amber);font-size:9px">${pendingCount} pending review</span>` : ''}</span>
        <span style="display:flex;gap:6px;align-items:center">`;
    if (sel.size === 0) {
        html += `<span style="font-size:11px;color:var(--muted);margin-right:4px">Select offers to quote &rarr;</span>`;
    }
    html += `<button class="btn btn-primary" id="ddBuildQuoteBtn-${reqId}" ${sel.size === 0 ? 'disabled style="opacity:.4;pointer-events:none;font-size:13px;padding:6px 16px"' : 'style="font-size:13px;padding:6px 16px;font-weight:700;box-shadow:0 2px 8px rgba(14,116,144,.3);animation:bqPulse 2s ease-in-out infinite"'} onclick="event.stopPropagation();ddBuildQuote(${reqId})">Build Quote${sel.size > 0 ? ` (${sel.size})` : ''}</button>
        </span>
    </div>`;
    // Sticky bottom bar when offers are selected
    if (sel.size > 0) {
        html += `<div id="ddBuildQuoteBar-${reqId}" style="position:sticky;bottom:0;z-index:10;background:var(--bg1);border-top:2px solid var(--teal);padding:8px 12px;margin:8px -8px -8px;display:flex;justify-content:space-between;align-items:center;border-radius:0 0 8px 8px">
            <span style="font-size:12px;font-weight:600"><b>${sel.size}</b> offer${sel.size !== 1 ? 's' : ''} selected</span>
            <button class="btn btn-primary" style="font-size:14px;padding:8px 24px;font-weight:700;box-shadow:0 2px 8px rgba(14,116,144,.3)" onclick="event.stopPropagation();ddBuildQuote(${reqId})">Build Quote &rarr;</button>
        </div>`;
    }

    // Grouped layout
    const grpArr = Array.isArray(groups) ? groups : [];
    grpArr.forEach((g, gi) => {
        // Sort: unquoted first, then by price
        const offers = (g.offers || []).slice().sort((a, b) => {
            if (a.quoted_on && !b.quoted_on) return 1;
            if (!a.quoted_on && b.quoted_on) return -1;
            return (a.unit_price || 999999) - (b.unit_price || 999999);
        });
        if (!offers.length) return;
        const reqMpn = g.mpn || g.label || '';
        const targetPrice = g.target_price != null ? '$' + Number(g.target_price).toFixed(4) : '';
        const lastQ = g.last_quoted != null ? '$' + Number(g.last_quoted).toFixed(4) : '';

        // Count selected within this group
        const groupIds = offers.map(o => o.id || o.offer_id);
        const groupSelCount = groupIds.filter(id => sel.has(id)).length;

        html += `<div class="offer-group">`;
        html += `<div class="offer-group-header">
            <strong>${esc(reqMpn)}</strong>
            <span>need ${(g.target_qty || 0).toLocaleString()}</span>
            ${targetPrice ? '<span>target ' + targetPrice + '</span>' : ''}
            ${lastQ ? '<span>last: ' + lastQ + '</span>' : ''}
        </div>`;
        html += `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">`;
        html += `<table class="dtbl"><thead><tr>
            <th style="width:28px"><input type="checkbox" onchange="ddToggleGroupOffers(${reqId},${gi},this.checked)" ${groupSelCount === offers.length ? 'checked' : ''}></th>
            <th>Vendor</th><th>MPN</th><th>Mfr</th><th>Qty</th><th>Price</th><th>Lead</th><th>Cond</th><th>DC</th><th>Pkg</th><th>FW</th><th>HW</th><th>MOQ</th><th>Warranty</th><th>COO</th><th>Source</th><th>Status</th><th>By</th><th>Notes</th><th style="width:80px"></th>
        </tr></thead><tbody>`;

        for (const o of offers) {
            const oid = o.id || o.offer_id;
            const checked = sel.has(oid) ? 'checked' : '';
            const price = o.unit_price != null ? '$' + parseFloat(o.unit_price).toFixed(4) : '\u2014';
            let offerPriceColor = 'var(--teal)';
            let offerPriceTitle = '';
            if (g.target_price != null && o.unit_price != null) {
                const pctD = ((o.unit_price - g.target_price) / g.target_price) * 100;
                offerPriceColor = pctD <= 0 ? 'var(--green)' : pctD <= 15 ? 'var(--amber)' : 'var(--red)';
                offerPriceTitle = ` title="${pctD > 0 ? '+' : ''}${pctD.toFixed(0)}% vs target ($${Number(g.target_price).toFixed(4)})"`;
            }
            const offeredMpn = o.mpn || o.offered_mpn || '';
            const isSub = reqMpn && offeredMpn && offeredMpn.trim().toUpperCase() !== reqMpn.trim().toUpperCase();
            const subBadge = isSub ? '<span class="badge b-sub">SUB</span> ' : '';
            const isPending = o.status === 'pending_review';
            const rowBg = isPending ? 'background:rgba(245,158,11,.06);border-left:2px dashed var(--amber);' : (isSub ? 'background:rgba(14,116,144,.04);' : '');
            const statusBadge = isPending ? ' <span class="badge" style="background:var(--amber-light);color:var(--amber);font-size:9px">DRAFT</span>' : '';
            // Staleness indicator: flag offers older than 7 days
            const offerAgeDays = o.created_at ? Math.floor((Date.now() - new Date(o.created_at).getTime()) / 86400000) : 0;
            const staleBadge = offerAgeDays > 14 ? ' <span class="badge" style="background:var(--red-light);color:var(--red);font-size:8px">STALE</span>' : offerAgeDays > 7 ? ' <span class="badge" style="background:var(--amber-light);color:var(--amber);font-size:8px">AGING</span>' : '';
            const quotedBadge = o.quoted_on ? ` <span class="badge b-quoted">${esc(o.quoted_on)}</span>` : '';
            // Parse confidence badge for email-parsed offers
            let confBadge = '';
            if (o.parse_confidence != null) {
                const cc = o.parse_confidence >= 80 ? 'var(--green)' : o.parse_confidence >= 50 ? 'var(--amber)' : 'var(--red)';
                const cl = o.parse_confidence >= 80 ? 'High' : o.parse_confidence >= 50 ? 'Review' : 'Low';
                confBadge = ` <span class="badge" style="background:${cc}15;color:${cc};font-size:8px;padding:1px 4px" title="AI parse confidence: ${o.parse_confidence}%">${cl} ${o.parse_confidence}%</span>`;
            }

            // Edited-by info
            let editedInfo = '';
            if (o.updated_at) {
                const ago = _timeAgo(o.updated_at);
                editedInfo = `<div style="font-size:9px;color:var(--muted);margin-top:1px">Edited by ${esc(o.updated_by || '?')} \u00b7 ${ago} <span style="cursor:pointer" onclick="event.stopPropagation();ddShowChangelog('offer',${oid})" title="View changes">\u2139\ufe0f</span></div>`;
            }

            html += `<tr class="ofr-row ${checked ? 'selected' : ''}" style="${rowBg}" data-oid="${oid}">
                <td><input type="checkbox" ${checked} onclick="event.stopPropagation();ddToggleOffer(${reqId},${oid},event)" data-oid="${oid}"></td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'vendor_name',this)">${esc(o.vendor_name || '')}${statusBadge}${staleBadge}${confBadge}${quotedBadge}${editedInfo}</td>
                <td class="mono req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'mpn',this)">${subBadge}${esc(offeredMpn || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'manufacturer',this)" style="font-size:10px">${esc(o.manufacturer || '\u2014')}</td>
                <td class="req-edit-cell mono" onclick="ddInlineEditOffer(${reqId},${oid},'qty_available',this)">${o.qty_available != null ? Number(o.qty_available).toLocaleString() : (o.quantity || '\u2014')}</td>
                <td class="req-edit-cell mono" style="color:${offerPriceColor}"${offerPriceTitle} onclick="ddInlineEditOffer(${reqId},${oid},'unit_price',this)">${price}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'lead_time',this)">${esc(o.lead_time || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'condition',this)">${esc(o.condition || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'date_code',this)" style="font-size:10px">${esc(o.date_code || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'packaging',this)" style="font-size:10px">${esc(o.packaging || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'firmware',this)" style="font-size:10px">${esc(o.firmware || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'hardware_code',this)" style="font-size:10px">${esc(o.hardware_code || '\u2014')}</td>
                <td class="req-edit-cell mono" onclick="ddInlineEditOffer(${reqId},${oid},'moq',this)" style="font-size:10px">${o.moq != null ? Number(o.moq).toLocaleString() : '\u2014'}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'warranty',this)" style="font-size:10px">${esc(o.warranty || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'country_of_origin',this)" style="font-size:10px">${esc(o.country_of_origin || '\u2014')}</td>
                <td style="font-size:10px">${esc(o.source || '\u2014')}</td>
                <td style="font-size:10px">${esc(o.status || '\u2014')}</td>
                <td style="font-size:10px">${esc(o.entered_by || '\u2014')}</td>
                <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'notes',this)" style="font-size:10px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escAttr(o.notes || '')}">${esc(o.notes || '\u2014')}</td>
                <td style="white-space:nowrap">${isPending ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();ddApproveOffer(${reqId},${oid})" title="Approve" style="padding:2px 6px;font-size:10px;color:var(--green)">\u2713</button><button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();ddRejectOffer(${reqId},${oid})" title="Reject" style="padding:2px 6px;font-size:10px;color:var(--red)">\u2715</button>` : `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();ddEditOffer(${reqId},${oid})" title="Edit" style="padding:2px 6px;font-size:10px">\u270e</button><button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();ddDeleteOffer(${reqId},${oid})" title="Delete" style="padding:2px 6px;font-size:10px;color:var(--red)">\u2715</button>`}</td>
            </tr>`;
        }
        html += '</tbody></table></div>';

        // ── Historical Offers (cross-requisition) ──────────────────
        const histOffers = g.historical_offers || [];
        if (histOffers.length) {
            const hKey = `${reqId}-${g.requirement_id}`;
            const expanded = !!_ddHistoryExpanded[hKey];
            html += `<div style="margin-top:6px;border-top:1px dashed var(--border);padding-top:6px">`;
            html += `<button class="btn btn-sm" style="font-size:11px;background:var(--bg2);color:var(--text);border:1px solid var(--border);padding:4px 10px" onclick="event.stopPropagation();ddToggleHistory('${hKey}',${reqId})">
                ${expanded ? '\u25BC' : '\u25B6'} ${histOffers.length} historical offer${histOffers.length !== 1 ? 's' : ''} from other RFQs
            </button>`;
            if (expanded) {
                html += `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:4px">`;
                html += `<table class="dtbl"><thead><tr>
                    <th>Vendor</th><th>MPN</th><th>Qty</th><th>Price</th><th>Lead</th><th>Cond</th><th>Date</th><th>Source</th><th>Source Req</th>
                </tr></thead><tbody>`;
                for (const ho of histOffers) {
                    const hPrice = ho.unit_price != null ? '$' + parseFloat(ho.unit_price).toFixed(4) : '\u2014';
                    const hDate = ho.created_at ? new Date(ho.created_at).toLocaleDateString() : '\u2014';
                    const hSub = ho.is_substitute ? '<span class="badge b-sub">SUB</span> ' : '';
                    html += `<tr style="color:var(--muted)">
                        <td>${esc(ho.vendor_name || '')}</td>
                        <td class="mono">${hSub}${esc(ho.mpn || '\u2014')}</td>
                        <td class="mono">${ho.qty_available != null ? Number(ho.qty_available).toLocaleString() : '\u2014'}</td>
                        <td class="mono" style="color:var(--teal)">${hPrice}</td>
                        <td>${esc(ho.lead_time || '\u2014')}</td>
                        <td>${esc(ho.condition || '\u2014')}</td>
                        <td style="font-size:10px">${hDate}</td>
                        <td style="font-size:10px">${esc(ho.source || '\u2014')}</td>
                        <td style="font-size:10px">RFQ-${ho.from_requisition_id || '\u2014'}</td>
                    </tr>`;
                }
                html += '</tbody></table></div>';
            }
            html += '</div>';
        }

        html += '</div>';  // close .offer-group
    });
    panel.innerHTML = html;
}

function ddToggleHistory(hKey, reqId) {
    _ddHistoryExpanded[hKey] = !_ddHistoryExpanded[hKey];
    const data = _ddTabCache[reqId]?.offers;
    if (data) {
        const drow = document.getElementById('d-' + reqId);
        const panel = drow?.querySelector('.dd-panel');
        if (panel) _renderDdOffers(reqId, data, panel);
    }
}

// ── Inline Editing for Offers ──────────────────────────────────────────
function ddInlineEditOffer(reqId, offerId, field, td) {
    if (td.querySelector('.req-edit-input')) return; // already editing
    const groups = (_ddTabCache[reqId]?.offers?.groups || []);
    let offer;
    for (const g of groups) {
        offer = (g.offers || []).find(o => (o.id || o.offer_id) === offerId);
        if (offer) break;
    }
    if (!offer) return;

    const CONDITION_OPTIONS = ['New','New Surplus','Refurbished','Used','As-Is',''];
    let currentVal = '';
    if (field === 'qty_available') currentVal = String(offer.qty_available || '');
    else if (field === 'unit_price') currentVal = offer.unit_price != null ? String(offer.unit_price) : '';
    else if (field === 'moq') currentVal = offer.moq != null ? String(offer.moq) : '';
    else currentVal = offer[field] || '';

    let el;
    if (field === 'condition') {
        el = document.createElement('select');
        el.className = 'req-edit-input';
        el.innerHTML = '<option value="">\u2014</option>' + CONDITION_OPTIONS.filter(Boolean).map(o => `<option value="${o}"${currentVal === o ? ' selected' : ''}>${o}</option>`).join('');
    } else if (field === 'notes') {
        el = document.createElement('textarea');
        el.className = 'req-edit-input';
        el.value = currentVal;
        el.rows = 2;
        el.style.cssText = 'width:160px;font-size:11px;resize:vertical';
    } else {
        el = document.createElement('input');
        el.className = 'req-edit-input';
        el.value = currentVal;
        if (field === 'qty_available' || field === 'moq') { el.type = 'number'; el.min = '0'; el.style.width = '60px'; }
        else if (field === 'unit_price') { el.type = 'number'; el.step = '0.0001'; el.min = '0'; el.style.width = '70px'; }
        else el.style.width = '100px';
    }

    td.textContent = '';
    td.appendChild(el);
    const hint = document.createElement('span');
    hint.style.cssText = 'font-size:9px;color:var(--muted);display:block;margin-top:1px';
    hint.textContent = 'Enter \u2713  Esc \u2717';
    td.appendChild(hint);
    el.focus();
    if (el.select) el.select();

    let _saved = false;
    const save = async () => {
        if (_saved) return;
        _saved = true;
        const val = el.value.trim();
        if (val === currentVal) { _reRenderOffers(reqId); return; }
        const body = {};
        if (field === 'unit_price') body[field] = val ? parseFloat(val) : null;
        else if (field === 'qty_available' || field === 'moq') body[field] = val ? parseInt(val) : null;
        else body[field] = val || null;
        try {
            await apiFetch(`/api/offers/${offerId}`, { method: 'PUT', body });
            // Update cached offer data
            offer[field] = body[field];
            offer.updated_at = new Date().toISOString();
            offer.updated_by = window.__userName || '?';
        } catch(e) { logCatchError('ddInlineEditOffer', e); }
        _reRenderOffers(reqId);
    };

    el.addEventListener('blur', save);
    if (field === 'condition') el.addEventListener('change', () => el.blur());
    el.addEventListener('keydown', e => {
        if (e.key === 'Enter' && field !== 'notes') { e.preventDefault(); el.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); _saved = true; _reRenderOffers(reqId); }
    });
}

function _reRenderOffers(reqId) {
    const data = _ddTabCache[reqId]?.offers;
    if (!data) return;
    const drow = document.getElementById('d-' + reqId);
    const panel = drow?.querySelector('.dd-panel');
    if (panel) _renderDdOffers(reqId, data, panel);
}

// ── Approve / Reject Offers ────────────────────────────────────────────
async function ddApproveOffer(reqId, offerId) {
    try {
        await apiFetch(`/api/offers/${offerId}/approve`, { method: 'PUT' });
        showToast('Offer approved', 'success');
        delete _ddTabCache[reqId]?.offers;
        const drow = document.getElementById('d-' + reqId);
        const panel = drow?.querySelector('.dd-panel');
        if (panel) await _loadDdSubTab(reqId, 'offers', panel);
    } catch(e) { logCatchError('ddApproveOffer', e); showToast('Failed to approve', 'error'); }
}

async function ddRejectOffer(reqId, offerId) {
    promptInput('Reject Offer', 'Rejection reason (optional):', async function(reason) {
        reason = reason || '';
        try {
            await apiFetch(`/api/offers/${offerId}/reject?reason=${encodeURIComponent(reason)}`, { method: 'PUT' });
            showToast('Offer rejected', 'success');
            delete _ddTabCache[reqId]?.offers;
            const drow = document.getElementById('d-' + reqId);
            const panel = drow?.querySelector('.dd-panel');
            if (panel) await _loadDdSubTab(reqId, 'offers', panel);
        } catch(e) { logCatchError('ddRejectOffer', e); showToast('Failed to reject', 'error'); }
    }, {submitLabel: 'Reject', placeholder: 'Enter reason...'});
}

// ── Changelog Popover ──────────────────────────────────────────────────
async function ddShowChangelog(entityType, entityId) {
    // Remove existing popover
    document.querySelectorAll('.changelog-popover').forEach(el => el.remove());
    try {
        const changes = await apiFetch(`/api/changelog/${entityType}/${entityId}`);
        if (!changes.length) { showToast('No change history', 'info'); return; }
        const pop = document.createElement('div');
        pop.className = 'changelog-popover';
        let rows = changes.slice(0, 20).map(c => {
            const ago = c.created_at ? _timeAgo(c.created_at) : '';
            return `<tr><td style="font-weight:600">${esc(c.field_name)}</td><td style="color:var(--red);text-decoration:line-through">${esc(c.old_value || '\u2014')}</td><td style="color:var(--green)">${esc(c.new_value || '\u2014')}</td><td style="font-size:10px">${esc(c.user_name || '?')}</td><td style="font-size:10px;color:var(--muted)">${ago}</td></tr>`;
        }).join('');
        pop.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><strong style="font-size:12px">Change History</strong><button onclick="this.closest('.changelog-popover').remove()" style="border:none;background:none;cursor:pointer;font-size:14px;color:var(--muted)">\u2715</button></div><table class="dtbl" style="font-size:11px"><thead><tr><th>Field</th><th>Old</th><th>New</th><th>By</th><th>When</th></tr></thead><tbody>${rows}</tbody></table>`;
        document.body.appendChild(pop);
        // Position near click
        const rect = event?.target?.getBoundingClientRect();
        if (rect) { pop.style.top = (rect.bottom + 4) + 'px'; pop.style.left = Math.min(rect.left, window.innerWidth - 400) + 'px'; }
        // Click outside to close
        setTimeout(() => document.addEventListener('click', function _cl(e) { if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener('click', _cl); } }, { once: false }), 100);
    } catch(e) { logCatchError('ddShowChangelog', e); }
}

function ddToggleHistorySightings(hKey, reqId) {
    _ddHistoryExpanded[hKey] = !_ddHistoryExpanded[hKey];
    _renderSourcingDrillDown(reqId);
}

async function ddReconfirmOffer(offerId, reqId) {
    try {
        const res = await apiFetch(`/api/offers/${offerId}/reconfirm`, { method: 'PUT' });
        showToast('Offer reconfirmed', 'success');
        // Update row visually
        const row = document.querySelector(`tr[data-ho-id="${offerId}"]`);
        if (row) {
            const btn = row.querySelector('button[title="Mark as still valid"]');
            if (btn) { btn.textContent = '\u2713 ' + (res.reconfirm_count || 1) + 'x'; btn.style.color = 'var(--green)'; }
        }
    } catch (e) {
        showToast('Couldn\'t reconfirm — ' + friendlyError(e, 'please try again'), 'error');
    }
}

async function ddLogFromHistorical(reqId, ho) {
    await openLogOfferFromList(reqId);
    // Pre-fill fields from historical offer after modal opens
    setTimeout(() => {
        const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        if (ho.vendor_name) _s('loVendor', ho.vendor_name);
        if (ho.qty_available) _s('loQty', ho.qty_available);
        if (ho.unit_price) _s('loPrice', ho.unit_price);
        if (ho.lead_time) _s('loLead', ho.lead_time);
        if (ho.condition) _s('loCond', ho.condition);
        if (ho.manufacturer) _s('loMfr', ho.manufacturer);
        _s('loNotes', 'Logged from RFQ-' + (ho.from_requisition_id || ''));
        // Auto-select matching requirement by MPN
        const sel = document.getElementById('loReqPart');
        if (sel && ho.mpn) {
            const hoMpn = ho.mpn.trim().toUpperCase();
            for (const opt of sel.options) {
                if ((opt.dataset.mpn || '').trim().toUpperCase() === hoMpn) {
                    sel.value = opt.value;
                    break;
                }
            }
        }
    }, 100);
}

function ddToggleOffer(reqId, offerId, event) {
    if (event) event.stopPropagation();
    if (!_ddSelectedOffers[reqId]) _ddSelectedOffers[reqId] = new Set();
    const sel = _ddSelectedOffers[reqId];
    if (sel.has(offerId)) sel.delete(offerId); else sel.add(offerId);
    // Re-render sourcing drilldown (offers are now inline in sourcing tab)
    const activeTab = _ddActiveTab[reqId] || _ddDefaultTab(_currentMainView);
    if (activeTab === 'sourcing') {
        _renderSourcingDrillDown(reqId);
    } else {
        // Fallback: re-render standalone offers view if still used
        const data = _ddTabCache[reqId]?.offers;
        const drow = document.getElementById('d-' + reqId);
        if (data && drow) {
            const panel = drow.querySelector('.dd-panel');
            if (panel) _renderDdOffers(reqId, data, panel);
        }
    }
}

function ddToggleAllOffers(reqId, checked) {
    const data = _ddTabCache[reqId]?.offers;
    if (!data) return;
    const groups = data.groups || data || [];
    if (!_ddSelectedOffers[reqId]) _ddSelectedOffers[reqId] = new Set();
    const sel = _ddSelectedOffers[reqId];
    sel.clear();
    if (checked) {
        for (const g of (Array.isArray(groups) ? groups : [])) {
            for (const o of (g.offers || [])) {
                sel.add(o.id || o.offer_id);
            }
        }
    }
    const drow = document.getElementById('d-' + reqId);
    if (drow) {
        const panel = drow.querySelector('.dd-panel');
        if (panel) _renderDdOffers(reqId, data, panel);
    }
}

function ddToggleGroupOffers(reqId, groupIdx, checked) {
    const data = _ddTabCache[reqId]?.offers;
    if (!data) return;
    const groups = data.groups || data || [];
    const grpArr = Array.isArray(groups) ? groups : [];
    const g = grpArr[groupIdx];
    if (!g) return;
    if (!_ddSelectedOffers[reqId]) _ddSelectedOffers[reqId] = new Set();
    const sel = _ddSelectedOffers[reqId];
    for (const o of (g.offers || [])) {
        const oid = o.id || o.offer_id;
        if (checked) sel.add(oid); else sel.delete(oid);
    }
    const drow = document.getElementById('d-' + reqId);
    if (drow) {
        const panel = drow.querySelector('.dd-panel');
        if (panel) _renderDdOffers(reqId, data, panel);
    }
}



// ── Payment/Shipping Terms Presets ─────────────────────────────────────
// Shared preset lists for payment and shipping terms dropdowns.
// Used by: Build Quote modal, inline draft quote editor, ddShowCopyFromQuote
const _PAYMENT_TERMS = ['Net 30', 'Net 45', 'Net 60', 'COD', 'Prepaid', 'CIA'];
const _SHIPPING_TERMS = ['FOB Origin', 'FOB Destination', 'CIF', 'DDP', 'EXW', 'DAP'];

function _termsSelectHtml(id, currentVal, presets) {
    const isCustom = currentVal && !presets.includes(currentVal);
    let html = `<select id="${id}" style="width:120px;padding:2px 4px">`;
    for (const p of presets) {
        html += `<option value="${escAttr(p)}"${p === currentVal ? ' selected' : ''}>${esc(p)}</option>`;
    }
    html += `<option value="__custom"${isCustom ? ' selected' : ''}>Other\u2026</option></select>`;
    return html;
}

function _wireTermsSelect(selectId, customId) {
    const sel = document.getElementById(selectId);
    const custom = document.getElementById(customId);
    if (!sel || !custom) return;
    // If current value is custom, show the custom input pre-filled
    if (sel.value === '__custom') {
        custom.style.display = '';
    }
    sel.addEventListener('change', function() {
        if (this.value === '__custom') {
            custom.style.display = '';
            custom.focus();
        } else {
            custom.style.display = 'none';
            custom.value = '';
        }
    });
}

function _getTermsValue(selectId, customId) {
    const sel = document.getElementById(selectId);
    if (!sel) return '';
    if (sel.value === '__custom') {
        return document.getElementById(customId)?.value || '';
    }
    return sel.value;
}

async function ddShowCopyFromQuote(prefix) {
    try {
        const data = await apiFetch('/api/quotes/recent-terms');
        if (!data || !data.length) { showToast('No recent quotes found', 'info'); return; }
        // Build a small dropdown overlay near the button
        const btnId = prefix === 'bq' ? 'bqCopyPrevBtn' : null;
        let listHtml = '<div class="copy-terms-dropdown" style="position:absolute;z-index:9999;background:var(--bg1);border:1px solid var(--border);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15);padding:4px 0;min-width:220px;max-height:200px;overflow-y:auto">';
        for (const q of data) {
            const label = esc((q.customer_name || 'Unknown') + ' — ' + (q.quote_number || 'Draft'));
            const terms = [q.payment_terms, q.shipping_terms].filter(Boolean).join(' / ') || 'No terms';
            listHtml += `<div class="copy-terms-item" style="padding:6px 12px;cursor:pointer;font-size:11px;border-bottom:1px solid var(--border)" onclick="_applyCopiedTerms('${prefix}',${JSON.stringify(q).replace(/'/g, "\\'")})" onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background=''"><strong>${label}</strong><br><span style="color:var(--muted)">${esc(terms)}</span></div>`;
        }
        listHtml += '</div>';
        // Remove any existing dropdown
        document.querySelectorAll('.copy-terms-dropdown').forEach(el => el.remove());
        const btn = btnId ? document.getElementById(btnId) : null;
        if (btn) {
            btn.style.position = 'relative';
            btn.insertAdjacentHTML('afterend', listHtml);
        } else {
            document.body.insertAdjacentHTML('beforeend', listHtml);
        }
        // Close on outside click
        setTimeout(() => {
            document.addEventListener('click', function _closeCopy(e) {
                if (!e.target.closest('.copy-terms-dropdown')) {
                    document.querySelectorAll('.copy-terms-dropdown').forEach(el => el.remove());
                    document.removeEventListener('click', _closeCopy);
                }
            });
        }, 100);
    } catch (e) {
        showToast('Could not load recent quotes', 'error');
    }
}

function _applyCopiedTerms(prefix, q) {
    document.querySelectorAll('.copy-terms-dropdown').forEach(el => el.remove());
    const termsSelId = prefix === 'bq' ? 'bqTerms' : null;
    const shipSelId = prefix === 'bq' ? 'bqShip' : null;
    const notesId = prefix === 'bq' ? 'bqNotes' : null;
    const validId = prefix === 'bq' ? 'bqValid' : null;

    // Set payment terms
    if (termsSelId && q.payment_terms) {
        _setTermsSelect(termsSelId, termsSelId + 'Custom', q.payment_terms, _PAYMENT_TERMS);
    }
    // Set shipping terms
    if (shipSelId && q.shipping_terms) {
        _setTermsSelect(shipSelId, shipSelId + 'Custom', q.shipping_terms, _SHIPPING_TERMS);
    }
    // Set notes
    if (notesId && q.notes) {
        const notesEl = document.getElementById(notesId);
        if (notesEl) notesEl.value = q.notes;
    }
    // Set validity
    if (validId && q.validity_days) {
        const validEl = document.getElementById(validId);
        if (validEl) validEl.value = q.validity_days;
    }
    showToast('Terms copied from ' + (q.quote_number || 'recent quote'), 'success');
}

function _setTermsSelect(selectId, customId, value, presets) {
    const sel = document.getElementById(selectId);
    const custom = document.getElementById(customId);
    if (!sel) return;
    if (presets.includes(value)) {
        sel.value = value;
        if (custom) { custom.style.display = 'none'; custom.value = ''; }
    } else {
        sel.value = '__custom';
        if (custom) { custom.style.display = ''; custom.value = value; }
    }
}

function ddBuildQuote(reqId) {
    const sel = _ddSelectedOffers[reqId];
    if (!sel || sel.size === 0) return;
    // Gather selected offers from cache
    const data = _ddTabCache[reqId]?.offers;
    if (!data) return;
    const groups = data.groups || data || [];
    const offers = [];
    for (const g of (Array.isArray(groups) ? groups : [])) {
        for (const o of (g.offers || [])) {
            if (sel.has(o.id || o.offer_id)) {
                offers.push({...o, _targetPrice: g.target_price, _targetQty: g.target_qty, _lastQuoted: g.last_quoted});
            }
        }
    }
    if (!offers.length) return;
    // Build modal
    let linesHtml = '';
    for (let i = 0; i < offers.length; i++) {
        const o = offers[i];
        const cost = o.unit_price != null ? parseFloat(o.unit_price) : 0;
        const target = o._targetPrice != null ? fmtPrice(o._targetPrice) : '\u2014';
        const inpStyle = 'width:60px;padding:2px 4px;font-size:10px';
        linesHtml += `<tr>
            <td class="mono" style="font-size:11px">${esc(o.mpn || o.offered_mpn || '')}</td>
            <td style="font-size:10px">${esc(o.manufacturer || '\u2014')}</td>
            <td style="font-size:10px">${esc(o.vendor_name || '')}</td>
            <td class="mono">${(o.qty_available || 0).toLocaleString()}</td>
            <td class="mono">${fmtPrice(cost)}</td>
            <td style="font-size:10px;color:var(--muted)">${target}</td>
            <td><input type="number" step="0.01" class="bq-sell" data-idx="${i}" data-cost="${cost}" value="${cost.toFixed(2)}" style="width:85px;padding:2px 4px;font-size:11px;font-family:'JetBrains Mono',monospace"></td>
            <td class="bq-margin-cell" data-idx="${i}" style="font-weight:600">0.0%</td>
            <td><input type="text" class="bq-lead" data-idx="${i}" value="${escAttr(o.lead_time || '')}" placeholder="days" style="${inpStyle}"></td>
            <td><input type="text" class="bq-cond" data-idx="${i}" value="${escAttr(o.condition || '')}" placeholder="\u2014" style="${inpStyle}"></td>
            <td><input type="text" class="bq-dc" data-idx="${i}" value="${escAttr(o.date_code || '')}" placeholder="\u2014" style="${inpStyle}"></td>
            <td><input type="text" class="bq-pkg" data-idx="${i}" value="${escAttr(o.packaging || '')}" placeholder="\u2014" style="${inpStyle}"></td>
            <td><input type="text" class="bq-fw" data-idx="${i}" value="${escAttr(o.firmware || '')}" placeholder="\u2014" style="${inpStyle}"></td>
            <td><input type="text" class="bq-hw" data-idx="${i}" value="${escAttr(o.hardware_code || '')}" placeholder="\u2014" style="${inpStyle}"></td>
            <td class="mono" style="font-size:10px">${o.moq != null ? Number(o.moq).toLocaleString() : '\u2014'}</td>
            <td style="font-size:10px">${esc(o.warranty || '\u2014')}</td>
            <td style="font-size:10px">${esc(o.country_of_origin || '\u2014')}</td>
        </tr>`;
    }

    const html = `
    <div class="modal-bg open" id="ddBuildQuoteBg" onclick="if(event.target===this){this.remove()}">
        <div class="modal modal-lg" onclick="event.stopPropagation()" style="max-width:1100px">
            <h2>Build Quote \u2014 ${offers.length} line${offers.length !== 1 ? 's' : ''}</h2>
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
                <label style="font-size:12px;font-weight:600">Global Margin %</label>
                <input type="number" id="bqGlobalMargin" value="0" step="1" style="width:70px;padding:4px 6px" oninput="ddApplyGlobalMarkup()">
                <button class="btn btn-ghost btn-sm" onclick="ddApplyGlobalMarkup()">Apply</button>
            </div>
            <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
            <table class="dtbl" style="font-size:11px">
                <thead><tr><th>MPN</th><th>Mfr</th><th>Vendor</th><th>Qty</th><th>Cost</th><th>Target</th><th>Sell $</th><th>Margin</th><th>Lead</th><th>Cond</th><th>DC</th><th>Pkg</th><th>FW</th><th>HW</th><th>MOQ</th><th>Warranty</th><th>COO</th></tr></thead>
                <tbody>${linesHtml}</tbody>
                <tfoot><tr style="font-weight:700">
                    <td colspan="4">Total</td>
                    <td class="mono" id="bqTotalCost"></td>
                    <td></td>
                    <td class="mono" id="bqTotalSell" style="color:var(--teal)"></td>
                    <td id="bqTotalMargin"></td>
                    <td colspan="9"></td>
                </tr></tfoot>
            </table>
            </div>
            <div style="display:flex;gap:16px;margin-top:12px;flex-wrap:wrap;font-size:12px;align-items:end">
                <label>Payment Terms <select id="bqTerms" style="width:120px;padding:4px 6px"><option value="Net 30" selected>Net 30</option><option value="Net 45">Net 45</option><option value="Net 60">Net 60</option><option value="COD">COD</option><option value="Prepaid">Prepaid</option><option value="CIA">CIA</option><option value="__custom">Other…</option></select></label>
                <input id="bqTermsCustom" style="width:100px;padding:4px 6px;display:none" placeholder="Custom terms">
                <label>Shipping <select id="bqShip" style="width:120px;padding:4px 6px"><option value="FOB Origin" selected>FOB Origin</option><option value="FOB Destination">FOB Destination</option><option value="CIF">CIF</option><option value="DDP">DDP</option><option value="EXW">EXW</option><option value="DAP">DAP</option><option value="__custom">Other…</option></select></label>
                <input id="bqShipCustom" style="width:100px;padding:4px 6px;display:none" placeholder="Custom shipping">
                <label>Valid <input id="bqValid" type="number" value="7" style="width:50px;padding:4px 6px"> days</label>
                <button class="btn btn-ghost btn-sm" id="bqCopyPrevBtn" onclick="ddShowCopyFromQuote('bq')" title="Copy terms from a recent quote" style="font-size:11px">Copy from recent…</button>
            </div>
            <div style="margin-top:8px"><label style="font-size:12px">Notes<br><textarea id="bqNotes" rows="2" style="width:100%;font-size:11px;padding:4px" placeholder="Special instructions, terms, etc."></textarea></label></div>
            <div class="mactions" style="margin-top:12px">
                <button class="btn btn-ghost" onclick="document.getElementById('ddBuildQuoteBg').remove()">Cancel</button>
                <button class="btn btn-primary" id="bqCreateBtn" onclick="ddConfirmBuildQuote(${reqId})">Create Quote</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', html);

    // Wire up payment/shipping custom selects
    _wireTermsSelect('bqTerms', 'bqTermsCustom');
    _wireTermsSelect('bqShip', 'bqShipCustom');

    // Wire up per-line sell price inputs
    document.querySelectorAll('.bq-sell').forEach(inp => {
        inp.addEventListener('input', () => {
            const idx = inp.dataset.idx;
            const cost = parseFloat(inp.dataset.cost) || 0;
            const sell = parseFloat(inp.value) || 0;
            const margin = window.calcMarginPct ? window.calcMarginPct(sell, cost) : (sell > 0 ? ((sell - cost) / sell * 100) : 0);
            const cell = document.querySelector(`.bq-margin-cell[data-idx="${idx}"]`);
            if (cell) {
                cell.textContent = margin.toFixed(1) + '%';
                cell.style.color = window.marginColor ? window.marginColor(margin) : (margin >= 20 ? 'var(--green)' : margin >= 10 ? 'var(--amber)' : 'var(--red)');
            }
            ddUpdateBqTotals();
        });
    });
    ddUpdateBqTotals();
}

function ddApplyGlobalMarkup() {
    const pct = parseFloat(document.getElementById('bqGlobalMargin')?.value) || 0;
    document.querySelectorAll('.bq-sell').forEach(inp => {
        const cost = parseFloat(inp.dataset.cost) || 0;
        const sell = pct >= 100 ? 0 : Math.round(cost / (1 - pct / 100) * 100) / 100;
        inp.value = sell.toFixed(2);
        inp.dispatchEvent(new Event('input'));
    });
}

function ddUpdateBqTotals() {
    let totalCost = 0, totalSell = 0;
    document.querySelectorAll('.bq-sell').forEach(inp => {
        const cost = parseFloat(inp.dataset.cost) || 0;
        const sell = parseFloat(inp.value) || 0;
        // Find qty from the same row (4th cell — MPN, Mfr, Vendor, Qty)
        const row = inp.closest('tr');
        const qtyText = row?.children[3]?.textContent?.replace(/,/g, '') || '0';
        const qty = parseInt(qtyText) || 0;
        totalCost += cost * qty;
        totalSell += sell * qty;
    });
    const margin = totalSell > 0 ? ((totalSell - totalCost) / totalSell * 100) : 0;
    const costEl = document.getElementById('bqTotalCost');
    const sellEl = document.getElementById('bqTotalSell');
    const mEl = document.getElementById('bqTotalMargin');
    if (costEl) costEl.textContent = '$' + totalCost.toFixed(2);
    if (sellEl) sellEl.textContent = '$' + totalSell.toFixed(2);
    if (mEl) {
        mEl.textContent = margin.toFixed(1) + '%';
        mEl.style.color = margin >= 20 ? 'var(--green)' : margin >= 10 ? 'var(--amber)' : 'var(--red)';
    }
}

async function ddConfirmBuildQuote(reqId) {
    const btn = document.getElementById('bqCreateBtn');
    if (!btn) return;
    btn.disabled = true; btn.textContent = 'Creating\u2026';
    const sel = _ddSelectedOffers[reqId];
    try {
        // Create quote from offer IDs (backend generates line items at cost)
        const quote = await apiFetch('/api/requisitions/' + reqId + '/quote', {
            method: 'POST', body: { offer_ids: Array.from(sel) }
        });
        // Apply user's sell prices + line details to line items
        const sellInputs = document.querySelectorAll('.bq-sell');
        const lines = quote.line_items || [];
        sellInputs.forEach((inp, i) => {
            if (lines[i]) {
                const sell = parseFloat(inp.value) || 0;
                const cost = lines[i].cost_price || 0;
                lines[i].sell_price = sell;
                lines[i].margin_pct = window.calcMarginPct ? window.calcMarginPct(sell, cost) : (sell > 0 ? ((sell - cost) / sell * 100) : 0);
                const lead = document.querySelector(`.bq-lead[data-idx="${i}"]`);
                const cond = document.querySelector(`.bq-cond[data-idx="${i}"]`);
                const dc = document.querySelector(`.bq-dc[data-idx="${i}"]`);
                const pkg = document.querySelector(`.bq-pkg[data-idx="${i}"]`);
                const fw = document.querySelector(`.bq-fw[data-idx="${i}"]`);
                const hw = document.querySelector(`.bq-hw[data-idx="${i}"]`);
                if (lead) lines[i].lead_time = lead.value;
                if (cond) lines[i].condition = cond.value;
                if (dc) lines[i].date_code = dc.value;
                if (pkg) lines[i].packaging = pkg.value;
                if (fw) lines[i].firmware = fw.value;
                if (hw) lines[i].hardware_code = hw.value;
            }
        });
        // Save line items + terms
        await apiFetch('/api/quotes/' + quote.id, {
            method: 'PUT', body: {
                line_items: lines,
                payment_terms: _getTermsValue('bqTerms', 'bqTermsCustom'),
                shipping_terms: _getTermsValue('bqShip', 'bqShipCustom'),
                validity_days: parseInt(document.getElementById('bqValid')?.value) || 7,
                notes: document.getElementById('bqNotes')?.value || '',
            }
        });
        document.getElementById('ddBuildQuoteBg')?.remove();
        showToast('Quote created \u2014 switching to Quotes tab', 'success');
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
        _switchDdTab(reqId, 'quotes');
    } catch (e) {
        btn.disabled = false; btn.textContent = 'Create Quote';
        const msg = (e.message || '').toLowerCase();
        if (e.status === 400 && msg.includes('customer site')) {
            showToast('Link this requisition to a customer site first', 'error');
        } else {
            showToast('Error building quote: ' + (e.message || 'unknown'), 'error');
        }
    }
}

function ddEditOffer(reqId, offerId) {
    // Find the offer data in cache
    const data = _ddTabCache[reqId]?.offers;
    if (!data) return;
    let offer = null;
    for (const g of (data.groups || data || [])) {
        for (const o of (g.offers || [])) {
            if ((o.id || o.offer_id) === offerId) { offer = o; break; }
        }
        if (offer) break;
    }
    if (!offer) return;
    // Build inline edit form
    const o = offer;
    const formHtml = `
    <div class="modal-bg open" id="ddEditOfferBg" onclick="if(event.target===this){this.remove()}">
        <div class="modal modal-lg" onclick="event.stopPropagation()">
            <h2>Edit Offer — ${esc(o.mpn || '')} from ${esc(o.vendor_name || '')}</h2>
            <div class="lo-form">
                <div class="field"><label>Vendor Name</label><input id="ddEoVendor" value="${escAttr(o.vendor_name || '')}"></div>
                <div class="field"><label>MPN</label><input id="ddEoMpn" value="${escAttr(o.mpn || '')}"></div>
                <div class="field"><label>Qty Available</label><input id="ddEoQty" type="number" value="${o.qty_available || ''}"></div>
                <div class="field"><label>Unit Price ($)</label><input id="ddEoPrice" type="number" step="0.0001" value="${o.unit_price || ''}"></div>
                <div class="field"><label>Lead Time</label><input id="ddEoLead" value="${escAttr(o.lead_time || '')}"></div>
                <div class="field"><label>Condition</label>
                    <select id="ddEoCond"><option value="new" ${o.condition==='new'?'selected':''}>New</option><option value="refurbished" ${o.condition==='refurbished'?'selected':''}>Refurbished</option><option value="used" ${o.condition==='used'?'selected':''}>Used</option></select>
                </div>
                <div class="field"><label>MOQ</label><input id="ddEoMoq" type="number" value="${o.moq || ''}"></div>
                <div class="field"><label>Date Code</label><input id="ddEoDc" value="${escAttr(o.date_code || '')}"></div>
                <div class="field"><label>Manufacturer</label><input id="ddEoMfr" value="${escAttr(o.manufacturer || '')}"></div>
                <div class="field"><label>Packaging</label><input id="ddEoPkg" value="${escAttr(o.packaging || '')}"></div>
                <div class="field"><label>Warranty</label><input id="ddEoWar" value="${escAttr(o.warranty || '')}"></div>
                <div class="field"><label>Country of Origin</label><input id="ddEoCoo" value="${escAttr(o.country_of_origin || '')}"></div>
                <div class="field field-full"><label>Notes</label><textarea id="ddEoNotes" rows="2" style="resize:vertical">${esc(o.notes || '')}</textarea></div>
                <div class="field"><label>Status</label>
                    <select id="ddEoStatus"><option value="active" ${o.status==='active'?'selected':''}>Active</option><option value="pending_review" ${o.status==='pending_review'?'selected':''}>Pending Review</option><option value="expired" ${o.status==='expired'?'selected':''}>Expired</option><option value="won" ${o.status==='won'?'selected':''}>Won</option><option value="lost" ${o.status==='lost'?'selected':''}>Lost</option></select>
                </div>
            </div>
            <div class="mactions">
                <button type="button" class="btn btn-ghost" onclick="document.getElementById('ddEditOfferBg').remove()">Cancel</button>
                <button type="button" class="btn btn-primary" id="ddEoSaveBtn" onclick="ddSaveEditOffer(${reqId},${offerId})">Save Changes</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', formHtml);
    document.getElementById('ddEoVendor')?.focus();
}

async function ddSaveEditOffer(reqId, offerId) {
    const btn = document.getElementById('ddEoSaveBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }
    try {
        const _v = id => document.getElementById(id)?.value ?? '';
        const body = {
            vendor_name: _v('ddEoVendor').trim() || undefined,
            mpn: _v('ddEoMpn').trim() || undefined,
            qty_available: parseInt(_v('ddEoQty')) || null,
            unit_price: parseFloat(_v('ddEoPrice')) || null,
            lead_time: _v('ddEoLead').trim() || null,
            condition: _v('ddEoCond') || null,
            moq: parseInt(_v('ddEoMoq')) || null,
            date_code: _v('ddEoDc').trim() || null,
            manufacturer: _v('ddEoMfr').trim() || null,
            packaging: _v('ddEoPkg').trim() || null,
            warranty: _v('ddEoWar').trim() || null,
            country_of_origin: _v('ddEoCoo').trim() || null,
            notes: _v('ddEoNotes').trim() || null,
            status: _v('ddEoStatus'),
        };
        await apiFetch(`/api/offers/${offerId}`, { method: 'PUT', body });
        document.getElementById('ddEditOfferBg')?.remove();
        showToast('Offer updated', 'success');
        // Refresh offers tab
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].offers;
        const drow = document.getElementById('d-' + reqId);
        if (drow) {
            const panel = drow.querySelector('.dd-panel');
            if (panel) await _loadDdSubTab(reqId, 'offers', panel);
        }
    } catch (e) {
        showToast('Couldn\'t update — ' + friendlyError(e, 'please try again'), 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Save Changes'; }
    }
}

async function ddDeleteOffer(reqId, offerId) {
    confirmAction('Delete Offer', 'Delete this offer?', async function() {
        try {
            await apiFetch(`/api/offers/${offerId}`, { method: 'DELETE' });
            showToast('Offer deleted', 'success');
            if (_ddTabCache[reqId]) delete _ddTabCache[reqId].offers;
            const drow = document.getElementById('d-' + reqId);
            if (drow) {
                const panel = drow.querySelector('.dd-panel');
                if (panel) await _loadDdSubTab(reqId, 'offers', panel);
            }
            // Update list count
            const reqInfo = _reqListData.find(r => r.id === reqId);
            if (reqInfo && reqInfo.offer_count > 0) {
                reqInfo.offer_count--;
                renderReqList();
            }
        } catch (e) {
            showToast('Couldn\'t delete — ' + friendlyError(e, 'please try again'), 'error');
        }
    }, {confirmClass: 'btn-danger', confirmLabel: 'Delete'});
}

function _renderDdQuotes(reqId, data, panel) {
    // data is now an array of quotes (newest first from API)
    const quotes = Array.isArray(data) ? data : (data && data.id ? [data] : []);
    if (!quotes.length) {
        panel.innerHTML = '<div style="text-align:center;padding:24px 16px">' +
            '<div style="font-size:13px;font-weight:600;color:var(--muted);margin-bottom:6px">No quotes yet</div>' +
            '<div style="font-size:11px;color:var(--muted)">Select offers from the <b>Offers</b> tab and click <b>Build Quote</b> to create a customer quote.</div></div>';
        return;
    }
    try {

    let html = '';
    // ── Quotes list table ──
    html += `<table class="tbl" style="font-size:11px;width:100%">
        <thead><tr><th>Quote #</th><th>Rev</th><th>Customer</th><th>Lines</th><th>Subtotal</th><th>Margin</th><th>Status</th><th>Created</th></tr></thead><tbody>`;
    for (const q of quotes) {
        const lines = q.line_items || [];
        const subtotal = q.subtotal || lines.reduce((s, l) => s + ((l.sell_price || l.unit_sell || 0) * (l.qty || 0)), 0);
        const marginPct = q.total_margin_pct != null ? q.total_margin_pct : 0;
        const statusMap = {draft:'Draft',sent:'Sent',revised:'Revised',won:'Won',lost:'Lost'};
        const statusLabel = statusMap[q.status] || q.status || 'Draft';
        html += `<tr style="cursor:pointer" onclick="ddExpandQuote(${reqId},${q.id})">
            <td class="mono" style="font-weight:600">${esc(q.quote_number || 'Q-' + q.id)}</td>
            <td>${q.revision || 1}</td>
            <td style="font-size:10px">${esc(q.customer_name || '\u2014')}</td>
            <td>${lines.length}</td>
            <td class="mono">$${Number(subtotal).toLocaleString(undefined,{minimumFractionDigits:2})}</td>
            <td style="color:${marginPct >= 20 ? 'var(--green)' : marginPct >= 10 ? 'var(--amber)' : 'var(--red)'};font-weight:600">${Number(marginPct).toFixed(1)}%</td>
            <td><span class="status-badge status-${q.status || 'draft'}" style="font-size:10px;padding:2px 6px;border-radius:4px">${statusLabel}</span>${q.is_expired && (q.status === 'sent' || q.status === 'revised') ? ' <span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:9px;font-weight:600;color:#fff;background:#ef4444">Expired</span>' : (q.days_until_expiry != null && q.days_until_expiry <= 3 && q.days_until_expiry >= 0 && (q.status === 'sent' || q.status === 'revised')) ? ' <span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:9px;font-weight:600;color:#fff;background:#f59e0b">Expires in ' + q.days_until_expiry + 'd</span>' : ''}</td>
            <td style="font-size:10px;color:var(--muted)">${q.created_at ? fmtRelative(q.created_at) : '\u2014'}</td>
        </tr>
        <tr id="ddqDetail-${q.id}" style="display:none"><td colspan="8" style="padding:0"></td></tr>`;
    }
    html += `</tbody></table>`;
    panel.innerHTML = html;
    } catch (renderErr) {
        console.error('Quote tab render error:', renderErr);
        panel.innerHTML = '<span style="font-size:11px;color:var(--red)">Error rendering quotes — please refresh and try again</span>';
    }
}

// ── Files Tab ────────────────────────────────────────────────────────────
function _renderDdFiles(reqId, data, panel) {
    const files = Array.isArray(data) ? data : [];
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:12px;font-weight:600">Attachments (${files.length})</span>
        <label class="btn btn-ghost btn-sm" style="cursor:pointer;font-size:11px">
            <input type="file" style="display:none" onchange="uploadReqAttachment(${reqId},this)"> + Upload File
        </label>
    </div>`;
    if (!files.length) {
        html += '<span style="font-size:11px;color:var(--muted)">No files attached yet</span>';
    } else {
        for (const f of files) {
            const size = f.size_bytes ? (f.size_bytes < 1024 ? f.size_bytes + ' B' : (f.size_bytes / 1024).toFixed(1) + ' KB') : '';
            html += `<div class="att-row">
                <div style="display:flex;align-items:center;gap:8px;min-width:0;flex:1">
                    <span style="font-size:13px">${_fileIcon(f.content_type)}</span>
                    <a href="${f.onedrive_url || '#'}" target="_blank" style="font-size:12px;color:var(--blue);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.file_name)}</a>
                </div>
                <div style="display:flex;align-items:center;gap:10px;flex-shrink:0">
                    <span style="font-size:10px;color:var(--muted)">${size}</span>
                    <span style="font-size:10px;color:var(--muted)">${f.uploaded_by || ''}</span>
                    <span style="font-size:10px;color:var(--muted)">${f.created_at ? fmtRelative(f.created_at) : ''}</span>
                    <button class="btn btn-ghost btn-sm" style="font-size:10px;color:var(--red)" onclick="deleteReqAttachment(${reqId},${f.id})">Remove</button>
                </div>
            </div>`;
        }
    }
    panel.innerHTML = html;
}

function _fileIcon(contentType) {
    if (!contentType) return '\u{1F4CE}';
    if (contentType.includes('pdf')) return '\u{1F4C4}';
    if (contentType.includes('image')) return '\u{1F5BC}';
    if (contentType.includes('spreadsheet') || contentType.includes('excel') || contentType.includes('csv')) return '\u{1F4CA}';
    return '\u{1F4CE}';
}

async function uploadReqAttachment(reqId, input) {
    if (!input.files || !input.files.length) return;
    const file = input.files[0];
    if (file.size > 10 * 1024 * 1024) { showToast('File too large (max 10 MB)', 'error'); return; }
    const formData = new FormData();
    formData.append('file', file);
    try {
        await apiFetch(`/api/requisitions/${reqId}/attachments`, { method: 'POST', body: formData, raw: true });
        showToast('File uploaded', 'success');
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].files;
        const panel = input.closest('.dd-panel') || input.closest('[class*="panel"]');
        if (panel) await _loadDdSubTab(reqId, 'files', panel);
    } catch(e) {
        showToast('Upload failed — ' + friendlyError(e, 'please try again'), 'error');
    }
    input.value = '';
}

async function deleteReqAttachment(reqId, attId) {
    confirmAction('Remove File', 'Remove this file?', async function() {
        try {
            await apiFetch(`/api/requisition-attachments/${attId}`, { method: 'DELETE' });
            showToast('File removed', 'success');
            if (_ddTabCache[reqId]) delete _ddTabCache[reqId].files;
            // Re-render files tab
            const data = await apiFetch(`/api/requisitions/${reqId}/attachments`);
            if (_ddTabCache[reqId]) _ddTabCache[reqId].files = data;
            const panel = document.querySelector('.dd-panel') || document.querySelector('[class*="dd-sub-content"]');
            if (panel) _renderDdFiles(reqId, data, panel);
        } catch(e) {
            showToast('Couldn\'t delete — ' + friendlyError(e, 'please try again'), 'error');
        }
    }, {confirmClass: 'btn-danger', confirmLabel: 'Remove'});
}

// Expand/collapse a single quote detail row
function ddExpandQuote(reqId, quoteId) {
    const detailRow = document.getElementById('ddqDetail-' + quoteId);
    if (!detailRow) return;
    // Toggle: if already visible, collapse
    if (detailRow.style.display !== 'none') {
        detailRow.style.display = 'none';
        return;
    }
    // Collapse any other expanded quote in this req
    document.querySelectorAll(`[id^="ddqDetail-"]`).forEach(r => { if (r.id !== 'ddqDetail-' + quoteId) r.style.display = 'none'; });
    detailRow.style.display = '';
    const cell = detailRow.querySelector('td');
    // Find quote data from cache
    const allQuotes = _ddTabCache[reqId]?.quotes;
    const qArr = Array.isArray(allQuotes) ? allQuotes : (allQuotes?.id ? [allQuotes] : []);
    const q = qArr.find(x => x.id === quoteId);
    if (!q) { cell.innerHTML = '<span style="color:var(--red);font-size:11px">Quote data not found</span>'; return; }
    _ddQuoteData[reqId] = JSON.parse(JSON.stringify(q));
    _renderQuoteDetail(reqId, q, cell);
}

// Render full detail view for a single expanded quote
function _renderQuoteDetail(reqId, q, container) {
    const lines = q.lines || q.line_items || [];
    const isDraft = !q.status || q.status === 'draft';
    const statusMap = {draft:'Draft',sent:'Sent',revised:'Revised',won:'Won',lost:'Lost'};
    const statusLabel = statusMap[q.status] || q.status || 'Draft';
    let html = `<div style="border-radius:8px;margin:4px 0;background:#fff;overflow:hidden;border:2px solid var(--blue);box-shadow:var(--shadow-sm)">`;

    // ── Logo + quote info ──
    html += `<div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:10px">
            <img src="/static/trio_logo.png" alt="TRIO" style="height:28px">
            <div>
                <div style="font-weight:700;font-size:12px;color:var(--blue)">${esc(q.quote_number || 'Q-' + q.id)} <span style="font-weight:400;color:var(--muted)">Rev ${q.revision || 1}</span> <span class="status-badge status-${q.status || 'draft'}" style="font-size:10px;padding:2px 6px;border-radius:4px">${statusLabel}</span></div>
                <div style="font-size:10px;color:var(--muted)">${esc(q.customer_name || '')}${q.contact_name ? ' \u00b7 ' + esc(q.contact_name) : ''}${q.sent_at ? ' \u00b7 Sent ' + fmtRelative(q.sent_at) : ''}</div>
            </div>
        </div>
    </div>`;

    // ── Content ──
    html += `<div style="padding:12px 16px">`;

    // ── Line items table ──
    if (lines.length) {
        html += `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
        <table class="tbl quote-table" style="font-size:11px;width:100%">
            <thead><tr><th>MPN</th><th>Mfr</th><th>Qty</th>${isDraft ? '<th>Cost</th><th>Target</th>' : ''}<th>Unit Price</th>${isDraft ? '<th>Margin</th>' : ''}<th>Lead</th><th>Cond</th><th>DC</th><th>Pkg</th>${!isDraft ? '<th>Ext. Price</th>' : ''}</tr></thead><tbody>`;
        let totalCost = 0, totalRev = 0;
        for (let i = 0; i < lines.length; i++) {
            const l = lines[i];
            const cost = l.cost_price || l.unit_cost || 0;
            const sell = l.sell_price || l.unit_sell || 0;
            const qty = l.qty || 0;
            const margin = l.margin_pct != null ? l.margin_pct : (window.calcMarginPct ? window.calcMarginPct(sell, cost) : (sell > 0 ? ((sell - cost) / sell * 100) : 0));
            const marginClr = window.marginColor ? window.marginColor(margin) : (margin >= 20 ? 'var(--green)' : margin >= 10 ? 'var(--amber)' : 'var(--red)');
            const target = l.target_price != null ? fmtPrice(l.target_price) : '\u2014';
            totalCost += cost * qty;
            totalRev += sell * qty;

            const cellStyle = 'padding:2px 4px;font-size:10px;width:55px';
            const sellCell = isDraft
                ? `<input type="number" step="0.01" class="quote-sell-input ddq-sell" data-req="${reqId}" data-idx="${i}" value="${sell.toFixed(2)}" style="width:80px;padding:2px 4px;font-size:10px;font-family:'JetBrains Mono',monospace" onchange="ddUpdateQuoteLine(${reqId},${i},this.value)">`
                : fmtPrice(sell);
            const leadCell = isDraft
                ? `<input type="text" class="ddq-field" value="${escAttr(l.lead_time||'')}" onchange="ddUpdateQuoteField(${reqId},${i},'lead_time',this.value)" placeholder="days" style="${cellStyle}">`
                : fmtLead(l.lead_time);
            const condCell = isDraft
                ? `<input type="text" class="ddq-field" value="${escAttr(l.condition||'')}" onchange="ddUpdateQuoteField(${reqId},${i},'condition',this.value)" placeholder="\u2014" style="${cellStyle}">`
                : esc(l.condition || '\u2014');
            const dcCell = isDraft
                ? `<input type="text" class="ddq-field" value="${escAttr(l.date_code||'')}" onchange="ddUpdateQuoteField(${reqId},${i},'date_code',this.value)" placeholder="\u2014" style="${cellStyle}">`
                : esc(l.date_code || '\u2014');
            const pkgCell = isDraft
                ? `<input type="text" class="ddq-field" value="${escAttr(l.packaging||'')}" onchange="ddUpdateQuoteField(${reqId},${i},'packaging',this.value)" placeholder="\u2014" style="${cellStyle}">`
                : esc(l.packaging || '\u2014');

            html += `<tr>
                <td class="mono">${esc(l.mpn || '')}</td>
                <td style="font-size:10px">${esc(l.manufacturer || '\u2014')}</td>
                <td class="mono">${qty.toLocaleString()}</td>
                ${isDraft ? `<td class="mono">${fmtPrice(cost)}</td><td style="font-size:10px;color:var(--muted)">${target}</td>` : ''}
                <td class="mono" style="color:var(--teal)">${sellCell}</td>
                ${isDraft ? `<td class="ddq-margin" data-req="${reqId}" data-idx="${i}" style="color:${marginClr};font-weight:600">${margin.toFixed(1)}%</td>` : ''}
                <td>${leadCell}</td>
                <td>${condCell}</td>
                <td>${dcCell}</td>
                <td>${pkgCell}</td>
                ${!isDraft ? `<td class="mono" style="font-weight:600">$${Number(sell * qty).toLocaleString(undefined,{minimumFractionDigits:2})}</td>` : ''}
            </tr>`;
        }
        html += `</tbody></table></div>`;

        // Quick margin (draft only)
        if (isDraft) {
            html += `<div style="margin:8px 0;font-size:11px;display:flex;align-items:center;gap:8px">
                Quick Margin: <input type="number" id="ddQuoteMarkup-${reqId}" value="20" style="width:50px;padding:2px 4px" min="0" max="99">%
                <button class="btn btn-ghost btn-sm" onclick="ddApplyQuoteMarkup(${reqId})">Apply</button>
            </div>`;
        }

        // Totals bar
        const totalMargin = totalRev > 0 ? ((totalRev - totalCost) / totalRev * 100) : 0;
        const gp = totalRev - totalCost;
        if (isDraft) {
            html += `<div id="ddqTotals-${reqId}" style="display:flex;gap:16px;font-size:11px;padding:8px 0;border-top:1px solid var(--border)">
                <div>Cost: <strong>$${Number(totalCost).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
                <div>Revenue: <strong>$${Number(totalRev).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
                <div>Gross Profit: <strong style="color:var(--green)">$${Number(gp).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
                <div>Margin: <strong>${totalMargin.toFixed(1)}%</strong></div>
            </div>`;
        } else {
            html += `<div style="text-align:right;font-size:12px;font-weight:700;padding:8px 0;border-top:1px solid var(--border)">
                Subtotal: $${Number(totalRev).toLocaleString(undefined,{minimumFractionDigits:2})}
            </div>`;
        }
    }

    // ── Terms ──
    if (isDraft) {
        html += `<div style="display:flex;gap:14px;font-size:11px;margin:8px 0;flex-wrap:wrap;align-items:end">
            <label>Payment ${_termsSelectHtml('ddqTerms-' + reqId, q.payment_terms || '', _PAYMENT_TERMS)}</label>
            <input id="ddqTermsCustom-${reqId}" style="width:90px;padding:2px 4px;display:none" placeholder="Custom">
            <label>Shipping ${_termsSelectHtml('ddqShip-' + reqId, q.shipping_terms || '', _SHIPPING_TERMS)}</label>
            <input id="ddqShipCustom-${reqId}" style="width:90px;padding:2px 4px;display:none" placeholder="Custom">
            <label>Valid <input id="ddqValid-${reqId}" type="number" value="${q.validity_days||7}" style="width:50px;padding:2px 4px"> days</label>
        </div>
        <div style="margin:4px 0"><label style="font-size:11px">Notes<br><textarea id="ddqNotes-${reqId}" rows="2" style="width:100%;font-size:11px;padding:4px">${esc(q.notes||'')}</textarea></label></div>`;
    } else {
        const termParts = [];
        if (q.payment_terms) termParts.push(esc(q.payment_terms));
        if (q.shipping_terms) termParts.push(esc(q.shipping_terms));
        termParts.push('Valid ' + (q.validity_days || 7) + ' days');
        html += `<div style="font-size:11px;color:var(--text2);margin:8px 0;padding:6px 0;border-top:1px solid var(--border)"><strong>Terms:</strong> ${termParts.join(' \u00b7 ')}</div>`;
        if (q.notes) html += `<div style="font-size:11px;color:var(--text2);background:var(--bg2);padding:6px 10px;border-radius:6px;margin-bottom:8px">${esc(q.notes)}</div>`;
    }

    // ── Actions by status ──
    const statusActions = {
        draft: `<button class="btn btn-ghost btn-sm" onclick="ddSaveQuoteDraft(${reqId})">Save Draft</button> <button class="btn btn-primary btn-sm" onclick="ddSendQuote(${reqId})">Send Quote</button> <button class="btn btn-danger btn-sm" onclick="ddDeleteQuote(${reqId},${q.id})">Delete</button>`,
        sent: `<button class="btn btn-success btn-sm" onclick="ddMarkQuoteResult(${reqId},'won')">Mark Won</button> <button class="btn btn-danger btn-sm" onclick="ddMarkQuoteResult(${reqId},'lost')">Mark Lost</button> <button class="btn btn-ghost btn-sm" onclick="ddReviseQuote(${reqId})">Revise</button>`,
        won: `<span style="color:var(--green);font-weight:600;font-size:11px">Won${q.won_revenue ? ' \u2014 $' + Number(q.won_revenue).toLocaleString() : ''}</span>`,
        lost: `<span style="color:var(--red);font-weight:600;font-size:11px">Lost${q.result_reason ? ' \u2014 ' + esc(q.result_reason) : ''}</span> <button class="btn btn-ghost btn-sm" onclick="ddReviseQuote(${reqId})">Revise</button>`,
        revised: `<span style="font-size:11px;color:var(--muted)">Superseded by Rev ${(q.revision||0)+1}</span>`,
    };
    html += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center">${statusActions[q.status] || statusActions.draft}</div>`;

    html += `</div>`; // close content
    html += `</div>`; // close outer container
    container.innerHTML = html;

    // Wire up payment/shipping selects for draft quotes
    if (isDraft) {
        _wireTermsSelect('ddqTerms-' + reqId, 'ddqTermsCustom-' + reqId);
        _wireTermsSelect('ddqShip-' + reqId, 'ddqShipCustom-' + reqId);
    }
}

// ── Drill-down quote editing helpers ──────────────────────────────────

function ddUpdateQuoteField(reqId, idx, field, value) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const lines = q.lines || q.line_items || [];
    if (lines[idx]) lines[idx][field] = value;
}

function ddUpdateQuoteLine(reqId, idx, value) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const lines = q.lines || q.line_items || [];
    const item = lines[idx];
    if (!item) return;
    item.sell_price = parseFloat(value) || 0;
    const cost = item.cost_price || item.unit_cost || 0;
    item.margin_pct = window.calcMarginPct ? window.calcMarginPct(item.sell_price, cost) : (item.sell_price > 0 ? ((item.sell_price - cost) / item.sell_price * 100) : 0);
    // Update margin cell
    const mCell = document.querySelector(`.ddq-margin[data-req="${reqId}"][data-idx="${idx}"]`);
    if (mCell) {
        mCell.textContent = item.margin_pct.toFixed(1) + '%';
        mCell.style.color = item.margin_pct >= 20 ? 'var(--green)' : item.margin_pct >= 10 ? 'var(--amber)' : 'var(--red)';
    }
    _ddRefreshQuoteTotals(reqId);
}

function ddApplyQuoteMarkup(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const pct = parseFloat(document.getElementById('ddQuoteMarkup-' + reqId)?.value) || 0;
    const lines = q.lines || q.line_items || [];
    lines.forEach(item => {
        const cost = item.cost_price || item.unit_cost || 0;
        item.sell_price = pct >= 100 ? 0 : Math.round(cost / (1 - pct / 100) * 100) / 100;
        item.margin_pct = window.calcMarginPct ? window.calcMarginPct(item.sell_price, cost) : (item.sell_price > 0 ? ((item.sell_price - cost) / item.sell_price * 100) : 0);
    });
    // Re-render the expanded detail panel
    const detailRow = document.getElementById('ddqDetail-' + q.id);
    if (detailRow) {
        const cell = detailRow.querySelector('td');
        if (cell) _renderQuoteDetail(reqId, q, cell);
    }
}

function _ddRefreshQuoteTotals(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const lines = q.lines || q.line_items || [];
    let totalCost = 0, totalRev = 0;
    for (const l of lines) {
        const cost = l.cost_price || l.unit_cost || 0;
        const sell = l.sell_price || l.unit_sell || 0;
        const qty = l.qty || 0;
        totalCost += cost * qty;
        totalRev += sell * qty;
    }
    const gp = totalRev - totalCost;
    const margin = totalRev > 0 ? ((totalRev - totalCost) / totalRev * 100) : 0;
    const el = document.getElementById('ddqTotals-' + reqId);
    if (el) {
        el.innerHTML = `
            <div>Cost: <strong>$${Number(totalCost).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
            <div>Revenue: <strong>$${Number(totalRev).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
            <div>Profit: <strong style="color:var(--green)">$${Number(gp).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
            <div>Margin: <strong>${margin.toFixed(1)}%</strong></div>`;
    }
}

async function ddSaveQuoteDraft(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    if (ddSaveQuoteDraft._busy) return;
    ddSaveQuoteDraft._busy = true;
    try {
        await apiFetch('/api/quotes/' + q.id, {
            method: 'PUT', body: {
                line_items: q.lines || q.line_items,
                payment_terms: _getTermsValue('ddqTerms-' + reqId, 'ddqTermsCustom-' + reqId),
                shipping_terms: _getTermsValue('ddqShip-' + reqId, 'ddqShipCustom-' + reqId),
                validity_days: parseInt(document.getElementById('ddqValid-' + reqId)?.value) || 7,
                notes: document.getElementById('ddqNotes-' + reqId)?.value || '',
            }
        });
        showToast('Draft saved — you can continue editing anytime', 'success');
        // Collapse detail and refresh the list to show updated data
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
        const drow = document.getElementById('d-' + reqId);
        if (drow) {
            const panel = drow.querySelector('.dd-panel');
            if (panel) await _loadDdSubTab(reqId, 'quotes', panel);
        }
    } catch (e) {
        showToast('Couldn\'t save draft — ' + friendlyError(e, 'please try again'), 'error');
    } finally {
        ddSaveQuoteDraft._busy = false;
    }
}

async function ddSendQuote(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;

    // Persist latest edits before opening the send dialog
    try {
        await apiFetch('/api/quotes/' + q.id, {
            method: 'PUT', body: {
                line_items: q.lines || q.line_items,
                payment_terms: _getTermsValue('ddqTerms-' + reqId, 'ddqTermsCustom-' + reqId) || q.payment_terms || '',
                shipping_terms: _getTermsValue('ddqShip-' + reqId, 'ddqShipCustom-' + reqId) || q.shipping_terms || '',
                validity_days: parseInt(document.getElementById('ddqValid-' + reqId)?.value) || q.validity_days || 7,
                notes: document.getElementById('ddqNotes-' + reqId)?.value || q.notes || '',
            }
        });
    } catch (e) {
        showToast('Couldn\'t save quote — ' + friendlyError(e, 'please try again'), 'error');
        return;
    }

    const prefillEmail = q.contact_email || '';
    const prefillName = q.contact_name || '';
    const senderEmail = window.userEmail || 'your account';
    // Build contact options from site_contacts, auto-select matching or first
    let contactOpts = '';
    let autoSelectedEmail = prefillEmail;
    if (q.site_contacts && q.site_contacts.length) {
        for (const c of q.site_contacts) {
            if (!c.email) continue;
            const selected = (prefillEmail && c.email.toLowerCase() === prefillEmail.toLowerCase()) ? ' selected' : '';
            contactOpts += `<option value="${escAttr(c.email)}" data-name="${escAttr(c.full_name||'')}"${selected}>${esc(c.full_name || c.email)}${c.title ? ' (' + esc(c.title) + ')' : ''}</option>`;
        }
        // If no prefill match, auto-select first contact
        if (!prefillEmail && q.site_contacts[0]?.email) {
            autoSelectedEmail = q.site_contacts[0].email;
            contactOpts = contactOpts.replace('<option value="' + escAttr(autoSelectedEmail) + '"', '<option value="' + escAttr(autoSelectedEmail) + '" selected');
        }
    }
    const html = `<div class="modal-bg open" id="ddSendQuoteBg" onclick="if(event.target===this){this.remove()}">
        <div class="modal" onclick="event.stopPropagation()" style="max-width:860px">
            <h2 style="font-size:14px;margin-bottom:10px">Send Quote ${esc(q.quote_number || '')}</h2>
            <div style="display:flex;gap:16px;flex-wrap:wrap">
                <!-- Left: send options -->
                <div style="flex:0 0 300px;min-width:260px">
                    <div style="font-size:11px;color:var(--muted);margin-bottom:8px;padding:6px 8px;background:var(--bg2);border-radius:4px">
                        From: <strong>${esc(senderEmail)}</strong>
                    </div>
                    <div style="margin-bottom:8px">
                        <label style="font-size:11px;font-weight:600">Send To</label>
                        <select id="ddSendContact-${reqId}" style="width:100%;padding:5px;font-size:12px;margin-top:2px" onchange="ddOnContactSelect(${reqId})">
                            <option value="">-- Choose recipient --</option>
                            ${contactOpts}
                            <option value="__add_new__">+ Add New Contact\u2026</option>
                        </select>
                    </div>
                    <!-- Add new contact form (hidden by default) -->
                    <div id="ddNewContactForm-${reqId}" style="display:none;margin-bottom:8px;padding:8px;background:var(--bg2);border-radius:6px;border:1px solid var(--border)">
                        <div style="font-size:11px;font-weight:600;margin-bottom:6px">New Contact</div>
                        <div style="margin-bottom:4px"><input id="ddNewEmail-${reqId}" type="email" placeholder="Email *" style="width:100%;padding:4px;font-size:11px"></div>
                        <div style="margin-bottom:4px"><input id="ddNewName-${reqId}" placeholder="Full name" style="width:100%;padding:4px;font-size:11px"></div>
                        <div style="margin-bottom:4px"><input id="ddNewTitle-${reqId}" placeholder="Title (e.g. Purchasing Manager)" style="width:100%;padding:4px;font-size:11px"></div>
                        <div style="margin-bottom:4px"><input id="ddNewPhone-${reqId}" placeholder="Phone" style="width:100%;padding:4px;font-size:11px"></div>
                        <div style="display:flex;gap:6px;align-items:center">
                            <button class="btn btn-primary btn-sm" onclick="ddAddNewContact(${reqId})" id="ddAddContactBtn-${reqId}">Add &amp; Select</button>
                            <button class="btn btn-ghost btn-sm" onclick="ddFindContacts(${reqId})">Enrich</button>
                            <span id="ddNewContactStatus-${reqId}" style="font-size:10px;color:var(--muted)"></span>
                        </div>
                        <!-- Enrichment results -->
                        <div id="ddEnrichResults-${reqId}" style="display:none;margin-top:6px;max-height:120px;overflow-y:auto"></div>
                    </div>
                    <div style="margin-bottom:6px"><label style="font-size:11px;font-weight:600">To (Email) *</label><input id="ddSendEmail-${reqId}" type="email" value="${escAttr(prefillEmail)}" placeholder="customer@example.com" style="width:100%;padding:5px;font-size:12px;margin-top:2px" required></div>
                    <div style="margin-bottom:12px"><label style="font-size:11px;font-weight:600">To (Name)</label><input id="ddSendName-${reqId}" value="${escAttr(prefillName)}" placeholder="Contact name" style="width:100%;padding:5px;font-size:12px;margin-top:2px" onblur="ddRefreshPreview(${reqId})"></div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary btn-sm" id="ddSendConfirmBtn-${reqId}" onclick="ddConfirmSendQuote(${reqId})">Send Quote</button>
                        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('ddSendQuoteBg').remove()">Cancel</button>
                    </div>
                    <p style="font-size:10px;color:var(--muted);margin-top:8px">Once sent, this quote will be locked and no longer editable.</p>
                </div>
                <!-- Right: email preview -->
                <div id="ddSendPreview-${reqId}" style="flex:1;min-width:320px;border:1px solid var(--border);border-radius:6px;overflow:auto;max-height:520px;background:var(--bg2);font-size:11px;color:var(--muted);display:flex;align-items:center;justify-content:center;padding:20px">
                    Loading preview\u2026
                </div>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
    // Auto-populate from selected contact (if auto-selected)
    const _autoSel = document.getElementById('ddSendContact-' + reqId);
    if (_autoSel && _autoSel.value && _autoSel.value !== '' && _autoSel.value !== '__add_new__') {
        ddOnContactSelect(reqId);
    }
    // Auto-load preview
    ddRefreshPreview(reqId);
}

function ddOnContactSelect(reqId) {
    const sel = document.getElementById('ddSendContact-' + reqId);
    if (!sel) return;
    const val = sel.value;
    const form = document.getElementById('ddNewContactForm-' + reqId);
    if (val === '__add_new__') {
        if (form) form.style.display = 'block';
        const emailEl = document.getElementById('ddSendEmail-' + reqId); if (emailEl) emailEl.value = '';
        const nameEl = document.getElementById('ddSendName-' + reqId); if (nameEl) nameEl.value = '';
        document.getElementById('ddNewEmail-' + reqId)?.focus();
        return;
    }
    if (form) form.style.display = 'none';
    const opt = sel.options[sel.selectedIndex];
    const emailEl2 = document.getElementById('ddSendEmail-' + reqId); if (emailEl2) emailEl2.value = opt.value;
    const nameEl2 = document.getElementById('ddSendName-' + reqId); if (nameEl2) nameEl2.value = opt.dataset.name || '';
    ddRefreshPreview(reqId);
}

async function ddAddNewContact(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q || !q.customer_site_id) return;
    const email = (document.getElementById('ddNewEmail-' + reqId)?.value || '').trim().toLowerCase();
    const name = (document.getElementById('ddNewName-' + reqId)?.value || '').trim();
    const title = (document.getElementById('ddNewTitle-' + reqId)?.value || '').trim();
    const phone = (document.getElementById('ddNewPhone-' + reqId)?.value || '').trim();
    if (!email || !email.includes('@')) { showToast('Enter a valid email address', 'error'); return; }
    const btn = document.getElementById('ddAddContactBtn-' + reqId);
    const status = document.getElementById('ddNewContactStatus-' + reqId);
    if (btn) { btn.disabled = true; btn.textContent = 'Adding\u2026'; }
    try {
        // Create site contact
        await apiFetch('/api/sites/' + q.customer_site_id + '/contacts', {
            method: 'POST', body: { full_name: name || email.split('@')[0], email, title, phone, is_primary: true }
        });
        // Add to the dropdown and select it
        const sel = document.getElementById('ddSendContact-' + reqId);
        if (sel) {
            const opt = document.createElement('option');
            opt.value = email;
            opt.dataset.name = name;
            opt.textContent = (name || email) + (title ? ' (' + title + ')' : '');
            sel.insertBefore(opt, sel.querySelector('option[value="__add_new__"]'));
            sel.value = email;
        }
        const seEl = document.getElementById('ddSendEmail-' + reqId); if (seEl) seEl.value = email;
        const snEl = document.getElementById('ddSendName-' + reqId); if (snEl) snEl.value = name;
        const ncfEl = document.getElementById('ddNewContactForm-' + reqId); if (ncfEl) ncfEl.style.display = 'none';
        // Also update the cached quote data
        if (!q.site_contacts) q.site_contacts = [];
        q.site_contacts.push({ email, full_name: name, title, is_primary: true });
        showToast('Contact added: ' + (name || email), 'success');
        ddRefreshPreview(reqId);
        // Try to enrich in the background
        ddEnrichNewContact(reqId, email, name);
    } catch (e) {
        showToast('Couldn\'t add contact — ' + friendlyError(e, 'please try again'), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Add & Select'; }
    }
}

async function ddEnrichNewContact(reqId, email, name) {
    // Extract domain from email and try enrichment
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const domain = q.company_domain || email.split('@')[1];
    if (!domain) return;
    try {
        const resp = await apiFetch('/api/suggested-contacts?domain=' + encodeURIComponent(domain) + '&name=' + encodeURIComponent(q.company_name_short || ''));
        if (resp.contacts && resp.contacts.length) {
            // Find matching contact by email
            const match = resp.contacts.find(c => c.email?.toLowerCase() === email.toLowerCase());
            if (match && q.customer_site_id) {
                // Update the site contact with enriched data
                await apiFetch('/api/suggested-contacts/add-to-site', {
                    method: 'POST', body: {
                        site_id: q.customer_site_id,
                        contact: { full_name: match.full_name || name, email, phone: match.phone, title: match.title, linkedin_url: match.linkedin_url }
                    }
                });
                // Update name in the send modal if enrichment found a better name
                if (match.full_name && !name) {
                    const snEl = document.getElementById('ddSendName-' + reqId); if (snEl) snEl.value = match.full_name;
                    const sel = document.getElementById('ddSendContact-' + reqId);
                    if (sel) {
                        for (const opt of sel.options) {
                            if (opt.value === email) { opt.textContent = match.full_name + (match.title ? ' (' + match.title + ')' : ''); opt.dataset.name = match.full_name; break; }
                        }
                    }
                    ddRefreshPreview(reqId);
                }
            }
        }
    } catch (e) { /* enrichment is best-effort */ }
}

async function ddFindContacts(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const domain = q.company_domain || (document.getElementById('ddNewEmail-' + reqId)?.value || '').split('@')[1];
    if (!domain) { showToast('No domain available \u2014 enter an email first', 'error'); return; }
    const resultsEl = document.getElementById('ddEnrichResults-' + reqId);
    const status = document.getElementById('ddNewContactStatus-' + reqId);
    if (status) status.textContent = 'Searching\u2026';
    if (resultsEl) { resultsEl.style.display = 'block'; resultsEl.innerHTML = '<div style="font-size:10px;color:var(--muted);padding:4px">Looking up contacts at ' + esc(domain) + '\u2026</div>'; }
    try {
        const resp = await apiFetch('/api/suggested-contacts?domain=' + encodeURIComponent(domain) + '&name=' + encodeURIComponent(q.company_name_short || ''));
        if (status) status.textContent = '';
        if (!resp.contacts || !resp.contacts.length) {
            if (resultsEl) resultsEl.innerHTML = '<div style="font-size:10px;color:var(--muted);padding:4px">No contacts found at ' + esc(domain) + '</div>';
            return;
        }
        let html = '';
        for (const c of resp.contacts) {
            html += `<div style="padding:4px 6px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;cursor:pointer;font-size:10px" onclick="ddPickEnrichedContact(${reqId},this)" data-email="${escAttr(c.email||'')}" data-name="${escAttr(c.full_name||'')}" data-title="${escAttr(c.title||'')}" data-phone="${escAttr(c.phone||'')}" data-linkedin="${escAttr(c.linkedin_url||'')}">
                <div>
                    <strong>${esc(c.full_name || c.email)}</strong>${c.title ? ' <span style="color:var(--muted)">' + esc(c.title) + '</span>' : ''}
                    <div style="color:var(--muted)">${esc(c.email||'')}${c.phone ? ' \u00b7 ' + esc(c.phone) : ''}</div>
                </div>
                <span style="color:var(--teal);font-size:9px">${esc(c.source || '')}</span>
            </div>`;
        }
        if (resultsEl) resultsEl.innerHTML = html;
    } catch (e) {
        if (status) status.textContent = '';
        if (resultsEl) resultsEl.innerHTML = '<div style="font-size:10px;color:var(--red);padding:4px">' + esc(e.message || 'Enrichment failed') + '</div>';
    }
}

function ddPickEnrichedContact(reqId, el) {
    const email = el.dataset.email;
    const name = el.dataset.name;
    const title = el.dataset.title;
    const phone = el.dataset.phone;
    const _s = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
    _s('ddNewEmail-' + reqId, email); _s('ddNewName-' + reqId, name);
    _s('ddNewTitle-' + reqId, title); _s('ddNewPhone-' + reqId, phone);
    const erEl = document.getElementById('ddEnrichResults-' + reqId); if (erEl) erEl.style.display = 'none';
}

function ddRefreshPreview(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const el = document.getElementById('ddSendPreview-' + reqId);
    if (!el) return;
    const toName = (document.getElementById('ddSendName-' + reqId)?.value || '').trim() || q.contact_name || '';
    const lines = q.lines || q.line_items || [];
    const validity = q.validity_days || 7;
    const now = new Date();
    const expires = new Date(now.getTime() + validity * 86400000);
    const fmtDate = d => d.toLocaleDateString('en-US', {month:'long',day:'numeric',year:'numeric'});
    let subtotal = 0;

    let rows = '';
    for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        const sell = l.sell_price || l.unit_sell || 0;
        const qty = l.qty || 0;
        const ext = sell * qty;
        subtotal += ext;
        rows += `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:6px 8px;font-weight:600" class="mono">${esc(l.mpn || '')}</td>
            <td style="padding:6px 8px;font-size:10px">${esc(l.manufacturer || '\u2014')}</td>
            <td style="padding:6px 8px;text-align:center" class="mono">${qty.toLocaleString()}</td>
            <td style="padding:6px 8px">${esc(l.condition || '\u2014')}</td>
            <td style="padding:6px 8px">${esc(l.date_code || '\u2014')}</td>
            <td style="padding:6px 8px">${esc(l.packaging || '\u2014')}</td>
            <td style="padding:6px 8px;text-align:right" class="mono">${fmtPrice(sell)}</td>
            <td style="padding:6px 8px;text-align:right">${fmtLead(l.lead_time)}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:600" class="mono">$${Number(ext).toLocaleString(undefined,{minimumFractionDigits:2})}</td>
        </tr>`;
    }

    const greeting = toName ? 'Dear ' + esc(toName) + ',' : 'Dear Valued Customer,';

    el.style.display = 'block';
    el.style.padding = '0';
    el.innerHTML = `<div style="font-family:var(--font);font-size:12px;color:var(--text);background:#fff;border-radius:8px;overflow:hidden;border:2px solid var(--blue);box-shadow:var(--shadow-sm)">
        <!-- Logo -->
        <!-- Header -->
        <div style="padding:20px 20px 16px">
            <img src="/static/trio_logo.png" alt="TRIO" style="height:44px">
        </div>
        <div style="margin:0 20px;height:1px;background:var(--border)"></div>
        <!-- Quote info -->
        <div style="padding:16px 20px;display:flex;justify-content:space-between;align-items:flex-start">
            <div>
                <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#127fbf;margin-bottom:4px">Quotation</div>
                <div style="font-size:15px;font-weight:700;color:var(--text)">${esc(q.quote_number || '')}</div>
                <div style="font-size:10px;color:var(--muted);margin-top:2px">Rev ${q.revision || 1} &middot; ${fmtDate(now)}</div>
            </div>
            <div style="text-align:right">
                <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#127fbf;margin-bottom:4px">Prepared For</div>
                <div style="font-size:12px;font-weight:600">${esc(q.customer_name || '')}</div>
                ${toName ? `<div style="font-size:10px;color:var(--muted)">${esc(toName)}</div>` : ''}
            </div>
        </div>
        <!-- Body -->
        <div style="padding:0 20px 16px">
            <p style="margin:0 0 4px;font-size:13px">${greeting}</p>
            <p style="margin:0 0 16px;font-size:11px;color:var(--muted);line-height:1.5">Thank you for your interest. Please find our quotation detailed below.</p>
            <!-- Line items -->
            <table class="tbl" style="width:100%;font-size:11px;border-collapse:collapse;margin-bottom:4px;border:1px solid var(--border);border-radius:4px">
                <thead><tr style="background:#F3F5F7">
                    <th style="padding:7px 8px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Part #</th>
                    <th style="padding:7px 8px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Mfr</th>
                    <th style="padding:7px 8px;text-align:center;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Qty</th>
                    <th style="padding:7px 8px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Cond</th>
                    <th style="padding:7px 8px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">DC</th>
                    <th style="padding:7px 8px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Pkg</th>
                    <th style="padding:7px 8px;text-align:right;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Unit Price</th>
                    <th style="padding:7px 8px;text-align:right;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Lead</th>
                    <th style="padding:7px 8px;text-align:right;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #127fbf;color:var(--text2)">Ext. Price</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <!-- Total -->
            <div style="display:flex;justify-content:flex-end;align-items:baseline;gap:12px;padding:12px 0;margin-bottom:16px">
                <span style="font-size:11px;color:var(--muted)">Subtotal</span>
                <span style="font-size:16px;font-weight:700;color:var(--blue);font-family:'JetBrains Mono',Consolas,monospace;border-bottom:3px solid #127fbf;padding-bottom:4px">$${Number(subtotal).toLocaleString(undefined,{minimumFractionDigits:2})}</span>
            </div>
            <!-- Terms -->
            <div style="background:#FBFBFC;border:1px solid var(--border);border-radius:4px;padding:10px 14px;margin-bottom:12px">
                <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--blue);margin-bottom:6px">Terms</div>
                <table style="font-size:11px;width:100%">
                    ${q.payment_terms ? `<tr><td style="padding:3px 0;color:var(--muted);width:80px">Payment</td><td style="padding:3px 0;font-weight:600">${esc(q.payment_terms)}</td></tr>` : ''}
                    ${q.shipping_terms ? `<tr><td style="padding:3px 0;color:var(--muted)">Shipping</td><td style="padding:3px 0;font-weight:600">${esc(q.shipping_terms)}</td></tr>` : ''}
                    <tr><td style="padding:3px 0;color:var(--muted)">Currency</td><td style="padding:3px 0;font-weight:600">USD</td></tr>
                    <tr><td style="padding:3px 0;color:var(--muted)">Valid Until</td><td style="padding:3px 0;font-weight:600">${fmtDate(expires)}</td></tr>
                </table>
            </div>
            ${q.notes ? `<div style="padding:8px 12px;background:#F3F5F7;border-left:3px solid #127fbf;border-radius:4px;font-size:11px;color:var(--text2)">${esc(q.notes)}</div>` : ''}
        </div>
        <!-- Footer -->
        <div style="border-top:2px solid var(--blue)"></div>
        <div style="background:#282c30;padding:8px 20px;display:flex;justify-content:space-between;align-items:center;border-radius:0 0 7px 7px">
            <span style="font-size:10px;color:#8899aa;font-weight:600">Trio Supply Chain Solutions</span>
            <span style="font-size:10px;color:#127fbf;font-weight:600">trioscs.com</span>
        </div>
    </div>`;
}

async function ddConfirmSendQuote(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const toEmail = (document.getElementById('ddSendEmail-' + reqId)?.value || '').trim();
    const toName = (document.getElementById('ddSendName-' + reqId)?.value || '').trim();
    if (!toEmail) { showToast('Recipient email is required', 'error'); return; }
    if (!toEmail.includes('@') || !toEmail.includes('.')) { showToast('Enter a valid email address (e.g. name@company.com)', 'error'); return; }
    const btn = document.getElementById('ddSendConfirmBtn-' + reqId);
    if (btn) { btn.disabled = true; btn.textContent = 'Sending\u2026'; }
    try {
        const sendResult = await apiFetch('/api/quotes/' + q.id + '/send', {
            method: 'POST', body: { to_email: toEmail, to_name: toName }
        });
        document.getElementById('ddSendQuoteBg')?.remove();
        showToast('Quote sent to ' + toEmail, 'success');
        // Update requisition status badge to "Quoted"
        notifyStatusChange(sendResult);
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) { reqInfo.quote_status = 'sent'; reqInfo.quote_sent_at = new Date().toISOString(); }
        _refreshReqRow(reqId);
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
        const drow = document.getElementById('d-' + reqId);
        if (drow) {
            const panel = drow.querySelector('.dd-panel');
            if (panel) await _loadDdSubTab(reqId, 'quotes', panel);
        }
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'Send Quote'; }
        showToast('Couldn\'t send — ' + friendlyError(e, 'please try again'), 'error');
    }
}

async function ddMarkQuoteResult(reqId, result) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    if (result === 'won') { ddOpenBuyPlanModal(reqId); return; }
    if (result === 'lost') {
        promptInput('Mark as Lost', 'Reason for loss (optional):', async function(reason) {
            reason = reason || '';
            try {
                const markResult = await apiFetch('/api/quotes/' + q.id + '/result', {
                    method: 'POST', body: { result, reason }
                });
                notifyStatusChange(markResult);
                const reqInfo = _reqListData.find(r => r.id === reqId);
                if (reqInfo) reqInfo.quote_status = result;
                _refreshReqRow(reqId);
                if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
                const drow = document.getElementById('d-' + reqId);
                if (drow) {
                    const panel = drow.querySelector('.dd-panel');
                    if (panel) await _loadDdSubTab(reqId, 'quotes', panel);
                }
                if (window._refreshCustPipeline) window._refreshCustPipeline();
                showToast('Quote marked as lost', 'success', { duration: 8000, action: { label: 'Undo', fn: async () => {
                    try {
                        await apiFetch('/api/quotes/' + q.id + '/result', {
                            method: 'POST', body: { result: 'sent', reason: '' }
                        });
                        showToast('Reverted to sent', 'success');
                        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
                        loadRequisitions();
                    } catch (ue) { showToast('Undo failed — ' + friendlyError(ue), 'error'); }
                }}});
            } catch (e) {
                showToast(friendlyError(e, 'Something went wrong — please try again'), 'error');
            }
        }, {submitLabel: 'Mark Lost', placeholder: 'Enter reason...'});
        return;
    }
    try {
        const markResult = await apiFetch('/api/quotes/' + q.id + '/result', {
            method: 'POST', body: { result, reason: '' }
        });
        showToast('Quote marked as ' + result, 'success');
        notifyStatusChange(markResult);
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) reqInfo.quote_status = result;
        _refreshReqRow(reqId);
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
        const drow = document.getElementById('d-' + reqId);
        if (drow) {
            const panel = drow.querySelector('.dd-panel');
            if (panel) await _loadDdSubTab(reqId, 'quotes', panel);
        }
        if (window._refreshCustPipeline) window._refreshCustPipeline();
    } catch (e) {
        showToast(friendlyError(e, 'Something went wrong — please try again'), 'error');
    }
}

// ── Buy Plan (drill-down) ──────��──────────────────────────────────────

function ddOpenBuyPlanModal(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const lines = q.lines || q.line_items || [];
    const rows = lines.map((item, i) => {
        const qty = item.qty || 0;
        const cost = Number(item.cost_price || 0);
        return `<tr>
            <td style="text-align:center"><input type="checkbox" class="dd-bp-check" data-idx="${i}" checked></td>
            <td style="font-weight:600">${esc(item.mpn || '')}</td>
            <td>${esc(item.manufacturer || '\u2014')}</td>
            <td>${esc(item.vendor_name || '\u2014')}</td>
            <td style="text-align:right">${qty.toLocaleString()}</td>
            <td style="text-align:right"><input type="number" class="dd-bp-qty" data-idx="${i}" value="${qty}" min="1" style="width:70px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px;text-align:right" oninput="ddUpdateBpTotals(${reqId})"></td>
            <td style="text-align:right">${fmtPrice(cost)}</td>
            <td style="text-align:right" class="dd-bp-line-total">${fmtPrice(qty * cost)}</td>
            <td>${fmtLead(item.lead_time)}</td>
        </tr>`;
    }).join('');

    // Build modal overlay
    let existing = document.getElementById('ddBuyPlanOverlay');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.id = 'ddBuyPlanOverlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    overlay.innerHTML = `<div style="background:var(--white);border-radius:10px;width:90%;max-width:820px;max-height:85vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.2);padding:24px">
        <h2 style="margin:0 0 4px;font-size:16px">Mark Won & Submit Buy Plan</h2>
        <p style="font-size:11px;color:var(--muted);margin:0 0 16px">Select items and set the quantity to purchase from each vendor. This will be sent to management for approval.</p>
        <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
        <table class="tbl" style="font-size:11px;width:100%;margin-bottom:12px">
            <thead><tr>
                <th style="width:30px"></th><th>MPN</th><th>Mfr</th><th>Vendor</th><th style="text-align:right">Avail</th><th style="text-align:right">Plan Qty</th><th style="text-align:right">Unit Cost</th><th style="text-align:right">Line Total</th><th>Lead</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
        </div>
        <div style="text-align:right;font-weight:600;font-size:13px;margin-bottom:16px">Total Cost: <span id="ddBpTotal" style="color:var(--blue)"></span></div>
        <div style="margin-bottom:16px">
            <label style="font-size:11px;font-weight:600;display:block;margin-bottom:4px">Notes for Manager & Buyers</label>
            <textarea id="ddBpNotes" rows="3" placeholder="Add any context, special instructions, or notes for the purchasing team..." style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input);box-sizing:border-box"></textarea>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-ghost" onclick="document.getElementById('ddBuyPlanOverlay').remove()">Cancel</button>
            <button class="btn btn-ghost" id="ddBpDraftBtn" onclick="ddCreateBuyPlanDraft(${reqId})">Create as draft</button>
            <button class="btn btn-success" id="ddBpSubmitBtn" onclick="ddSubmitBuyPlan(${reqId})">Mark Won & Submit Buy Plan</button>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    ddUpdateBpTotals(reqId);
}

function ddUpdateBpTotals(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const lines = q.lines || q.line_items || [];
    let total = 0;
    document.querySelectorAll('.dd-bp-qty').forEach(input => {
        const idx = parseInt(input.dataset.idx);
        const item = lines[idx];
        if (!item) return;
        const qty = parseInt(input.value) || 0;
        const cost = Number(item.cost_price || 0);
        const lineTotal = qty * cost;
        total += lineTotal;
        const row = input.closest('tr');
        const cell = row.querySelector('.dd-bp-line-total');
        if (cell) cell.textContent = fmtPrice(lineTotal);
    });
    const el = document.getElementById('ddBpTotal');
    if (el) el.textContent = '$' + total.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function _ddBpPayload(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return null;
    const lines = q.lines || q.line_items || [];
    const checks = document.querySelectorAll('.dd-bp-check:checked');
    const selectedIndices = Array.from(checks).map(c => parseInt(c.dataset.idx));
    if (!selectedIndices.length) return null;
    const offerIds = [];
    const planQtys = {};
    selectedIndices.forEach(i => {
        const item = lines[i];
        if (item && item.offer_id) {
            offerIds.push(item.offer_id);
            const qtyInput = document.querySelector('.dd-bp-qty[data-idx="' + i + '"]');
            if (qtyInput) planQtys[item.offer_id] = parseInt(qtyInput.value) || item.qty || 0;
        }
    });
    if (!offerIds.length) return null;
    const notes = (document.getElementById('ddBpNotes')?.value || '').trim();
    return { offer_ids: offerIds, plan_qtys: planQtys, salesperson_notes: notes };
}

async function ddCreateBuyPlanDraft(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const payload = _ddBpPayload(reqId);
    if (!payload) { showToast('Select at least one item', 'error'); return; }
    const btn = document.getElementById('ddBpDraftBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Creating\u2026'; }
    try {
        const res = await apiFetch('/api/quotes/' + q.id + '/buy-plan/draft', { method: 'POST', body: payload });
        showToast('Buy plan created as draft. Go to Buy Plans and click "Ready to send" when ready.', 'success');
        const overlay = document.getElementById('ddBuyPlanOverlay');
        if (overlay) overlay.remove();
        if (typeof window.showBuyPlans === 'function') { window.showBuyPlans(); if (typeof window.loadBuyPlans === 'function') window.loadBuyPlans(); }
    } catch (e) {
        showToast(friendlyError(e, 'Something went wrong — please try again'), 'error');
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Create as draft'; }
}

async function ddSubmitBuyPlan(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    const payload = _ddBpPayload(reqId);
    if (!payload) { showToast('Select at least one item', 'error'); return; }

    const btn = document.getElementById('ddBpSubmitBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting\u2026'; }
    try {
        const res = await apiFetch('/api/quotes/' + q.id + '/buy-plan', {
            method: 'POST', body: payload
        });
        showToast('Buy plan submitted for approval!', 'success');
        const overlay = document.getElementById('ddBuyPlanOverlay');
        if (overlay) overlay.remove();
        notifyStatusChange(res);
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) reqInfo.quote_status = 'won';
        _refreshReqRow(reqId);
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
        const drow = document.getElementById('d-' + reqId);
        if (drow) {
            const panel = drow.querySelector('.dd-panel');
            if (panel) await _loadDdSubTab(reqId, 'quotes', panel);
        }
    } catch (e) {
        showToast(friendlyError(e, 'Something went wrong — please try again'), 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Mark Won & Submit Buy Plan'; }
    }
}

async function ddReviseQuote(reqId) {
    const q = _ddQuoteData[reqId];
    if (!q) return;
    try {
        await apiFetch('/api/quotes/' + q.id + '/revise', { method: 'POST' });
        showToast('New revision created', 'success');
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
        const drow = document.getElementById('d-' + reqId);
        if (drow) {
            const panel = drow.querySelector('.dd-panel');
            if (panel) await _loadDdSubTab(reqId, 'quotes', panel);
        }
    } catch (e) {
        showToast('Couldn\'t revise — ' + friendlyError(e, 'please try again'), 'error');
    }
}

async function ddDeleteQuote(reqId, quoteId) {
    confirmAction('Delete Draft Quote', 'Delete this draft quote? This cannot be undone.', async function() {
        try {
            await apiFetch('/api/quotes/' + quoteId, { method: 'DELETE' });
            showToast('Draft deleted', 'success');
            if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
            const drow = document.getElementById('d-' + reqId);
            if (drow) {
                const panel = drow.querySelector('.dd-panel');
                if (panel) await _loadDdSubTab(reqId, 'quotes', panel);
            }
        } catch (e) {
            showToast(friendlyError(e, 'Something went wrong — please try again'), 'error');
        }
    }, {confirmClass: 'btn-danger', confirmLabel: 'Delete'});
}

export async function toggleDrillDown(reqId) {
    // Mobile: open full-screen overlay instead of inline expand
    if (window.__isMobile) {
        _openMobileDrillDown(reqId);
        return;
    }
    const drow = document.getElementById('d-' + reqId);
    const arrow = document.getElementById('a-' + reqId);
    const rrow = drow ? drow.previousElementSibling : null;
    if (!drow) return;
    const opening = !drow.classList.contains('open');
    drow.classList.toggle('open');
    if (arrow) arrow.classList.toggle('open');
    if (rrow) rrow.classList.toggle('expanded', opening);
    _updateDrillToggleLabel();
    // Update URL hash to reflect drill-down state — only when the list view is active,
    // so that expanding a drill-down from a background render doesn't override the current view's hash
    if (_currentViewId === 'view-list') {
        if (opening) {
            try { _pushNav('view-list', reqId); } catch(e) {}
        } else {
            try { _pushNav('view-list'); _lastPushedHash = '#rfqs'; } catch(e) {}
        }
    }
    if (!opening) {
        delete _addRowActive[reqId];
        // Unbind context panel when drill-down closes
        if (_ctxBoundObject && _ctxBoundObject.type === 'requisition' && _ctxBoundObject.id === reqId) {
            unbindContextPanel();
        }
        return;
    }

    // Bind context panel to this requisition
    const reqInfo = _reqListData.find(r => r.id === reqId);
    const ctxLabel = reqInfo ? (reqInfo.customer_display || reqInfo.name || 'Req #' + reqId) : 'Req #' + reqId;
    bindContextPanel('requisition', reqId, ctxLabel);

    // Load default sub-tab
    const defaultTab = _ddActiveTab[reqId] || _ddDefaultTab(_currentMainView);
    _ddActiveTab[reqId] = defaultTab;
    // Update pill active state
    drow.querySelectorAll('.dd-tab').forEach(t => t.classList.toggle('on', t.dataset.tab === defaultTab));
    const panel = drow.querySelector('.dd-panel');
    if (!panel) return;
    // Wire up scroll-end detection so CSS fade-out hint disappears at end
    if (!panel._scrollEndWired) {
        panel.addEventListener('scroll', () => {
            const atEnd = panel.scrollLeft + panel.clientWidth >= panel.scrollWidth - 2;
            panel.classList.toggle('scrolled-end', atEnd);
        });
        panel._scrollEndWired = true;
    }
    await _loadDdSubTab(reqId, defaultTab, panel);
}

// ── Mobile full-screen drill-down ─────────────────────────────────────
function _openMobileDrillDown(reqId) {
    // Close any existing mobile drill-down
    const existing = document.getElementById('mobileDrillDown');
    if (existing) existing.remove();

    const r = _reqListData.find(x => x.id === reqId);
    const cust = r ? (r.customer_display || r.name || 'Req') : 'Req';
    const total = r ? (r.requirement_count || 0) : 0;
    const offers = r ? (r.offer_count || 0) : 0;
    const badgeMap = {draft:'m-chip',active:'m-chip-blue',sourcing:'m-chip-blue',quoted:'m-chip-purple',won:'m-chip-green',lost:'m-chip-red'};
    const bc = badgeMap[r?.status] || 'm-chip';
    const _sl = {draft:'Draft',active:'Sourcing',sourcing:'Sourcing',quoted:'Quoted',won:'Won',lost:'Lost'};

    // Deadline
    let dlBadge = '';
    if (r?.deadline === 'ASAP') dlBadge = '<span class="m-chip m-chip-amber">ASAP</span>';
    else if (r?.deadline) dlBadge = '<span class="m-chip">' + fmtDate(r.deadline) + '</span>';

    // Build tab pills
    const tabs = _ddSubTabs(_currentMainView);
    const defaultTab = _ddActiveTab[reqId] || _ddDefaultTab(_currentMainView);
    _ddActiveTab[reqId] = defaultTab;
    const pillsHtml = tabs.map(t =>
        `<button class="m-tab-pill${t === defaultTab ? ' active' : ''}" data-tab="${t}" onclick="_mobileDdSwitchTab(${reqId},'${t}',this)">${_ddTabLabel(t)}</button>`
    ).join('');

    const overlay = document.createElement('div');
    overlay.id = 'mobileDrillDown';
    overlay.className = 'm-fullscreen';
    overlay.innerHTML = `
        <div class="m-detail-header">
            <button class="m-back-btn" onclick="_closeMobileDrillDown()">&larr;</button>
            <span class="m-detail-title">${esc(cust)}</span>
        </div>
        <div class="m-fullscreen-body">
            <div style="padding:12px 16px">
                <div class="m-card" style="cursor:default;margin-bottom:0">
                    <div class="m-card-header">
                        <span style="font-weight:600;font-size:13px">${esc(r?.name||'')}</span>
                        <span class="m-chip ${bc}">${_sl[r?.status]||r?.status||''}</span>
                    </div>
                    <div class="m-card-body" style="margin-top:6px">
                        <span style="font-size:12px"><b>${total}</b> parts</span>
                        <span style="font-size:12px"><b>${offers}</b> offers</span>
                        ${dlBadge}
                    </div>
                    ${renderStatusStrip([
                        { label: 'Parts', value: total },
                        { label: 'Sourced', value: r ? (r.sourced_count || 0) + '/' + total : '0' },
                        { label: 'RFQs', value: r ? (r.rfq_sent_count || 0) : 0 },
                        { label: 'Offers', value: offers },
                    ])}
                </div>
            </div>
            <div class="m-tabs-scroll" id="mobileDdTabs">${pillsHtml}</div>
            <div id="mobileDdPanel" style="padding:8px 12px">
                <span style="font-size:12px;color:var(--muted)">Loading\u2026</span>
            </div>
        </div>`;
    document.body.appendChild(overlay);

    // Load the default sub-tab content
    const panel = document.getElementById('mobileDdPanel');
    if (panel) _loadDdSubTab(reqId, defaultTab, panel);
}

function _closeMobileDrillDown() {
    const el = document.getElementById('mobileDrillDown');
    if (el) {
        el.style.animation = 'none';
        el.style.transform = 'translateY(100%)';
        el.style.transition = 'transform .2s ease-in';
        setTimeout(() => el.remove(), 200);
    }
}

async function _mobileDdSwitchTab(reqId, tabName, btn) {
    _ddActiveTab[reqId] = tabName;
    const tabs = document.getElementById('mobileDdTabs');
    if (tabs) tabs.querySelectorAll('.m-tab-pill').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
    const panel = document.getElementById('mobileDdPanel');
    if (panel) {
        panel.innerHTML = '<span style="font-size:12px;color:var(--muted)">Loading\u2026</span>';
        await _loadDdSubTab(reqId, tabName, panel);
    }
}

// ── Mobile drill-down card renderers ──────────────────────────────────
// Card-based views optimized for touch on small screens.

function _renderMobilePartsList(parts, reqId, panel) {
    const reqs = Array.isArray(parts) ? parts : [];
    if (!reqs.length) {
        panel.innerHTML = '<div style="text-align:center;padding:24px 0;color:var(--muted);font-size:13px">No parts on this requisition</div>';
        return;
    }
    let html = '<div style="font-size:12px;font-weight:600;margin-bottom:8px">' + reqs.length + ' Part' + (reqs.length !== 1 ? 's' : '') + '</div>';
    for (const r of reqs) {
        const bestPrice = r.best_offer_price != null
            ? '$' + parseFloat(r.best_offer_price).toFixed(4)
            : (r.target_price != null ? '$' + parseFloat(r.target_price).toFixed(2) + ' target' : '');
        const sourceCount = r.offer_count || r.sighting_count || 0;
        const subs = (r.substitutes || []).filter(Boolean);
        html += '<div class="m-card" style="cursor:default;margin-bottom:8px">';
        html += '<div class="m-card-header">';
        html += '<span class="mono" data-mpn="' + escAttr(r.primary_mpn || '') + '" style="font-weight:700;font-size:14px">' + esc(r.primary_mpn || '---') + '</span>';
        html += _reqBadge(r);
        html += '</div>';
        html += '<div class="m-card-body" style="margin-top:6px;gap:12px">';
        html += '<div style="display:flex;flex-direction:column;gap:2px">';
        html += '<span style="font-size:11px;color:var(--muted)">Qty Needed</span>';
        html += '<span style="font-size:13px;font-weight:600">' + (r.target_qty ? Number(r.target_qty).toLocaleString() : '---') + '</span>';
        html += '</div>';
        if (bestPrice) {
            html += '<div style="display:flex;flex-direction:column;gap:2px">';
            html += '<span style="font-size:11px;color:var(--muted)">Best Price</span>';
            html += '<span style="font-size:13px;font-weight:600;color:var(--teal)">' + esc(bestPrice) + '</span>';
            html += '</div>';
        }
        if (sourceCount > 0) {
            html += '<div style="display:flex;flex-direction:column;gap:2px">';
            html += '<span style="font-size:11px;color:var(--muted)">Sources</span>';
            html += '<span style="font-size:13px;font-weight:600">' + sourceCount + '</span>';
            html += '</div>';
        }
        html += '</div>';
        if (r.manufacturer || r.brand) {
            html += '<div style="font-size:11px;color:var(--muted);margin-top:4px">Mfr: ' + esc(r.manufacturer || r.brand) + '</div>';
        }
        if (subs.length) {
            html += '<div style="font-size:11px;color:var(--muted);margin-top:4px">Subs: ' + subs.map(s => '<span class="mono" style="color:var(--blue)">' + esc(s) + '</span>').join(', ') + '</div>';
        }
        if (r.condition && r.condition !== 'Any') {
            html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">Condition: ' + esc(r.condition) + '</div>';
        }
        if (r.notes) {
            html += '<div style="font-size:11px;color:var(--muted);margin-top:4px;border-top:1px solid var(--border);padding-top:4px">' + esc(r.notes) + '</div>';
        }
        html += '</div>';
    }
    panel.innerHTML = html;
}

function _renderMobileOffersList(data, reqId, panel) {
    const groups = data?.groups || data || [];
    let totalOffers = 0;
    if (Array.isArray(groups)) {
        for (const g of groups) totalOffers += (g.offers || []).length;
    }
    if (!totalOffers) {
        panel.innerHTML = '<div style="text-align:center;padding:24px 0;color:var(--muted);font-size:13px">No offers yet</div>';
        return;
    }
    let html = '<div style="font-size:12px;font-weight:600;margin-bottom:8px">' + totalOffers + ' Offer' + (totalOffers !== 1 ? 's' : '') + '</div>';
    const grpArr = Array.isArray(groups) ? groups : [];
    for (const g of grpArr) {
        const offers = (g.offers || []).slice().sort((a, b) => (a.unit_price || 999999) - (b.unit_price || 999999));
        if (!offers.length) continue;
        const reqMpn = g.mpn || g.label || '';
        html += '<div style="margin-bottom:12px">';
        html += '<div style="font-size:12px;font-weight:600;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center">';
        html += '<span class="mono">' + esc(reqMpn) + '</span>';
        html += '<span style="font-size:11px;color:var(--muted)">need ' + (g.target_qty || 0).toLocaleString() + '</span>';
        html += '</div>';
        for (const o of offers) {
            const oid = o.id || o.offer_id;
            const price = o.unit_price != null ? '$' + parseFloat(o.unit_price).toFixed(4) : '---';
            const isPending = o.status === 'pending_review';
            const borderColor = isPending ? 'var(--amber)' : 'var(--border)';
            let priceColor = 'var(--teal)';
            if (g.target_price != null && o.unit_price != null) {
                const pctD = ((o.unit_price - g.target_price) / g.target_price) * 100;
                priceColor = pctD <= 0 ? 'var(--green)' : pctD <= 15 ? 'var(--amber)' : 'var(--red)';
            }
            html += '<div class="m-card" style="cursor:default;margin-bottom:6px;border-left:3px solid ' + borderColor + '">';
            html += '<div class="m-card-header">';
            html += '<span style="font-weight:600;font-size:13px">' + esc(o.vendor_name || 'Unknown') + '</span>';
            if (isPending) html += '<span class="m-chip m-chip-amber" style="font-size:10px">DRAFT</span>';
            html += '</div>';
            html += '<div class="m-card-body" style="margin-top:6px;gap:12px">';
            html += '<div style="display:flex;flex-direction:column;gap:2px">';
            html += '<span style="font-size:11px;color:var(--muted)">Price</span>';
            html += '<span style="font-size:14px;font-weight:700;color:' + priceColor + '">' + price + '</span>';
            html += '</div>';
            html += '<div style="display:flex;flex-direction:column;gap:2px">';
            html += '<span style="font-size:11px;color:var(--muted)">Qty</span>';
            html += '<span style="font-size:13px;font-weight:600">' + (o.qty_available != null ? Number(o.qty_available).toLocaleString() : (o.quantity || '---')) + '</span>';
            html += '</div>';
            if (o.lead_time) {
                html += '<div style="display:flex;flex-direction:column;gap:2px">';
                html += '<span style="font-size:11px;color:var(--muted)">Lead</span>';
                html += '<span style="font-size:13px">' + esc(o.lead_time) + '</span>';
                html += '</div>';
            }
            html += '</div>';
            // Detail rows
            const detailPairs = [];
            if (o.condition) detailPairs.push('Cond: ' + esc(o.condition));
            if (o.date_code) detailPairs.push('DC: ' + esc(o.date_code));
            if (o.packaging) detailPairs.push('Pkg: ' + esc(o.packaging));
            if (o.moq != null) detailPairs.push('MOQ: ' + Number(o.moq).toLocaleString());
            if (o.firmware) detailPairs.push('FW: ' + esc(o.firmware));
            if (o.hardware_code) detailPairs.push('HW: ' + esc(o.hardware_code));
            if (o.warranty) detailPairs.push('Warranty: ' + esc(o.warranty));
            if (o.country_of_origin) detailPairs.push('COO: ' + esc(o.country_of_origin));
            if (o.manufacturer) detailPairs.push('Mfr: ' + esc(o.manufacturer));
            if (o.source) detailPairs.push('Source: ' + esc(o.source));
            if (detailPairs.length) {
                html += '<div style="font-size:11px;color:var(--muted);margin-top:4px;line-height:1.5">';
                html += detailPairs.join(' | ');
                html += '</div>';
            }
            if (o.notes) {
                html += '<div style="font-size:10px;color:var(--muted);margin-top:2px;font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escAttr(o.notes) + '">Note: ' + esc(o.notes) + '</div>';
            }
            // Action chips
            if (isPending) {
                html += '<div style="display:flex;gap:8px;margin-top:8px">';
                html += '<button class="m-chip m-chip-green" style="cursor:pointer;border:none;font-size:12px;padding:6px 16px" onclick="event.stopPropagation();ddApproveOffer(' + reqId + ',' + oid + ')">Accept</button>';
                html += '<button class="m-chip m-chip-red" style="cursor:pointer;border:none;font-size:12px;padding:6px 16px" onclick="event.stopPropagation();ddRejectOffer(' + reqId + ',' + oid + ')">Reject</button>';
                html += '</div>';
            }
            html += '</div>';
        }
        html += '</div>';
    }
    panel.innerHTML = html;
}

function _renderMobileQuotesList(data, reqId, panel) {
    const quotes = Array.isArray(data) ? data : (data && data.id ? [data] : []);
    if (!quotes.length) {
        panel.innerHTML = '<div style="text-align:center;padding:24px 0;color:var(--muted);font-size:13px">No quotes yet</div>';
        return;
    }
    let html = '<div style="font-size:12px;font-weight:600;margin-bottom:8px">' + quotes.length + ' Quote' + (quotes.length !== 1 ? 's' : '') + '</div>';
    const statusColors = {draft:'var(--muted)',sent:'var(--blue)',revised:'var(--amber)',won:'var(--green)',lost:'var(--red)'};
    const statusLabels = {draft:'Draft',sent:'Sent',revised:'Revised',won:'Won',lost:'Lost'};
    for (const q of quotes) {
        const lines = q.line_items || [];
        const subtotal = q.subtotal || lines.reduce((s, l) => s + ((l.sell_price || l.unit_sell || 0) * (l.qty || 0)), 0);
        const marginPct = q.total_margin_pct != null ? q.total_margin_pct : 0;
        const sc = statusColors[q.status] || 'var(--muted)';
        const sl = statusLabels[q.status] || q.status || 'Draft';
        html += '<div class="m-card" style="cursor:default;margin-bottom:8px;border-left:3px solid ' + sc + '">';
        html += '<div class="m-card-header">';
        html += '<span class="mono" style="font-weight:700;font-size:13px">' + esc(q.quote_number || 'Q-' + q.id) + '</span>';
        html += '<span class="m-chip" style="background:' + sc + '20;color:' + sc + ';font-size:10px">' + sl + '</span>';
        html += '</div>';
        html += '<div class="m-card-body" style="margin-top:6px;gap:12px">';
        html += '<div style="display:flex;flex-direction:column;gap:2px">';
        html += '<span style="font-size:11px;color:var(--muted)">Customer</span>';
        html += '<span style="font-size:13px;font-weight:600">' + esc(q.customer_name || '---') + '</span>';
        html += '</div>';
        html += '<div style="display:flex;flex-direction:column;gap:2px">';
        html += '<span style="font-size:11px;color:var(--muted)">Total</span>';
        html += '<span style="font-size:14px;font-weight:700;color:var(--teal)">$' + Number(subtotal).toLocaleString(undefined, {minimumFractionDigits: 2}) + '</span>';
        html += '</div>';
        html += '<div style="display:flex;flex-direction:column;gap:2px">';
        html += '<span style="font-size:11px;color:var(--muted)">Margin</span>';
        html += '<span style="font-size:13px;font-weight:600;color:' + (marginPct >= 20 ? 'var(--green)' : marginPct >= 10 ? 'var(--amber)' : 'var(--red)') + '">' + Number(marginPct).toFixed(1) + '%</span>';
        html += '</div>';
        html += '</div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:4px">';
        html += lines.length + ' line' + (lines.length !== 1 ? 's' : '');
        if (q.revision && q.revision > 1) html += ' | Rev ' + q.revision;
        if (q.created_at) html += ' | ' + fmtRelative(q.created_at);
        html += '</div>';
        html += '</div>';
    }
    panel.innerHTML = html;
}

function _renderMobileBuyPlansList(data, reqId, panel) {
    const plans = Array.isArray(data) ? data : [];
    if (!plans.length) {
        panel.innerHTML = '<div style="text-align:center;padding:24px 0;color:var(--muted);font-size:13px">No buy plans yet</div>';
        return;
    }
    const statusColors = {draft:'var(--muted)',pending:'var(--amber)',approved:'var(--green)',po_entered:'var(--blue)',po_confirmed:'var(--blue)',completed:'var(--green)',rejected:'var(--red)',cancelled:'var(--red)',halted:'var(--amber)'};
    const statusLabels = {draft:'Draft',pending:'Pending',approved:'Approved',po_entered:'PO Entered',po_confirmed:'PO Confirmed',completed:'Completed',rejected:'Rejected',cancelled:'Cancelled',halted:'Halted'};
    let html = '<div style="font-size:12px;font-weight:600;margin-bottom:8px">' + plans.length + ' Buy Plan' + (plans.length !== 1 ? 's' : '') + '</div>';
    for (const bp of plans) {
        const sc = statusColors[bp.status] || 'var(--muted)';
        const sl = statusLabels[bp.status] || bp.status || 'Draft';
        const lines = bp.lines || bp.line_items || [];
        const total = bp.total_cost || lines.reduce((s, l) => s + ((l.unit_price || l.buy_price || 0) * (l.qty || 0)), 0);
        html += '<div class="m-card" style="cursor:default;margin-bottom:8px;border-left:3px solid ' + sc + '">';
        html += '<div class="m-card-header">';
        html += '<span style="font-weight:600;font-size:13px">' + esc(bp.vendor_name || bp.primary_vendor || 'Vendor') + '</span>';
        html += '<span class="m-chip" style="background:' + sc + '20;color:' + sc + ';font-size:10px">' + sl + '</span>';
        html += '</div>';
        html += '<div class="m-card-body" style="margin-top:6px;gap:12px">';
        html += '<div style="display:flex;flex-direction:column;gap:2px">';
        html += '<span style="font-size:11px;color:var(--muted)">Total Cost</span>';
        html += '<span style="font-size:14px;font-weight:700;color:var(--teal)">$' + Number(total).toLocaleString(undefined, {minimumFractionDigits: 2}) + '</span>';
        html += '</div>';
        if (lines.length) {
            html += '<div style="display:flex;flex-direction:column;gap:2px">';
            html += '<span style="font-size:11px;color:var(--muted)">Lines</span>';
            html += '<span style="font-size:13px;font-weight:600">' + lines.length + '</span>';
            html += '</div>';
        }
        if (bp.po_number) {
            html += '<div style="display:flex;flex-direction:column;gap:2px">';
            html += '<span style="font-size:11px;color:var(--muted)">PO #</span>';
            html += '<span class="mono" style="font-size:13px;font-weight:600">' + esc(bp.po_number) + '</span>';
            html += '</div>';
        }
        html += '</div>';
        if (bp.created_at || bp.submitted_by_name) {
            html += '<div style="font-size:11px;color:var(--muted);margin-top:4px">';
            if (bp.submitted_by_name) html += 'By ' + esc(bp.submitted_by_name);
            if (bp.submitted_by_name && bp.created_at) html += ' | ';
            if (bp.created_at) html += fmtRelative(bp.created_at);
            html += '</div>';
        }
        // Submit button for draft plans
        if (bp.status === 'draft' || bp.status === 'pending') {
            html += '<div style="margin-top:8px">';
            if (bp.status === 'draft') {
                html += '<button class="m-chip m-chip-blue" style="cursor:pointer;border:none;font-size:12px;padding:6px 16px;width:100%" onclick="event.stopPropagation();if(typeof openBuyPlanDetailV3===\'function\')openBuyPlanDetailV3(' + bp.id + ')">Open Detail</button>';
            }
            html += '</div>';
        }
        if (bp.status === 'halted' || bp.status === 'cancelled') {
            html += '<div style="margin-top:8px">';
            html += '<button class="btn-sm" style="font-size:11px;width:100%;color:var(--blue);border-color:var(--blue)" onclick="event.stopPropagation();_resubmitBuyPlan(' + bp.id + ')">↻ Resubmit as Draft</button>';
            html += '</div>';
        }
        html += '</div>';
    }
    panel.innerHTML = html;
}

function _renderMobileActivityList(reqId, data, panel) {
    const vendors = (data && data.vendors) ? data.vendors : [];
    if (!vendors.length) {
        panel.innerHTML = '<div style="text-align:center;padding:24px 0;color:var(--muted);font-size:13px">'
            + 'No activity yet'
            + '<div style="margin-top:8px"><button class="m-chip m-chip-blue" style="cursor:pointer;border:none;font-size:12px;padding:6px 16px" '
            + 'onclick="event.stopPropagation();checkForReplies(' + reqId + ',this)">Check for Replies</button></div>'
            + '</div>';
        return;
    }
    // Summary counts
    let totalContacts = 0, totalReplies = 0;
    for (const v of vendors) {
        totalContacts += (v.contacts || []).length;
        totalReplies += (v.responses || []).length;
    }
    let html = '<div style="display:flex;gap:12px;align-items:center;margin-bottom:8px;font-size:12px">';
    html += '<span><b>' + totalContacts + '</b> RFQs sent</span>';
    html += '<span><b>' + totalReplies + '</b> replies</span>';
    html += '<button class="m-chip m-chip-blue" style="cursor:pointer;border:none;font-size:11px;padding:4px 10px;margin-left:auto" onclick="event.stopPropagation();checkForReplies(' + reqId + ',this)">Check Inbox</button>';
    html += '</div>';
    // Timeline cards per vendor
    for (const v of vendors) {
        const contacts = v.contacts || [];
        const responses = v.responses || [];
        const activities = v.activities || [];
        const hasReply = responses.length > 0;
        html += '<div class="m-card" style="cursor:default;margin-bottom:8px;border-left:3px solid ' + (hasReply ? 'var(--green)' : 'var(--amber)') + '">';
        html += '<div class="m-card-header">';
        html += '<span style="font-weight:600;font-size:13px">' + esc(v.vendor_name || 'Vendor') + '</span>';
        if (hasReply) {
            html += '<span class="m-chip m-chip-green" style="font-size:10px">' + responses.length + ' repl' + (responses.length !== 1 ? 'ies' : 'y') + '</span>';
        } else {
            html += '<span class="m-chip m-chip-amber" style="font-size:10px">Awaiting</span>';
        }
        html += '</div>';
        // Contact info
        if (contacts.length) {
            html += '<div style="font-size:11px;color:var(--muted);margin-top:4px">';
            html += contacts.map(c => esc(c.contact_name || c.email || '')).filter(Boolean).join(', ');
            html += '</div>';
        }
        // Recent activities
        const allEvents = [];
        for (const c of contacts) {
            allEvents.push({type: 'rfq', date: c.sent_at || c.created_at, label: 'RFQ sent to ' + esc(c.contact_name || c.email || 'contact')});
        }
        for (const r of responses) {
            allEvents.push({type: 'reply', date: r.received_at || r.created_at, label: 'Reply received' + (r.subject ? ': ' + esc(r.subject) : '')});
        }
        for (const a of activities) {
            const ch = a.channel === 'phone' ? 'Call' : a.activity_type === 'note' ? 'Note' : 'Email';
            allEvents.push({type: 'activity', date: a.created_at, label: ch + (a.summary ? ': ' + esc(a.summary) : '')});
        }
        allEvents.sort((a, b) => new Date(b.date || 0) - new Date(a.date || 0));
        if (allEvents.length) {
            html += '<div style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px">';
            const show = allEvents.slice(0, 3);
            for (const ev of show) {
                const dotColor = ev.type === 'reply' ? 'var(--green)' : ev.type === 'rfq' ? 'var(--blue)' : 'var(--muted)';
                html += '<div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:4px;font-size:11px">';
                html += '<span style="color:' + dotColor + ';flex-shrink:0;margin-top:2px">&#9679;</span>';
                html += '<span style="flex:1;color:var(--text)">' + ev.label + '</span>';
                html += '<span style="color:var(--muted);flex-shrink:0;font-size:10px">' + (ev.date ? fmtRelative(ev.date) : '') + '</span>';
                html += '</div>';
            }
            if (allEvents.length > 3) {
                html += '<div style="font-size:10px;color:var(--muted);text-align:center;margin-top:2px">+' + (allEvents.length - 3) + ' more events</div>';
            }
            html += '</div>';
        }
        html += '</div>';
    }
    panel.innerHTML = html;
}

function _renderDdDetails(reqId, targetPanel) {
    const dd = targetPanel || (document.getElementById('d-' + reqId) || {}).querySelector?.('.dd-panel');
    if (!dd) return;
    const reqs = _ddReqCache[reqId] || [];
    const meta = _reqListData.find(r => r.id === reqId) || {};

    let html = '<div class="dd-details">';

    // ── RFQ context card ──
    const dlClass = meta.deadline === 'ASAP' ? 'dd-dl-asap' : (meta.deadline ? '' : 'dd-dl-none');
    const dlText = meta.deadline === 'ASAP' ? 'ASAP' : (meta.deadline || 'Not set');
    html += `<div class="det-ctx">
        <div class="det-ctx-main">
            <div class="det-ctx-cust">${esc(meta.customer_display || '—')}</div>
            <div class="det-ctx-name">${esc(meta.name || 'Untitled Requirement')}</div>
        </div>
        <div class="det-ctx-meta">
            <div class="det-kv"><span class="det-k">Bid Due</span><span class="det-v ${dlClass}">${dlText}</span></div>
            <div class="det-kv"><span class="det-k">Created</span><span class="det-v">${meta.created_at ? new Date(meta.created_at).toLocaleDateString() : '—'}</span></div>
            <div class="det-kv"><span class="det-k">By</span><span class="det-v">${esc(meta.created_by_name || '—')}</span></div>
            <div class="det-kv"><span class="det-k">Parts</span><span class="det-v">${reqs.length}</span></div>
        </div>
    </div>`;

    // ── Parts ──
    if (!reqs.length) {
        html += '<p style="font-size:11px;color:var(--muted);margin-top:8px">No parts on this requirement</p>';
    } else {
        for (const r of reqs) {
            const subs = (r.substitutes || []).filter(s => s);
            html += '<div class="det-part">';

            // Left: core need
            html += '<div class="det-part-core">';
            html += `<div class="det-part-mpn mono" data-mpn="${escAttr(r.primary_mpn || '')}">${esc(r.primary_mpn || '—')}</div>`;
            const displayBrand = r.brand || r.manufacturer;
            if (displayBrand) html += '<div class="det-part-brand">' + esc(displayBrand) + '</div>';
            if (r.manufacturer && r.manufacturer !== r.brand && r.brand) html += '<div style="font-size:11px;color:var(--muted)">Mfr: ' + esc(r.manufacturer) + '</div>';
            if (subs.length) {
                html += `<div class="det-part-subs"><span class="det-k">Substitutes</span>`;
                for (const s of subs) html += `<span class="det-sub mono">${esc(s)}</span>`;
                html += '</div>';
            }
            html += '</div>';

            // Right: requirements + specs
            html += '<div class="det-part-info">';
            // Primary requirements row
            html += '<div class="det-req-row">';
            html += `<div class="det-req"><span class="det-k">Qty Needed</span><span class="det-req-val">${r.target_qty ? Number(r.target_qty).toLocaleString() : '—'}</span></div>`;
            html += `<div class="det-req"><span class="det-k">Target Price</span><span class="det-req-val ${r.target_price != null ? 'det-price' : ''}">${r.target_price != null ? '$' + parseFloat(r.target_price).toFixed(2) : '—'}</span></div>`;
            html += `<div class="det-req"><span class="det-k">Condition</span><span class="det-req-val">${esc(r.condition || 'Any')}</span></div>`;
            html += '</div>';
            // Specs row (only if any exist)
            const specs = [];
            if (r.date_codes) specs.push(['Date Codes', r.date_codes]);
            if (r.packaging) specs.push(['Packaging', r.packaging]);
            if (r.firmware) specs.push(['Firmware', r.firmware]);
            if (r.hardware_codes) specs.push(['HW Codes', r.hardware_codes]);
            if (specs.length) {
                html += '<div class="det-spec-row">';
                for (const [label, val] of specs) {
                    html += `<div class="det-spec"><span class="det-k">${label}</span><span class="det-spec-val">${esc(val)}</span></div>`;
                }
                html += '</div>';
            }
            html += '</div>'; // end det-part-info

            // Notes (full width)
            if (r.notes) html += `<div class="det-part-notes">${esc(r.notes)}</div>`;

            html += '</div>'; // end det-part
        }
    }

    html += '</div>';
    dd.innerHTML = html;
}

function _reqBadge(r) {
    // Full pipeline: NO RFQ → SEARCHING → RFQ SENT → OFFERS (count) → QUOTED
    if (r.offer_count > 0) return `<span class="req-badge req-badge-offers" style="cursor:pointer" title="${r.offer_count} vendor offer${r.offer_count !== 1 ? 's' : ''} received — click to view" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 4l6-2 6 2v5a6 6 0 0 1-6 5 6 6 0 0 1-6-5z"/><path d="M5.5 8l2 2 3.5-3.5"/></svg>OFFERS (${r.offer_count})</span>`;
    if (r.contact_count > 0 && r.hours_since_activity != null && r.hours_since_activity < 48) return '<span class="req-badge req-badge-searching" title="RFQ sent — vendor activity within 48h"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="7" cy="7" r="4.5"/><line x1="10.2" y1="10.2" x2="13.5" y2="13.5"/></svg>RFQ SENT</span>';
    if (r.contact_count > 0) return '<span class="req-badge req-badge-stalled" title="RFQ sent but no vendor activity in 48+ hours"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="8" r="6"/><line x1="8" y1="5" x2="8" y2="8.5"/><line x1="8" y1="8.5" x2="10.5" y2="10"/></svg>STALLED</span>';
    if (r.sighting_count > 0) return `<span class="req-badge req-badge-searching" title="${r.sighting_count} vendor${r.sighting_count !== 1 ? 's' : ''} found — review sightings and send RFQ"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="7" cy="7" r="4.5"/><line x1="10.2" y1="10.2" x2="13.5" y2="13.5"/></svg>SOURCING (${r.sighting_count})</span>`;
    return '<span class="req-badge req-badge-norfq" title="No RFQ sent yet — search for vendors first"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="8" x2="12" y2="8"/></svg>NO RFQ</span>';
}


function _renderDrillDownTable(rfqId, targetPanel) {
    const dd = targetPanel || (document.getElementById('d-' + rfqId) || {}).querySelector?.('.dd-panel');
    if (!dd) return;
    const reqs = _ddReqCache[rfqId] || [];
    if (!reqs.length && !_addRowActive[rfqId]) {
        // Auto-open add row when there are no parts
        _addRowActive[rfqId] = true;
    }
    if (!reqs.length && _addRowActive[rfqId]) {
        dd.innerHTML = `<table class="dtbl"><thead><tr>
            <th style="width:20px"></th><th></th><th>MPN</th><th>Qty</th><th>Target $</th><th title="Substitute part numbers">Subs</th><th>Condition</th><th>Date Codes</th><th title="Firmware version">FW</th><th title="Hardware revision codes">HW</th><th title="Packaging type">Pkg</th><th>Notes</th><th style="width:24px"></th>
        </tr></thead><tbody></tbody></table>`;
        _appendAddRow(rfqId, dd);
        return;
    }
    const DD_LIMIT = 100;
    const showAll = dd.dataset.showAll === '1';
    const visible = showAll ? reqs : reqs.slice(0, DD_LIMIT);
    let html = `<table class="dtbl"><thead><tr>
        <th style="width:20px"></th><th></th><th>MPN</th><th>Qty</th><th>Target $</th><th title="Substitute part numbers">Subs</th><th>Condition</th><th>Date Codes</th><th title="Firmware version">FW</th><th title="Hardware revision codes">HW</th><th title="Packaging type">Pkg</th><th>Notes</th><th style="width:24px"></th>
    </tr></thead><tbody>`;
    for (const r of visible) {
        const subsText = (r.substitutes || []).length ? r.substitutes.join(', ') : '—';
        const notesTrunc = (r.notes || '').length > 30 ? r.notes.substring(0, 30) + '\u2026' : (r.notes || '—');
        const isExpanded = _partExpandState[rfqId + '-' + r.id];
        html += `<tr class="part-row${isExpanded ? ' part-expanded' : ''}" id="pr-${rfqId}-${r.id}">
            <td style="padding:2px 4px;cursor:pointer" onclick="event.stopPropagation();togglePartExpand(${rfqId},${r.id})"><span class="part-arrow${isExpanded ? ' open' : ''}" id="pa-${rfqId}-${r.id}">\u25b6</span></td>
            <td style="padding:2px 4px">${_reqBadge(r)}</td>
            <td class="mono dd-edit" data-mpn="${escAttr(r.primary_mpn || '')}" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'primary_mpn')">${esc(r.primary_mpn || '—')}</td>
            <td class="mono dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'target_qty')">${r.target_qty || 0}</td>
            <td class="mono dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'target_price')" style="color:${r.target_price ? 'var(--teal)' : 'var(--muted)'}">${r.target_price != null ? '$' + parseFloat(r.target_price).toFixed(2) : '—'}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'substitutes')" style="font-size:10px">${esc(subsText)}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'condition')">${esc(r.condition || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'date_codes')">${esc(r.date_codes || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'firmware')" style="font-size:10px">${esc(r.firmware || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'hardware_codes')" style="font-size:10px">${esc(r.hardware_codes || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'packaging')" style="font-size:10px">${esc(r.packaging || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'notes')" title="${escAttr(r.notes || '')}" style="font-size:10px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;${r.notes ? 'color:var(--blue);font-weight:600' : ''}">${r.notes ? '\ud83d\udcdd ' : ''}${esc(notesTrunc)}</td>
            <td><button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteDrillRow(${rfqId},${r.id})" title="Remove" style="font-size:10px;padding:1px 5px">\u2715</button></td>
        </tr>`;
        // Part-level expansion row
        html += `<tr class="part-detail-row" id="pd-${rfqId}-${r.id}" style="display:${isExpanded ? 'table-row' : 'none'}">
            <td colspan="13" style="padding:0;border-top:none"><div class="part-detail-panel" id="pdp-${rfqId}-${r.id}"></div></td>
        </tr>`;
    }
    html += '</tbody></table>';
    if (!showAll && reqs.length > DD_LIMIT) {
        html += `<a onclick="event.stopPropagation();this.closest('.dd-panel').dataset.showAll='1';_renderDrillDownTable(${rfqId})" style="font-size:11px;color:var(--blue);cursor:pointer;display:inline-block;margin-top:4px">Show all ${reqs.length} parts\u2026</a>`;
    }
    dd.innerHTML = html;
    if (_addRowActive[rfqId]) _appendAddRow(rfqId, dd);
    // AI Insights card above parts table
    _renderInsightsCard(rfqId, dd);
}

// ── Split-pane: Parts on left, Offers on right ──────────────────────────
// Renders a side-by-side layout when the "parts" sub-tab is active.
// Uses existing .split-panel CSS classes. Loads offers data in parallel.
async function _renderSplitPartsOffers(reqId, partsData, panel) {
    // Full-width parts table — sightings removed (available on Sourcing page)
    _renderDrillDownTable(reqId, panel);
}

// ── Part-level expand/collapse ──────────────────────────────────────────
// Toggles the detail panel below a part row showing Offers/Notes/Tasks sub-tabs.
// Called from the ▶ arrow in each part row.
async function togglePartExpand(reqId, requirementId) {
    const key = reqId + '-' + requirementId;
    const isOpen = _partExpandState[key];
    const detailRow = document.getElementById('pd-' + key);
    const arrow = document.getElementById('pa-' + key);
    if (isOpen) {
        _partExpandState[key] = false;
        if (detailRow) detailRow.style.display = 'none';
        if (arrow) arrow.classList.remove('open');
        const partRow = document.getElementById('pr-' + key);
        if (partRow) partRow.classList.remove('part-expanded');
        return;
    }
    _partExpandState[key] = true;
    if (detailRow) detailRow.style.display = 'table-row';
    if (arrow) arrow.classList.add('open');
    const partRow = document.getElementById('pr-' + key);
    if (partRow) partRow.classList.add('part-expanded');
    if (!_partActiveTab[key]) _partActiveTab[key] = 'offers';
    const panel = document.getElementById('pdp-' + key);
    if (panel) _renderPartDetail(reqId, requirementId, panel);
}
window.togglePartExpand = togglePartExpand;

// Renders the part-level detail panel with sub-tab pills and content
async function _renderPartDetail(reqId, requirementId, panel) {
    const key = reqId + '-' + requirementId;
    const activeTab = _partActiveTab[key] || 'offers';
    const tabs = ['offers', 'notes', 'tasks'];
    const tabLabels = { offers: 'Offers', notes: 'Notes', tasks: 'Tasks' };
    const pills = tabs.map(t =>
        `<button class="part-tab${t === activeTab ? ' on' : ''}" onclick="event.stopPropagation();_switchPartTab(${reqId},${requirementId},'${t}')">${tabLabels[t]}</button>`
    ).join('');
    panel.innerHTML = `<div class="part-detail-tabs">${pills}</div><div class="part-detail-content" id="pdc-${key}"><span style="font-size:11px;color:var(--muted)">Loading\u2026</span></div>`;
    await _loadPartTab(reqId, requirementId, activeTab);
}

async function _switchPartTab(reqId, requirementId, tabName) {
    const key = reqId + '-' + requirementId;
    _partActiveTab[key] = tabName;
    const panel = document.getElementById('pdp-' + key);
    if (!panel) return;
    panel.querySelectorAll('.part-tab').forEach(t => {
        t.classList.toggle('on', t.textContent === { offers: 'Offers', notes: 'Notes', tasks: 'Tasks' }[tabName]);
    });
    await _loadPartTab(reqId, requirementId, tabName);
}
window._switchPartTab = _switchPartTab;

async function _loadPartTab(reqId, requirementId, tabName) {
    const key = reqId + '-' + requirementId;
    const cacheKey = key + '-' + tabName;
    const contentEl = document.getElementById('pdc-' + key);
    if (!contentEl) return;
    const cached = _partDetailCache[cacheKey];
    if (cached) { _renderPartTab(reqId, requirementId, tabName, cached, contentEl); return; }
    contentEl.innerHTML = '<span style="font-size:11px;color:var(--muted)">Loading\u2026</span>';
    try {
        let data;
        switch (tabName) {
            case 'offers':
                data = await apiFetch(`/api/requirements/${requirementId}/offers`);
                break;
            case 'notes':
                data = await apiFetch(`/api/requirements/${requirementId}/notes`);
                break;
            case 'tasks':
                data = await apiFetch(`/api/requirements/${requirementId}/tasks`);
                break;
        }
        _partDetailCache[cacheKey] = data;
        _renderPartTab(reqId, requirementId, tabName, data, contentEl);
    } catch(e) {
        contentEl.innerHTML = '<span style="font-size:11px;color:var(--red)">Failed to load</span>';
    }
}

function _renderPartTab(reqId, requirementId, tabName, data, contentEl) {
    switch (tabName) {
        case 'offers': _renderPartOffers(reqId, requirementId, data, contentEl); break;
        case 'notes': _renderPartNotes(reqId, requirementId, data, contentEl); break;
        case 'tasks': _renderPartTasks(reqId, requirementId, data, contentEl); break;
        default: contentEl.textContent = '';
    }
}

// ── Part-level Offers renderer ──
// Shows active and historical offers for a single part number
function _renderPartOffers(reqId, requirementId, data, contentEl) {
    const offers = Array.isArray(data) ? data : (data?.offers || []);
    if (!offers.length) {
        contentEl.innerHTML = '<span style="font-size:11px;color:var(--muted)">No offers yet for this part</span>';
        return;
    }
    // Look up target price from requirement cache for price-vs-target indicator
    const _reqs = _ddReqCache[reqId] || [];
    const _req = _reqs.find(r => r.id === requirementId);
    const targetPrice = _req?.target_price ?? null;
    let html = `<table class="dtbl" style="font-size:11px"><thead><tr>
        <th>Vendor</th><th>MPN</th><th>Qty</th><th>Price</th><th>Lead</th><th>Condition</th><th>Source</th><th>Status</th><th>Date</th><th>Notes</th>
    </tr></thead><tbody>`;
    for (const o of offers) {
        const price = o.unit_price != null ? '$' + parseFloat(o.unit_price).toFixed(4) : '\u2014';
        let priceColor = 'var(--teal)';
        let priceTitle = '';
        if (targetPrice != null && o.unit_price != null) {
            const pctD = ((o.unit_price - targetPrice) / targetPrice) * 100;
            priceColor = pctD <= 0 ? 'var(--green)' : pctD <= 15 ? 'var(--amber)' : 'var(--red)';
            priceTitle = ` title="${pctD > 0 ? '+' : ''}${pctD.toFixed(0)}% vs target ($${Number(targetPrice).toFixed(4)})"`;
        }
        const status = o.status || 'pending';
        const statusColor = status === 'accepted' ? 'var(--green)' : status === 'rejected' ? 'var(--red)' : 'var(--muted)';
        const age = o.created_at ? _timeAgo(o.created_at) : '\u2014';
        const isHistorical = o.is_historical || false;
        html += `<tr style="${isHistorical ? 'opacity:0.7;font-style:italic' : ''}">
            <td>${esc(o.vendor_name || '\u2014')}</td>
            <td class="mono">${esc(o.mpn || '\u2014')}</td>
            <td class="mono">${o.qty_available != null ? o.qty_available.toLocaleString() : '\u2014'}</td>
            <td class="mono" style="color:${priceColor}"${priceTitle}>${price}</td>
            <td>${esc(o.lead_time || '\u2014')}</td>
            <td>${esc(o.condition || '\u2014')}</td>
            <td style="font-size:10px">${esc(o.source_type || '\u2014')}${isHistorical ? ' (hist)' : ''}</td>
            <td style="color:${statusColor};font-weight:600;font-size:10px">${esc(status)}</td>
            <td style="font-size:10px;color:var(--muted)">${age}</td>
            <td style="font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escAttr(o.notes || '')}">${esc(o.notes || '\u2014')}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    contentEl.innerHTML = html;
}

// ── Part-level Notes renderer ──
// Shows notes on the requirement and notes on individual offers
function _renderPartNotes(reqId, requirementId, data, contentEl) {
    const notes = Array.isArray(data) ? data : (data?.notes || []);
    const reqs = _ddReqCache[reqId] || [];
    const req = reqs.find(x => x.id === requirementId);
    let html = '';
    // Requirement-level notes
    html += `<div style="margin-bottom:12px">
        <div style="font-size:11px;font-weight:700;margin-bottom:4px;color:var(--muted)">Requirement Notes</div>
        <div class="part-note-box dd-edit" onclick="event.stopPropagation();editDrillCell(this,${reqId},${requirementId},'notes')" style="min-height:32px;padding:6px 8px;background:var(--bg2);border-radius:4px;font-size:11px;cursor:pointer">${esc(req?.notes || 'Click to add notes\u2026')}</div>
    </div>`;
    // Offer notes
    if (notes.length) {
        html += '<div style="font-size:11px;font-weight:700;margin-bottom:4px;color:var(--muted)">Offer Notes</div>';
        for (const n of notes) {
            html += `<div style="padding:4px 8px;margin-bottom:4px;background:var(--bg2);border-radius:4px;border-left:3px solid var(--blue);font-size:11px">
                <span style="font-weight:600">${esc(n.vendor_name || 'Offer')}</span>
                <span style="color:var(--muted);margin-left:6px">${n.created_at ? _timeAgo(n.created_at) : ''}</span>
                <div style="margin-top:2px">${esc(n.note || n.notes || n.text || '\u2014')}</div>
            </div>`;
        }
    } else if (!req?.notes) {
        html += '<span style="font-size:11px;color:var(--muted)">No notes yet</span>';
    }
    // Add note button
    html += `<button class="btn btn-ghost btn-sm" style="font-size:10px;margin-top:6px" onclick="event.stopPropagation();_addPartNote(${reqId},${requirementId})">+ Add Note</button>`;
    contentEl.innerHTML = html;
}

// ── Part-level Tasks renderer ──
// Shows tasks linked to this requirement
function _renderPartTasks(reqId, requirementId, data, contentEl) {
    const tasks = Array.isArray(data) ? data : (data?.tasks || []);
    if (!tasks.length) {
        contentEl.innerHTML = `<span style="font-size:11px;color:var(--muted)">No tasks for this part</span>
            <button class="btn btn-ghost btn-sm" style="font-size:10px;margin-left:8px" onclick="event.stopPropagation();_addPartTask(${reqId},${requirementId})">+ Add Task</button>`;
        return;
    }
    let html = '<div style="display:flex;flex-direction:column;gap:4px">';
    for (const t of tasks) {
        const done = t.status === 'done' || t.status === 'completed';
        const statusIcon = done ? '\u2705' : t.status === 'in_progress' ? '\ud83d\udfe1' : '\u2b1c';
        html += `<div style="display:flex;align-items:center;gap:6px;padding:4px 8px;background:var(--bg2);border-radius:4px;font-size:11px${done ? ';opacity:0.6;text-decoration:line-through' : ''}">
            <span>${statusIcon}</span>
            <span style="flex:1">${esc(t.title || t.description || '\u2014')}</span>
            <span style="font-size:10px;color:var(--muted)">${t.due_date ? _timeAgo(t.due_date) : ''}</span>
        </div>`;
    }
    html += '</div>';
    html += `<button class="btn btn-ghost btn-sm" style="font-size:10px;margin-top:6px" onclick="event.stopPropagation();_addPartTask(${reqId},${requirementId})">+ Add Task</button>`;
    contentEl.innerHTML = html;
}

// Placeholder for adding a note to a part — opens inline input
function _addPartNote(reqId, requirementId) {
    const key = reqId + '-' + requirementId;
    const contentEl = document.getElementById('pdc-' + key);
    if (!contentEl) return;
    const existing = contentEl.querySelector('.part-note-input');
    if (existing) { existing.focus(); return; }
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'margin-top:6px;display:flex;gap:4px';
    wrapper.innerHTML = `<textarea class="part-note-input" placeholder="Add a note\u2026" rows="2" style="flex:1;font-size:11px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--text);resize:vertical"></textarea>
        <button class="btn btn-primary btn-sm" style="font-size:10px;align-self:flex-end" onclick="event.stopPropagation();_savePartNote(${reqId},${requirementId},this.previousElementSibling.value)">Save</button>`;
    contentEl.appendChild(wrapper);
    wrapper.querySelector('textarea').focus();
}
window._addPartNote = _addPartNote;

async function _savePartNote(reqId, requirementId, text) {
    if (!text?.trim()) return;
    try {
        await apiFetch(`/api/requirements/${requirementId}/notes`, { method: 'POST', body: { text: text.trim() } });
        delete _partDetailCache[reqId + '-' + requirementId + '-notes'];
        await _loadPartTab(reqId, requirementId, 'notes');
    } catch(e) { logCatchError('_savePartNote', e); }
}
window._savePartNote = _savePartNote;

// Placeholder for adding a task to a part
function _addPartTask(reqId, requirementId) {
    const key = reqId + '-' + requirementId;
    const contentEl = document.getElementById('pdc-' + key);
    if (!contentEl) return;
    const existing = contentEl.querySelector('.part-task-input');
    if (existing) { existing.focus(); return; }
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'margin-top:6px;display:flex;gap:4px';
    wrapper.innerHTML = `<input class="part-task-input" placeholder="Task description\u2026" style="flex:1;font-size:11px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--text)">
        <button class="btn btn-primary btn-sm" style="font-size:10px" onclick="event.stopPropagation();_savePartTask(${reqId},${requirementId},this.previousElementSibling.value)">Add</button>`;
    contentEl.appendChild(wrapper);
    wrapper.querySelector('input').focus();
}
window._addPartTask = _addPartTask;

async function _savePartTask(reqId, requirementId, title) {
    if (!title?.trim()) return;
    try {
        await apiFetch(`/api/requirements/${requirementId}/tasks`, { method: 'POST', body: { title: title.trim() } });
        delete _partDetailCache[reqId + '-' + requirementId + '-tasks'];
        await _loadPartTab(reqId, requirementId, 'tasks');
    } catch(e) { logCatchError('_savePartTask', e); }
}
window._savePartTask = _savePartTask;

function editDrillCell(td, rfqId, reqId, field) {
    if (td.querySelector('input, select, textarea')) return;
    const reqs = _ddReqCache[rfqId] || [];
    const r = reqs.find(x => x.id === reqId);
    if (!r) return;

    let currentVal;
    if (field === 'substitutes') currentVal = (r.substitutes || []).join(', ');
    else if (field === 'target_qty') currentVal = String(r.target_qty || 1);
    else if (field === 'target_price') currentVal = r.target_price != null ? String(r.target_price) : '';
    else currentVal = r[field] || '';

    let el;
    if (field === 'condition') {
        el = document.createElement('select');
        el.className = 'req-edit-input';
        el.innerHTML = '<option value="">—</option>' + CONDITION_OPTIONS.map(o => `<option value="${o}"${currentVal === o ? ' selected' : ''}>${o}</option>`).join('');
    } else if (field === 'notes') {
        el = document.createElement('textarea');
        el.className = 'req-edit-input';
        el.value = currentVal;
        el.rows = 2;
        el.style.cssText = 'width:180px;font-size:11px;resize:vertical';
    } else {
        el = document.createElement('input');
        el.className = 'req-edit-input';
        el.value = currentVal;
        if (field === 'target_qty') { el.type = 'number'; el.min = '1'; el.style.width = '50px'; }
        if (field === 'target_price') { el.type = 'number'; el.step = '0.01'; el.min = '0'; el.style.width = '60px'; el.placeholder = '0.00'; }
    }

    td.textContent = '';
    td.appendChild(el);
    const hint = document.createElement('span');
    hint.style.cssText = 'font-size:9px;color:var(--muted);display:block;margin-top:1px';
    hint.textContent = 'Enter \u2713  Esc \u2717';
    td.appendChild(hint);
    el.focus();
    if (el.select) el.select();

    let _cancelled = false;
    const save = async () => {
        if (_cancelled) return;
        _cancelled = true; // prevent double-fire
        const val = el.value.trim();
        if (val === currentVal) {
            // No change — just restore the cell display text without full re-render
            _restoreDrillCell(td, r, field);
            return;
        }
        if (field === 'primary_mpn' && !val) {
            showToast('MPN cannot be blank', 'warn');
            _restoreDrillCell(td, r, field);
            return;
        }
        const body = {};
        if (field === 'target_price') {
            const pf = val ? parseFloat(val) : null;
            body[field] = (pf !== null && (isNaN(pf) || pf < 0)) ? null : pf;
        }
        else if (field === 'target_qty') { const pq = parseInt(val); body[field] = (pq >= 1) ? pq : 1; }
        else if (field === 'substitutes') body[field] = val ? val.split(',').map(s => s.trim()).filter(Boolean) : [];
        else body[field] = val;
        try {
            await apiFetch(`/api/requirements/${reqId}`, { method: 'PUT', body });
            const idx = reqs.findIndex(x => x.id === reqId);
            if (idx >= 0) Object.assign(reqs[idx], body);
        } catch(e) { logCatchError('editDrillCell', e); }
        // Update just this cell in-place instead of re-rendering entire table
        _restoreDrillCell(td, r, field);
    };

    el.addEventListener('blur', save);
    if (field === 'condition') {
        el.addEventListener('change', () => el.blur());
    }
    el.addEventListener('keydown', e => {
        if (e.key === 'Enter' && field !== 'notes') { e.preventDefault(); el.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); _cancelled = true; _restoreDrillCell(td, r, field); }
    });
}

function _restoreDrillCell(td, r, field) {
    // Restore cell display value without full table re-render
    let display;
    if (field === 'primary_mpn') display = esc(r.primary_mpn || '\u2014');
    else if (field === 'target_qty') display = String(r.target_qty || 0);
    else if (field === 'target_price') {
        display = r.target_price != null ? '$' + parseFloat(r.target_price).toFixed(2) : '\u2014';
        td.style.color = r.target_price ? 'var(--teal)' : 'var(--muted)';
    }
    else if (field === 'substitutes') display = esc((r.substitutes || []).length ? r.substitutes.join(', ') : '\u2014');
    else if (field === 'notes') {
        const notesTrunc = (r.notes || '').length > 30 ? r.notes.substring(0, 30) + '\u2026' : (r.notes || '\u2014');
        display = (r.notes ? '\ud83d\udcdd ' : '') + esc(notesTrunc);
        td.title = r.notes || '';
        td.style.color = r.notes ? 'var(--blue)' : '';
        td.style.fontWeight = r.notes ? '600' : '';
    }
    else display = esc(r[field] || '\u2014');
    td.innerHTML = display;
}

function addDrillRow(rfqId) {
    if (_addRowActive[rfqId]) {
        const dd = document.getElementById('d-' + rfqId)?.querySelector('.dd-panel');
        const mpnInput = dd?.querySelector('.add-row-mpn');
        if (mpnInput) { mpnInput.focus(); mpnInput.select(); }
        return;
    }
    _addRowActive[rfqId] = true;
    _renderDrillDownTable(rfqId);
}

function _appendAddRow(rfqId, dd) {
    const tbody = dd.querySelector('.dtbl tbody');
    if (!tbody) return;

    // Remove any existing add-row to prevent duplicates
    tbody.querySelectorAll('.add-row').forEach(r => r.remove());

    const tr = document.createElement('tr');
    tr.className = 'add-row';
    tr.addEventListener('click', e => e.stopPropagation());

    // Expand arrow (empty for add row)
    let td = document.createElement('td');
    tr.appendChild(td);

    // Badge (empty for add row)
    td = document.createElement('td');
    tr.appendChild(td);

    // MPN (required)
    td = document.createElement('td');
    td.className = 'mono';
    const inMpn = document.createElement('input');
    inMpn.type = 'text'; inMpn.className = 'add-row-mpn'; inMpn.placeholder = 'MPN *';
    td.appendChild(inMpn); tr.appendChild(td);

    // Qty
    td = document.createElement('td');
    td.className = 'mono';
    const inQty = document.createElement('input');
    inQty.type = 'number'; inQty.className = 'add-row-qty'; inQty.min = '1'; inQty.value = '1'; inQty.style.width = '50px';
    td.appendChild(inQty); tr.appendChild(td);

    // Target $
    td = document.createElement('td');
    td.className = 'mono';
    const inPrice = document.createElement('input');
    inPrice.type = 'number'; inPrice.className = 'add-row-price'; inPrice.step = '0.01'; inPrice.min = '0'; inPrice.placeholder = '0.00'; inPrice.style.width = '60px';
    td.appendChild(inPrice); tr.appendChild(td);

    // Subs, Condition, Date Codes, FW, HW, Pkg, Notes — placeholder dashes
    for (let i = 0; i < 7; i++) {
        td = document.createElement('td');
        td.style.cssText = 'color:var(--muted);font-size:10px';
        td.textContent = '\u2014';
        tr.appendChild(td);
    }

    // Cancel button
    td = document.createElement('td');
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-sm';
    cancelBtn.textContent = '\u2715';
    cancelBtn.title = 'Cancel';
    cancelBtn.style.cssText = 'font-size:10px;padding:1px 5px;color:var(--muted)';
    cancelBtn.addEventListener('click', () => _cancelAddRow(rfqId));
    td.appendChild(cancelBtn); tr.appendChild(td);

    tbody.appendChild(tr);

    // Keyboard handling
    [inMpn, inQty, inPrice].forEach(inp => {
        inp.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); _saveAddRow(rfqId); }
            if (e.key === 'Escape') { e.preventDefault(); _cancelAddRow(rfqId); }
        });
    });

    setTimeout(() => inMpn.focus(), 0);
}

const _saveAddRowPending = {};
async function _saveAddRow(rfqId) {
    if (_saveAddRowPending[rfqId]) return; // prevent double-submit
    const dd = document.getElementById('d-' + rfqId)?.querySelector('.dd-panel');
    if (!dd) return;
    const mpnInput = dd.querySelector('.add-row-mpn');
    const qtyInput = dd.querySelector('.add-row-qty');
    const priceInput = dd.querySelector('.add-row-price');
    if (!mpnInput) return;

    const mpn = mpnInput.value.trim();
    if (!mpn) {
        mpnInput.style.borderColor = 'var(--red)';
        mpnInput.focus();
        showToast('MPN is required', 'warn');
        return;
    }

    const parsedQty = parseInt(qtyInput?.value);
    const body = { primary_mpn: mpn, target_qty: (parsedQty >= 1) ? parsedQty : 1 };
    const priceVal = priceInput?.value.trim();
    if (priceVal) {
        const pf = parseFloat(priceVal);
        if (!isNaN(pf) && pf >= 0) body.target_price = pf;
    }

    // Disable inputs during save
    _saveAddRowPending[rfqId] = true;
    dd.querySelectorAll('.add-row input').forEach(inp => inp.disabled = true);

    try {
        const addResult = await apiFetch(`/api/requisitions/${rfqId}/requirements`, { method: 'POST', body });
        // Show duplicate warnings if any
        const dups = addResult && addResult.duplicates;
        if (dups && dups.length) {
            const dupMsg = dups.map(d => `${d.mpn} (RFQ-${d.req_id}: ${d.req_name})`).join(', ');
            showToast('Duplicate alert: ' + dupMsg + ' quoted for this customer in last 30 days', 'warning');
        }
        // Keep add row active so user can enter next part immediately
        delete _ddReqCache[rfqId];
        if (_ddTabCache[rfqId]) { delete _ddTabCache[rfqId].parts; delete _ddTabCache[rfqId].details; }
        _ddReqCache[rfqId] = await apiFetch(`/api/requisitions/${rfqId}/requirements`);
        const rfq = _reqListData.find(r => r.id === rfqId);
        if (rfq) {
            const freshReqs = _ddReqCache[rfqId] || [];
            rfq.requirement_count = freshReqs.length;
            rfq.sourced_count = freshReqs.filter(r => (r.sighting_count || 0) > 0).length;
        }
        if (_ddTabCache[rfqId]) { _ddTabCache[rfqId].parts = _ddReqCache[rfqId]; _ddTabCache[rfqId].details = _ddReqCache[rfqId]; }
        _addRowActive[rfqId] = true;
        _renderDrillDownTable(rfqId);
        _refreshReqRow(rfqId);
        showToast('Part added \u2014 enter next part or press Esc to finish', 'success');
        const drow = document.getElementById('d-' + rfqId);
        if (drow) {
            const hdr = drow.querySelector('span[style*="font-weight:700"]');
            const total = _ddReqCache[rfqId].length;
            if (hdr) hdr.textContent = `${total} part${total !== 1 ? 's' : ''}`;
        }
    } catch(e) {
        showToast('Failed to add part', 'error');
        dd.querySelectorAll('.add-row input').forEach(inp => inp.disabled = false);
        mpnInput.focus();
    } finally {
        delete _saveAddRowPending[rfqId];
    }
}

function _cancelAddRow(rfqId) {
    delete _addRowActive[rfqId];
    _renderDrillDownTable(rfqId);
}

async function deleteDrillRow(rfqId, reqId) {
    confirmAction('Remove Part', 'Remove this part?', async function() {
        try {
        await apiFetch(`/api/requirements/${reqId}`, { method: 'DELETE' });
        const reqs = _ddReqCache[rfqId];
        if (reqs) {
            const idx = reqs.findIndex(x => x.id === reqId);
            if (idx >= 0) reqs.splice(idx, 1);
        }
        // Sync tab cache
        if (_ddTabCache[rfqId]) { _ddTabCache[rfqId].parts = reqs; _ddTabCache[rfqId].details = reqs; }
        // Update counts from the modified cache
        const rfq = _reqListData.find(r => r.id === rfqId);
        if (rfq) {
            const freshReqs = reqs || [];
            rfq.requirement_count = freshReqs.length;
            rfq.sourced_count = freshReqs.filter(r => (r.sighting_count || 0) > 0).length;
        }
        _renderDrillDownTable(rfqId);
        _refreshReqRow(rfqId);
        // Update header count
        const drow = document.getElementById('d-' + rfqId);
        if (drow) {
            const hdr = drow.querySelector('span[style*="font-weight:700"]');
            const total = (reqs || []).length;
            if (hdr) hdr.textContent = `${total} part${total !== 1 ? 's' : ''}`;
        }
    } catch(e) { showToast('Failed to remove part', 'error'); }
    }, {confirmClass: 'btn-danger', confirmLabel: 'Remove'});
}

// ── Bulk Upload (CSV/Excel) ───────────────────────────────────────────────
function ddUploadFile(rfqId) {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = '.csv,.xlsx,.xls';
    inp.style.display = 'none';
    inp.onchange = async () => {
        const file = inp.files[0];
        if (!file) return;
        const form = new FormData();
        form.append('file', file);
        try {
            const data = await apiFetch(`/api/requisitions/${rfqId}/upload`, { method: 'POST', body: form });
            const added = data.added || data.count || 0;
            delete _ddReqCache[rfqId];
            if (_ddTabCache[rfqId]) { delete _ddTabCache[rfqId].parts; delete _ddTabCache[rfqId].details; }
            _ddReqCache[rfqId] = await apiFetch(`/api/requisitions/${rfqId}/requirements`);
            if (_ddTabCache[rfqId]) { _ddTabCache[rfqId].parts = _ddReqCache[rfqId]; _ddTabCache[rfqId].details = _ddReqCache[rfqId]; }
            const rfq = _reqListData.find(r => r.id === rfqId);
            if (rfq) {
                const freshReqs = _ddReqCache[rfqId] || [];
                rfq.requirement_count = freshReqs.length;
                rfq.sourced_count = freshReqs.filter(r => (r.sighting_count || 0) > 0).length;
            }
            _renderDrillDownTable(rfqId);
            _refreshReqRow(rfqId);
            const drow = document.getElementById('d-' + rfqId);
            if (drow) {
                const hdr = drow.querySelector('span[style*="font-weight:700"]');
                const total = _ddReqCache[rfqId].length;
                if (hdr) hdr.textContent = `${total} part${total !== 1 ? 's' : ''}`;
            }
            showToast(`Added ${added} part${added !== 1 ? 's' : ''} from ${file.name}`, 'success');
        } catch (e) {
            showToast('Upload failed: ' + e.message, 'error');
        }
        inp.remove();
    };
    document.body.appendChild(inp);
    inp.click();
}

// ── Bulk Paste from Spreadsheet ──────────────────────────────────────────
function ddPasteRows(rfqId) {
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('pasteTargetRfqId', 'value', rfqId);
    _s('pasteTsvInput', 'value', '');
    _s('pastePreview', 'textContent', '');
    _s('pasteSubmitBtn', 'disabled', true);
    document.getElementById('pastePartsModal')?.classList.add('open');
    setTimeout(() => document.getElementById('pasteTsvInput')?.focus(), 100);
}

function _parseTsvInput(text) {
    const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    if (!lines.length) return [];

    // Split each line by tab (or 2+ spaces as fallback)
    const rows = lines.map(l => l.split(/\t/).map(c => c.trim()));
    if (!rows.length) return [];

    // Auto-detect header row
    const first = rows[0].map(c => c.toLowerCase().replace(/[^a-z0-9]/g, ''));
    const mpnAliases = ['mpn', 'partnumber', 'partno', 'pn', 'mfgpart', 'mfgpartnumber', 'part', 'mfpn'];
    const qtyAliases = ['qty', 'quantity', 'targetqty', 'reqd', 'required', 'need'];
    const priceAliases = ['price', 'targetprice', 'target', 'unitprice', 'unit'];
    const condAliases = ['condition', 'cond', 'cond.'];
    const dcAliases = ['datecode', 'dc', 'datecodes', 'date_code', 'date_codes'];
    const subsAliases = ['subs', 'substitutes', 'alts', 'alternates', 'sub', 'substitute'];
    const fwAliases = ['firmware', 'fw', 'firmwareversion'];
    const hwAliases = ['hardware', 'hw', 'hardwarecodes', 'hardware_codes', 'hwrev'];
    const pkgAliases = ['packaging', 'pkg', 'package'];
    const notesAliases = ['notes', 'note', 'comments', 'comment', 'remarks'];

    let mpnCol = -1, qtyCol = -1, priceCol = -1;
    let condCol = -1, dcCol = -1, subsCol = -1, fwCol = -1, hwCol = -1, pkgCol = -1, notesCol = -1;
    let dataStart = 0;

    // Check if first row is a header
    const hasHeader = first.some(c => mpnAliases.includes(c) || qtyAliases.includes(c));
    if (hasHeader) {
        first.forEach((c, i) => {
            if (mpnCol < 0 && mpnAliases.includes(c)) mpnCol = i;
            if (qtyCol < 0 && qtyAliases.includes(c)) qtyCol = i;
            if (priceCol < 0 && priceAliases.includes(c)) priceCol = i;
            if (condCol < 0 && condAliases.includes(c)) condCol = i;
            if (dcCol < 0 && dcAliases.includes(c)) dcCol = i;
            if (subsCol < 0 && subsAliases.includes(c)) subsCol = i;
            if (fwCol < 0 && fwAliases.includes(c)) fwCol = i;
            if (hwCol < 0 && hwAliases.includes(c)) hwCol = i;
            if (pkgCol < 0 && pkgAliases.includes(c)) pkgCol = i;
            if (notesCol < 0 && notesAliases.includes(c)) notesCol = i;
        });
        dataStart = 1;
    }

    // Default column mapping: first col = MPN, second = qty, third = price
    if (mpnCol < 0) mpnCol = 0;
    if (qtyCol < 0 && rows[0].length > 1) qtyCol = 1;
    if (priceCol < 0 && rows[0].length > 2) priceCol = 2;

    const results = [];
    for (let i = dataStart; i < rows.length; i++) {
        const r = rows[i];
        const mpn = (r[mpnCol] || '').trim();
        if (!mpn) continue;
        const obj = { primary_mpn: mpn };
        if (qtyCol >= 0 && r[qtyCol]) {
            const q = parseInt(r[qtyCol].replace(/[^0-9]/g, ''));
            if (q > 0) obj.target_qty = q;
        }
        if (priceCol >= 0 && r[priceCol]) {
            const p = parseFloat(r[priceCol].replace(/[^0-9.]/g, ''));
            if (p > 0) obj.target_price = p;
        }
        if (condCol >= 0 && r[condCol]) obj.condition = r[condCol].trim();
        if (dcCol >= 0 && r[dcCol]) obj.date_codes = r[dcCol].trim();
        if (subsCol >= 0 && r[subsCol]) obj.substitutes = r[subsCol].split(/[,;]/).map(s => s.trim()).filter(Boolean);
        if (fwCol >= 0 && r[fwCol]) obj.firmware = r[fwCol].trim();
        if (hwCol >= 0 && r[hwCol]) obj.hardware_codes = r[hwCol].trim();
        if (pkgCol >= 0 && r[pkgCol]) obj.packaging = r[pkgCol].trim();
        if (notesCol >= 0 && r[notesCol]) obj.notes = r[notesCol].trim();
        results.push(obj);
    }
    return results;
}

function _previewPaste() {
    const text = document.getElementById('pasteTsvInput')?.value || '';
    const parts = _parseTsvInput(text);
    const preview = document.getElementById('pastePreview');
    const btn = document.getElementById('pasteSubmitBtn');
    if (!preview || !btn) return;
    if (parts.length === 0) {
        preview.textContent = 'No parts detected';
        btn.disabled = true;
    } else {
        // Build a mini table showing detected columns
        const extraCols = parts.some(p => p.condition || p.date_codes || p.substitutes || p.firmware || p.hardware_codes || p.packaging || p.notes);
        let tbl = `<b>${parts.length}</b> part${parts.length !== 1 ? 's' : ''} detected`;
        if (extraCols) {
            const cols = ['MPN','Qty','Price'];
            if (parts.some(p => p.condition)) cols.push('Cond');
            if (parts.some(p => p.date_codes)) cols.push('DC');
            if (parts.some(p => p.packaging)) cols.push('Pkg');
            if (parts.some(p => p.substitutes)) cols.push('Subs');
            if (parts.some(p => p.firmware)) cols.push('FW');
            if (parts.some(p => p.hardware_codes)) cols.push('HW');
            if (parts.some(p => p.notes)) cols.push('Notes');
            tbl += ` (columns: ${cols.join(', ')})`;
        }
        tbl += ':<br>';
        const show = parts.slice(0, 5);
        tbl += '<table style="font-size:11px;margin-top:4px;width:100%;border-collapse:collapse"><tr style="color:var(--muted)"><th style="text-align:left;padding:2px 6px">MPN</th><th style="text-align:right;padding:2px 6px">Qty</th><th style="text-align:right;padding:2px 6px">Price</th></tr>';
        for (const p of show) {
            tbl += `<tr><td style="padding:2px 6px">${esc(p.primary_mpn)}</td><td style="text-align:right;padding:2px 6px">${p.target_qty || 1}</td><td style="text-align:right;padding:2px 6px">${p.target_price ? '$' + p.target_price.toFixed(2) : '\u2014'}</td></tr>`;
        }
        if (parts.length > 5) tbl += `<tr><td colspan="3" style="padding:2px 6px;color:var(--muted);font-style:italic">\u2026 and ${parts.length - 5} more</td></tr>`;
        tbl += '</table>';
        preview.innerHTML = tbl;
        btn.disabled = false;
    }
}

async function submitPastedRows() {
    const rfqId = parseInt(document.getElementById('pasteTargetRfqId')?.value);
    const text = document.getElementById('pasteTsvInput')?.value || '';
    const parts = _parseTsvInput(text);
    if (!parts.length || !rfqId) return;

    const btn = document.getElementById('pasteSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Adding…';

    try {
        const pasteResult = await apiFetch(`/api/requisitions/${rfqId}/requirements`, { method: 'POST', body: parts });
        // Show duplicate warnings if any
        const pasteDups = pasteResult && pasteResult.duplicates;
        if (pasteDups && pasteDups.length) {
            const dupMsg = pasteDups.map(d => `${d.mpn} (RFQ-${d.req_id}: ${d.req_name})`).join(', ');
            showToast('Duplicate alert: ' + dupMsg + ' quoted for this customer in last 30 days', 'warning');
        }
        closeModal('pastePartsModal');
        delete _ddReqCache[rfqId];
        if (_ddTabCache[rfqId]) { delete _ddTabCache[rfqId].parts; delete _ddTabCache[rfqId].details; }
        _ddReqCache[rfqId] = await apiFetch(`/api/requisitions/${rfqId}/requirements`);
        if (_ddTabCache[rfqId]) { _ddTabCache[rfqId].parts = _ddReqCache[rfqId]; _ddTabCache[rfqId].details = _ddReqCache[rfqId]; }
        const rfq = _reqListData.find(r => r.id === rfqId);
        if (rfq) {
            const freshReqs = _ddReqCache[rfqId] || [];
            rfq.requirement_count = freshReqs.length;
            rfq.sourced_count = freshReqs.filter(r => (r.sighting_count || 0) > 0).length;
        }
        _renderDrillDownTable(rfqId);
        _refreshReqRow(rfqId);
        const drow = document.getElementById('d-' + rfqId);
        if (drow) {
            const hdr = drow.querySelector('span[style*="font-weight:700"]');
            const total = _ddReqCache[rfqId].length;
            if (hdr) hdr.textContent = `${total} part${total !== 1 ? 's' : ''}`;
        }
        showToast(`Added ${parts.length} part${parts.length !== 1 ? 's' : ''}`, 'success');
    } catch (e) {
        showToast('Paste import failed: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = 'Add Parts';
}

// ── Sourcing Score Tooltip Builder ────────────────────────────────────────
function _buildEffortTip(score, color, signals) {
    if (!signals) return '';
    const s = signals;
    const rows = [
        {label: 'Sources found', val: s.sources.val, pct: s.sources.pct, level: s.sources.level,
         tip: s.sources.level === 'low' ? 'Search more vendors' : ''},
        {label: 'RFQs sent', val: s.rfqs.val, pct: s.rfqs.pct, level: s.rfqs.level,
         tip: s.rfqs.level === 'low' ? 'Send more RFQs' : ''},
        {label: 'Vendor replies', val: s.replies.val + (s.replies.of ? '/' + s.replies.of : ''), pct: s.replies.pct, level: s.replies.level,
         tip: s.replies.level === 'low' ? 'Follow up on RFQs' : ''},
        {label: 'Offers received', val: s.offers.val, pct: s.offers.pct, level: s.offers.level,
         tip: s.offers.level === 'low' ? 'Push for firm offers' : ''},
        {label: 'Phone calls', val: s.calls.val, pct: s.calls.pct, level: s.calls.level,
         tip: s.calls.level === 'low' ? 'Pick up the phone' : ''},
        {label: 'Emails', val: s.emails.val, pct: s.emails.pct, level: s.emails.level,
         tip: s.emails.level === 'low' ? 'More vendor outreach' : ''},
    ];
    // Find weakest signals for summary
    const weak = rows.filter(r => r.level === 'low').map(r => r.tip).filter(Boolean);
    const summary = weak.length ? weak.slice(0, 2).join(' · ') : (color === 'green' ? 'Strong sourcing' : 'Good progress');
    let html = `<span class="effort-tip">`;
    html += `<div style="font-weight:700;margin-bottom:6px;font-size:12px">Sourcing Score: ${Math.round(score)}/100</div>`;
    for (const r of rows) {
        html += `<div class="effort-sig"><span style="min-width:85px">${r.label}</span><span class="effort-sig-bar"><span class="effort-sig-fill ${r.level}" style="width:${r.pct}%"></span></span><span style="min-width:28px;text-align:right;font-weight:600">${r.val}</span></div>`;
    }
    html += `<div style="margin-top:6px;font-style:italic;color:var(--muted);font-size:10px">${esc(summary)}</div>`;
    html += `</span>`;
    return html;
}

// ── Sourcing Drill-Down (sightings view) ────────────────────────────────
// Cache for per-requirement sourcing scores
const _ddScoreCache = {};
let _ddScoreAborts = {};  // reqId → AbortController for score fetches

// ── Sighting filters ─────────────────────────────────────────────────────
const _ddSightingFilters = {};
const _ddFilterTimers = {};
const _ddTypeFilter = {}; // reqId → 'all' | 'exact' | 'sub' | 'available' | 'na'
function _ddSetTypeFilter(reqId, type) {
    _ddTypeFilter[reqId] = type;
    _renderSourcingDrillDown(reqId);
}
function _ddFilterSightings(reqId, field, value) {
    if (!_ddSightingFilters[reqId]) _ddSightingFilters[reqId] = {};
    _ddSightingFilters[reqId][field] = value;
    clearTimeout(_ddFilterTimers[reqId]);
    _ddFilterTimers[reqId] = setTimeout(() => {
        _renderSourcingDrillDown(reqId);
        // Restore focus to the input being typed in
        const inp = document.querySelector(`[data-sfilter="${reqId}-${field}"]`);
        if (inp) { inp.focus(); inp.selectionStart = inp.selectionEnd = inp.value.length; }
    }, 200);
}
function _ddShowVendorSuggestions(reqId, query) {
    const dd = document.getElementById('ddVendorAc-' + reqId);
    if (!dd) return;
    if (query.length < 1) { dd.classList.remove('open'); return; }
    const data = _ddSightingsCache[reqId] || {};
    const seen = new Set();
    const vendors = [];
    for (const [, group] of Object.entries(data)) {
        for (const s of (group.sightings || [])) {
            const vn = (s.vendor_name || '').trim();
            if (!vn) continue;
            const key = vn.toLowerCase();
            if (seen.has(key)) continue;
            if (!key.includes(query.toLowerCase())) continue;
            seen.add(key);
            vendors.push(vn);
            if (vendors.length >= 15) break;
        }
        if (vendors.length >= 15) break;
    }
    if (!vendors.length) { dd.classList.remove('open'); return; }
    dd.innerHTML = vendors.map(v => `<div class="ac-item" onmousedown="_ddSelectVendor(${reqId},'${esc(v.replace(/'/g, "\\'"))}')">${esc(v)}</div>`).join('');
    dd.classList.add('open');
}
function _ddSelectVendor(reqId, vendor) {
    const inp = document.querySelector('[data-sfilter="' + reqId + '-vendor"]');
    if (inp) inp.value = vendor;
    _ddFilterSightings(reqId, 'vendor', vendor);
    const dd = document.getElementById('ddVendorAc-' + reqId);
    if (dd) dd.classList.remove('open');
}
function _ddApplyFilters(sightings, reqId, groupLabel) {
    const f = _ddSightingFilters[reqId];
    const tf = _ddTypeFilter[reqId] || 'all';
    let result = sightings;
    if (f) {
        result = result.filter(s => {
            if (f.vendor && !(s.vendor_name || '').toLowerCase().includes(f.vendor.toLowerCase())) return false;
            if (f.source && !(s.source_type || '').toLowerCase().includes(f.source.toLowerCase())) return false;
            if (f.condition && !((s.condition || '') + ' ' + (s.date_code || '')).toLowerCase().includes(f.condition.toLowerCase())) return false;
            return true;
        });
    }
    if (tf !== 'all') {
        result = result.filter(s => {
            const isSub = groupLabel && s.mpn_matched && s.mpn_matched.trim().toUpperCase() !== groupLabel.trim().toUpperCase();
            if (tf === 'exact') return !isSub;
            if (tf === 'sub') return isSub;
            if (tf === 'available') return !s.is_unavailable;
            if (tf === 'na') return s.is_unavailable || s.qty_available == null || s.qty_available <= 0;
            return true;
        });
    }
    return result;
}
function _ddClearFilters(reqId) {
    delete _ddSightingFilters[reqId];
    delete _ddTypeFilter[reqId];
    _renderSourcingDrillDown(reqId);
}

function _ddVendorScoreRing(s) {
    if (s.is_authorized) {
        return `<span style="display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;border:2px solid var(--green);background:var(--green-light);font-size:7px;font-weight:700;color:var(--green);margin-right:4px;cursor:default;vertical-align:middle" title="Authorized Distributor">\u2713</span>`;
    }
    const vc = s.vendor_card || {};
    if (vc.is_new_vendor || vc.vendor_score == null) {
        return `<span style="display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;border:2px solid var(--muted);background:var(--card2);font-size:6px;font-weight:700;color:var(--muted);margin-right:4px;cursor:default;vertical-align:middle" title="New Vendor — no order history">NEW</span>`;
    }
    const vs = Math.round(vc.vendor_score);
    const color = vs >= 66 ? 'var(--green)' : vs >= 33 ? 'var(--amber)' : 'var(--red)';
    const bg = vs >= 66 ? 'var(--green-light)' : vs >= 33 ? 'var(--amber-light)' : 'var(--red-light)';
    const tier = vs >= 66 ? 'Proven' : vs >= 33 ? 'Developing' : 'Caution';
    return `<span style="display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;border:2px solid ${color};background:${bg};font-size:7px;font-weight:700;color:${color};margin-right:4px;cursor:default;vertical-align:middle" title="Vendor Score: ${vs}/100 (${tier}) — based on order history, response rate, and reliability">${vs}</span>`;
}

function _ddVendorLinkPill(s) {
    const sourceUrl = s.click_url || s.octopart_url || s.vendor_url || '';
    return sourceUrl ? `<a href="${escAttr(sourceUrl)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="View listing" style="color:var(--blue);font-size:12px;margin-left:4px;text-decoration:none">&#x1f517;</a>` : '';
}

function _ddVendorInlineBadges(s) {
    const vc = s.vendor_card || {};
    let html = '';
    if (vc.avg_rating != null) {
        html += `<span style="font-size:10px;margin-left:2px;vertical-align:middle"><span class="stars">\u2605</span>${vc.avg_rating}</span>`;
    }
    return html;
}

function _ddEvidenceBadge(tier) {
    if (!tier) return '';
    const cfg = {
        T1: { bg: '#dcfce7', color: '#166534', label: 'T1', tip: 'Authorized Distributor API' },
        T2: { bg: '#dbeafe', color: '#1e40af', label: 'T2', tip: 'Direct API' },
        T3: { bg: '#fef3c7', color: '#92400e', label: 'T3', tip: 'Marketplace' },
        T4: { bg: '#fce7f3', color: '#9d174d', label: 'T4', tip: 'AI Parsed (needs review)' },
        T5: { bg: '#e0e7ff', color: '#3730a3', label: 'T5', tip: 'AI Parsed (verified)' },
        T6: { bg: '#f3f4f6', color: '#374151', label: 'T6', tip: 'Manual Entry' },
        T7: { bg: '#f5f3ff', color: '#6b21a8', label: 'T7', tip: 'Historical' },
    };
    const c = cfg[tier] || { bg: '#f3f4f6', color: '#374151', label: tier, tip: tier };
    return ` <span style="font-size:8px;padding:1px 4px;border-radius:3px;background:${c.bg};color:${c.color};font-weight:700;cursor:default" title="${c.tip}">${c.label}</span>`;
}

function _ddScoreTooltip(s) {
    const sc = s.score_components;
    if (!sc) return s.score != null ? 'Score: ' + s.score : '';
    const bar = (label, val) => {
        const w = Math.min(100, Math.max(0, val));
        const color = w >= 66 ? 'var(--green)' : w >= 33 ? 'var(--amber)' : 'var(--red)';
        return label + ': ' + Math.round(val) + '/100';
    };
    return [
        'Score: ' + (s.score || 0) + '/100',
        bar('Trust', sc.trust),
        bar('Price', sc.price),
        bar('Qty', sc.qty),
        bar('Fresh', sc.freshness),
        bar('Complete', sc.completeness),
    ].join(' | ');
}

function _ddCopyContact(text, type) {
    navigator.clipboard.writeText(text).then(() => showToast(type + ' copied', 'success')).catch(e => console.warn('clipboard copy failed:', e));
}

function _ddRenderTierRows(sightings, reqId, sel, groupLabel, targetPrice, showContact) {
    let html = '';
    for (const s of sightings) {
        // Historical offer rows — rendered at same size as regular sightings
        if (s._historical) {
            const ho = s._ho;
            const hPrice = ho.unit_price != null ? '$' + parseFloat(ho.unit_price).toFixed(4) : '\u2014';
            const hSub = ho.is_substitute ? '<span class="badge b-sub">SUB</span> ' : '';
            const sAge = ho.created_at ? fmtRelative(ho.created_at) : '\u2014';
            const hQty = ho.qty_available != null ? Number(ho.qty_available).toLocaleString() : '\u2014';
            const hoJson = esc(JSON.stringify(ho));
            const safeHVName = (ho.vendor_name||'').replace(/'/g, "\\'");
            html += `<tr style="background:var(--hist-bg,#faf5ff)">
                <td style="text-align:center"><span style="font-size:9px;padding:2px 5px;border-radius:3px;background:#7c3aed;color:#fff;font-weight:700">HIST</span></td>
                <td><a onclick="event.stopPropagation();openVendorPopup('${safeHVName}')" style="cursor:pointer;font-weight:600">${esc(ho.vendor_name || '\u2014')}</a> <span style="color:var(--muted)">RFQ-${ho.from_requisition_id || '\u2014'}</span></td>
                ${showContact !== false ? '<td style="font-size:10px;color:var(--muted)">\u2014</td>' : ''}
                <td class="mono">${hSub}${esc(ho.mpn || '\u2014')}</td>
                <td class="mono">${hQty}</td>
                <td class="mono" style="color:var(--teal)">${hPrice}</td>
                <td style="font-size:10px">historical</td>
                <td>${esc(ho.condition || '\u2014')}</td>
                <td>${esc(ho.lead_time || '\u2014')}</td>
                <td style="color:var(--muted)">${sAge}
                    <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 6px;margin-left:4px" onclick="event.stopPropagation();ddReconfirmOffer(${ho.id},${reqId})" title="Mark as still valid">\u2713 Reconfirm</button>
                    <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 6px;color:var(--teal)" onclick='event.stopPropagation();ddLogFromHistorical(${reqId},${hoJson})' title="Log as new offer on this RFQ">+ Log</button>
                </td>
            </tr>`;
            continue;
        }
        const hasEmail = !!(s.vendor_email || (s.vendor_card && s.vendor_card.has_emails));
        const checked = sel.has(s.id) ? 'checked' : '';
        const dimStyle = !hasEmail ? 'opacity:.7' : '';
        const disabledAttr = !hasEmail ? 'disabled title="Vendor needs an email address before sending RFQ"' : '';
        const price = s.unit_price != null ? '$' + parseFloat(s.unit_price).toFixed(2) + (s.price_outlier ? ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:#fee2e2;color:#991b1b;font-weight:600" title="Suspected outlier">!</span>' : '') : '\u2014'; // nosec: numeric values only
        const qty = s.qty_available != null ? Number(s.qty_available).toLocaleString() : '\u2014';
        const safeVName = (s.vendor_name||'').replace(/'/g, "\\'");
        const needsEmail = !hasEmail ? ` <a onclick="event.stopPropagation();ddPromptVendorEmail(${reqId},${s.id},'${safeVName}')" style="color:var(--red);font-size:10px;cursor:pointer;font-weight:600" title="Click to add an email address for this vendor">+ add email</a>` : '';
        const ring = _ddVendorScoreRing(s);
        const linkPill = _ddVendorLinkPill(s);
        const inlineBadges = _ddVendorInlineBadges(s);
        const sAge = s.created_at ? fmtRelative(s.created_at) : '\u2014';
        const isSub = groupLabel && s.mpn_matched && s.mpn_matched.trim().toUpperCase() !== groupLabel.trim().toUpperCase();
        const subBadge = isSub ? '<span class="badge b-sub">SUB</span> ' : '';
        const unavail = s.is_unavailable;
        const unavailBadge = unavail ? ' <span class="badge b-unavail">NOT AVAIL</span>' : '';
        const unavailBtn = s.id
            ? `<button class="btn-unavail" onclick="event.stopPropagation();markUnavailable(${s.id},${!unavail},${reqId})" title="${unavail ? 'Mark as available again' : 'Mark as unavailable — vendor confirmed stock is sold or wrong part'}">${unavail ? '\u21a9 Available' : '\u2715 Unavailable'}</button>`
            : '';
        // Contact info
        const cEmail = s.vendor_email || (s.vendor_card && s.vendor_card.emails && s.vendor_card.emails[0]) || '';
        const cPhone = s.vendor_phone || (s.vendor_card && s.vendor_card.phones && s.vendor_card.phones[0]) || '';
        const truncEmail = cEmail.length > 20 ? cEmail.slice(0, 18) + '\u2026' : cEmail;
        const truncPhone = cPhone.length > 20 ? cPhone.slice(0, 18) + '\u2026' : cPhone;
        let contactHtml = '';
        if (cEmail) contactHtml += `<a href="mailto:${escAttr(cEmail)}" onclick="event.stopPropagation();_ddCopyContact('${escAttr(cEmail)}','Email')" title="${escAttr(cEmail)}" style="color:var(--muted);text-decoration:none">${esc(truncEmail)}</a>`;
        if (cEmail && cPhone) contactHtml += '<br>';
        if (cPhone) contactHtml += phoneLink(cPhone, {vendor_card_id: (s.vendor_card && s.vendor_card.id) || null, requirement_id: s.requirement_id || null, origin: 'sighting_row'});
        // Price color-coding vs target
        let priceColor = s.unit_price ? 'var(--teal)' : 'var(--muted)';
        let priceTitle = '';
        if (s.price_outlier) {
            priceColor = 'var(--red)';
            priceTitle = ' title="Price outlier — 20x+ above median market price"';
        } else if (targetPrice && s.unit_price) {
            const pctDelta = ((s.unit_price - targetPrice) / targetPrice) * 100;
            priceColor = pctDelta <= 0 ? 'var(--green)' : pctDelta <= 15 ? 'var(--amber)' : 'var(--red)';
            priceTitle = ` title="${pctDelta > 0 ? '+' : ''}${pctDelta.toFixed(0)}% vs target ($${Number(targetPrice).toFixed(2)})"`;
        }
        const rowBg = unavail ? 'background:rgba(220,38,38,.04);opacity:.6' : isSub ? 'background:rgba(14,116,144,.04)' : '';
        const staleOpacity = s.is_stale && !unavail ? 'opacity:0.55;' : '';
        html += `<tr style="${staleOpacity}${dimStyle}${rowBg ? ';' + rowBg : ''}">
            <td><input type="checkbox" ${checked} ${disabledAttr} onclick="event.stopPropagation();ddToggleSighting(${reqId},${s.id})"></td>
            <td>${ring}${s.vendor_card && s.vendor_card.id ? '<a onclick="event.stopPropagation();openVendorDrawer('+s.vendor_card.id+')" style="cursor:pointer;font-weight:600;color:var(--text);text-decoration:none" onmouseover="this.style.color=\'var(--blue)\'" onmouseout="this.style.color=\'var(--text)\'">' + esc(s.vendor_name || '\u2014') + '</a>' : '<a onclick="event.stopPropagation();openVendorPopupByName(\''+safeVName+'\')" style="cursor:pointer;font-weight:600;color:var(--text);text-decoration:none" onmouseover="this.style.color=\'var(--blue)\'" onmouseout="this.style.color=\'var(--text)\'">' + esc(s.vendor_name || '\u2014') + '</a>'}${inlineBadges}${linkPill}${needsEmail}${unavailBadge}</td>
            ${showContact !== false ? `<td style="font-size:10px;color:var(--muted);max-width:140px;overflow:hidden;text-overflow:ellipsis">${contactHtml || '\u2014'}</td>` : ''}
            <td class="mono">${subBadge}${esc(s.mpn_matched || '\u2014')}</td>
            <td class="mono">${qty}</td>
            <td class="mono" style="color:${priceColor}"${priceTitle}>${price}</td>
            <td style="font-size:10px">${esc(s.source_type || '\u2014')}${_ddEvidenceBadge(s.evidence_tier)}${s.merged_count > 1 ? ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:var(--blue-light,#e0f2fe);color:var(--blue,#0284c7);font-weight:600" title="Merged from ' + s.merged_count + ' duplicate listings' + (s.merged_sources ? ' (' + s.merged_sources.join(', ') + ')' : '') + '">' + s.merged_count + 'x</span>' : ''}</td>
            <td style="font-size:10px">${esc(s.condition || '\u2014')}${s.date_code ? ' <span style="color:var(--muted)">\u00b7 DC:' + esc(s.date_code) + '</span>' : ''}</td>
            <td style="font-size:10px">${esc(s.lead_time || '\u2014')}</td>
            <td style="font-size:10px;color:var(--muted)">${sAge} ${unavailBtn}${!s._historical && !unavail && hasEmail ? ` <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 5px;color:var(--teal)" onclick="event.stopPropagation();ddQuickRfq(${reqId},'${safeVName}','${escAttr(s.mpn_matched || '')}')" title="Send RFQ to this vendor">&#x2709;</button>` : ''}</td>
        </tr>`;
    }
    return html;
}

function _renderSourcingDrillDown(reqId, targetPanel) {
    const dd = targetPanel || (document.getElementById('d-' + reqId) || {}).querySelector?.('.dd-panel');
    if (!dd) return;
    const data = _ddSightingsCache[reqId] || {};
    const groups = Object.entries(data); // [ [reqId, {label, sightings}], ... ]
    if (!groups.length) { dd.innerHTML = '<span style="font-size:11px;color:var(--muted)">No sightings yet</span>'; return; }

    // Fetch per-requirement scores if not cached
    if (!_ddScoreCache[reqId]) {
        if (_ddScoreAborts[reqId]) try { _ddScoreAborts[reqId].abort(); } catch(e){}
        const ctrl = new AbortController();
        _ddScoreAborts[reqId] = ctrl;
        apiFetch(`/api/requisitions/${reqId}/sourcing-score`, { signal: ctrl.signal }).then(scores => {
            if (_currentMainView === 'archive') return; // stale — user left tab
            _ddScoreCache[reqId] = {};
            for (const rs of (scores.requirements || [])) {
                _ddScoreCache[reqId][rs.requirement_id] = rs;
            }
            _renderSourcingDrillDown(reqId); // re-render with scores
        }).catch(e => console.warn('score fetch error:', e));
        _ddScoreCache[reqId] = { _loading: true }; // sentinel to prevent duplicate fetches
    }
    const scoreMap = _ddScoreCache[reqId] || {};

    const sel = _ddSelectedSightings[reqId] || new Set();
    const DD_LIMIT = 250;
    const showAll = dd.dataset.showAll === '1';
    const f = _ddSightingFilters[reqId] || {};
    const tf = _ddTypeFilter[reqId] || 'all';
    const hasFilters = !!(f.vendor || f.source || f.condition || tf !== 'all');

    // Type filter pills
    const _tfPill = (val, label) => `<button class="src-type-pill${tf === val ? ' on' : ''}" onclick="event.stopPropagation();_ddSetTypeFilter(${reqId},'${val}')">${label}</button>`;

    // Filter bar
    let html = `<div style="display:flex;gap:6px;margin-bottom:8px;align-items:center;flex-wrap:wrap">
        <div class="src-type-pills">${_tfPill('all','All')}${_tfPill('exact','Exact')}${_tfPill('sub','Substitute')}${_tfPill('available','Available')}${_tfPill('na','N/A')}</div>
        <span style="position:relative"><input data-sfilter="${reqId}-vendor" placeholder="Filter vendor\u2026" value="${esc(f.vendor||'')}" oninput="_ddFilterSightings(${reqId},'vendor',this.value);_ddShowVendorSuggestions(${reqId},this.value)" onblur="setTimeout(()=>{const d=document.getElementById('ddVendorAc-${reqId}');if(d)d.classList.remove('open')},150)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;font-size:11px;width:130px;background:var(--card);color:var(--text)"><div id="ddVendorAc-${reqId}" class="ac-dropdown"></div></span>
        <input data-sfilter="${reqId}-source" placeholder="Filter source\u2026" value="${esc(f.source||'')}" oninput="_ddFilterSightings(${reqId},'source',this.value)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;font-size:11px;width:110px;background:var(--card);color:var(--text)">
        <input data-sfilter="${reqId}-condition" placeholder="Filter condition\u2026" value="${esc(f.condition||'')}" oninput="_ddFilterSightings(${reqId},'condition',this.value)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;font-size:11px;width:110px;background:var(--card);color:var(--text)">
        ${hasFilters ? `<a onclick="event.stopPropagation();_ddClearFilters(${reqId})" style="font-size:10px;color:var(--blue);cursor:pointer">\u2715 Clear</a>` : ''}
        <span style="margin-left:auto;display:flex;gap:6px;align-items:center">
            <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 8px" onclick="event.stopPropagation();ddExportSightingsCsv(${reqId})" title="Export sourcing results to CSV">&#x2B07; CSV</button>
            <span id="ddBulkHint-${reqId}" style="font-size:10px;color:var(--muted)">Select vendors to send RFQ</span>
            <button class="btn btn-primary btn-sm" id="ddBulkRfqBtn-${reqId}" style="display:none;font-size:10px" onclick="event.stopPropagation();ddSendBulkRfq(${reqId})">Prepare RFQ (0)</button>
            <button class="btn btn-sm" id="ddBuildQuoteSrc-${reqId}" style="display:none;font-size:10px;background:var(--bg3);color:var(--teal);border:1px solid var(--teal)" onclick="event.stopPropagation();ddBuildQuote(${reqId})">Build Quote (0)</button>
        </span>
    </div>`;

    let _groupIdx = 0;
    for (const [rId, group] of groups) {
        const allSightings = group.sightings || [];
        const label = group.label || 'Unknown MPN';

        // Separate aggregate (Octopart) from real vendor sightings
        const aggregates = allSightings.filter(s => (s.source_type || '').toLowerCase() === 'octopart');
        const sightings = allSightings.filter(s => (s.source_type || '').toLowerCase() !== 'octopart');

        // Merge historical offers into sightings as regular rows
        const histOffers = group.historical_offers || [];
        for (const ho of histOffers) {
            sightings.push({
                id: 'ho-' + ho.id,
                vendor_name: ho.vendor_name,
                mpn_matched: ho.mpn || label,
                qty_available: ho.qty_available,
                unit_price: ho.unit_price,
                source_type: 'historical',
                condition: ho.condition,
                lead_time: ho.lead_time,
                created_at: ho.created_at,
                is_substitute: ho.is_substitute,
                _historical: true,
                _ho: ho,
                vendor_card: {},
                score: 40,
            });
        }

        // Apply filters
        const filtered = _ddApplyFilters(sightings, reqId, label);

        // Look up target qty for this requirement group
        const _reqs = _ddReqCache[reqId] || [];
        const _req = _reqs.find(r => r.id == rId);
        const targetQty = _req?.target_qty || 0;

        // Sort: full-fill vendors first, then by score descending
        filtered.sort((a, b) => {
            const aFill = targetQty > 0 && a.qty_available >= targetQty ? 1 : 0;
            const bFill = targetQty > 0 && b.qty_available >= targetQty ? 1 : 0;
            if (aFill !== bFill) return bFill - aFill;
            const sa = a.vendor_card?.vendor_score ?? a.score ?? 0;
            const sb = b.vendor_card?.vendor_score ?? b.score ?? 0;
            return sb - sa;
        });

        // Per-requirement sourcing score dot with tooltip
        const rs = scoreMap[rId];
        let effortBadge = '';
        if (rs) {
            const dotColor = rs.color === 'green' ? 'var(--green)' : rs.color === 'yellow' ? 'var(--amber)' : 'var(--red)';
            effortBadge = ` <span class="effort-wrap" onclick="event.stopPropagation();this.classList.toggle('pinned')"><span class="effort-dot" style="background:${dotColor}"></span><span style="font-size:9px;color:var(--muted);margin-left:2px">${Math.round(rs.score)}</span>${_buildEffortTip(rs.score, rs.color, rs.signals)}</span>`;
        }
        // Look up target price from requirement cache
        const groupTargetPrice = _req?.target_price ?? null;
        const targetPriceLabel = groupTargetPrice != null ? ` \u00b7 target $${Number(groupTargetPrice).toFixed(2)}` : '';

        const filterNote = hasFilters ? ` <span style="font-size:10px;color:var(--blue)">(${filtered.length} of ${sightings.length} shown)</span>` : '';
        const _grpClass = _groupIdx % 2 === 1 ? 'src-group-alt' : '';
        _groupIdx++;
        html += `<div class="src-group ${_grpClass}" style="margin-bottom:10px;padding:8px;border-radius:6px">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
                <span style="font-size:11px;font-weight:700;color:var(--text2)">${esc(label)}${effortBadge} <span style="font-weight:400;color:var(--muted)">(${sightings.length} source${sightings.length !== 1 ? 's' : ''})${targetPriceLabel}</span>${filterNote}</span>
                <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 6px;margin-left:4px" onclick="event.stopPropagation();ddResearchPart(${reqId},${rId})" title="Re-search this part">\u21bb Search</button>
            </div>`;

        if (!filtered.length && !aggregates.length) {
            html += `<div style="font-size:11px;color:var(--muted);margin-bottom:6px">${hasFilters ? 'No matches for current filters' : 'No sources found'}</div></div>`;
            continue;
        }
        if (!filtered.length) {
            html += `<div style="font-size:11px;color:var(--muted);margin-bottom:6px">${hasFilters ? 'No matches for current filters' : 'No vendor listings yet \u2014 try searching'}</div></div>`;
            continue;
        }

        const visible = showAll ? filtered : filtered.slice(0, DD_LIMIT);
        const visibleIds = visible.filter(s => !s._historical && s.id && !!(s.vendor_email || (s.vendor_card && s.vendor_card.has_emails))).map(s => s.id);
        const allChecked = visibleIds.length > 0 && visibleIds.every(id => sel.has(id));
        const hasAnyContact = visible.some(s => s.vendor_email || (s.vendor_card && (s.vendor_card.has_emails || (s.vendor_card.emails && s.vendor_card.emails.length))) || s.vendor_phone || (s.vendor_card && s.vendor_card.phones && s.vendor_card.phones.length));
        html += `<table class="dtbl" style="margin:0"><thead><tr>
            <th style="width:24px"><input type="checkbox" ${allChecked ? 'checked' : ''} onchange="event.stopPropagation();ddToggleGroupSightings(${reqId},[${visibleIds.join(',')}],this.checked)" title="Select all in group"></th><th>Vendor</th>${hasAnyContact ? '<th>Contact</th>' : ''}<th>MPN</th><th>Qty</th><th>Price</th><th>Source</th><th>Condition</th><th>Lead</th><th>Age</th>
        </tr></thead><tbody>`;
        html += _ddRenderTierRows(visible, reqId, sel, label, groupTargetPrice, hasAnyContact);
        html += '</tbody></table>';
        if (!showAll && filtered.length > DD_LIMIT) {
            html += `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();this.closest('.dd-panel').dataset.showAll='1';_renderSourcingDrillDown(${reqId})" style="font-size:11px;margin:6px 0 0 12px;color:var(--blue)">Show all ${filtered.length} sources (${filtered.length - DD_LIMIT} more)</button>`;
        }

        // ── Inline Offers for this requirement group ──
        const _offData = _ddTabCache[reqId]?.offers;
        const _offGroups = _offData?.groups || _offData || [];
        const _myOffGroup = Array.isArray(_offGroups) ? _offGroups.find(og => String(og.requirement_id) === String(rId) || (og.mpn || '').toUpperCase() === label.toUpperCase()) : null;
        const _myOffers = (_myOffGroup?.offers || []).slice().sort((a, b) => (a.unit_price || 999999) - (b.unit_price || 999999));
        if (_myOffers.length) {
            const oSel = _ddSelectedOffers[reqId] || new Set();
            const oIds = _myOffers.map(o => o.id || o.offer_id);
            const oAllChecked = oIds.length > 0 && oIds.every(id => oSel.has(id));
            html += `<div class="inline-offers-section" style="margin-top:8px;border-top:2px solid var(--teal);padding-top:8px;background:rgba(14,116,144,.03);border-radius:0 0 6px 6px;padding:8px">
                <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
                    <span style="font-size:11px;font-weight:700;color:var(--teal)">&#x2709; ${_myOffers.length} Offer${_myOffers.length !== 1 ? 's' : ''}</span>
                    <span style="font-size:10px;color:var(--muted)">${oIds.filter(id => oSel.has(id)).length} selected</span>
                </div>
                <table class="dtbl"><thead><tr>
                    <th style="width:24px"><input type="checkbox" ${oAllChecked ? 'checked' : ''} onchange="event.stopPropagation();_ddToggleGroupInlineOffers(${reqId},[${oIds.join(',')}],this.checked)"></th>
                    <th>Vendor</th><th>MPN</th><th>Qty</th><th>Price</th><th>Lead</th><th>Cond</th><th>Status</th><th>Notes</th><th style="width:60px"></th>
                </tr></thead><tbody>`;
            for (const o of _myOffers) {
                const oid = o.id || o.offer_id;
                const offeredMpn = o.mpn || o.offered_mpn || '';
                const isSub = label && offeredMpn && offeredMpn.trim().toUpperCase() !== label.trim().toUpperCase();
                const subBadge = isSub ? '<span class="badge b-sub">SUB</span> ' : '';
                const oChecked = oSel.has(oid) ? 'checked' : '';
                const price = o.unit_price != null ? '$' + parseFloat(o.unit_price).toFixed(4) : '\u2014';
                const isPending = o.status === 'pending_review';
                const rowBg = isPending ? 'background:rgba(245,158,11,.06);' : (isSub ? 'background:rgba(14,116,144,.06);' : '');
                html += `<tr class="ofr-row" style="${rowBg}" data-oid="${oid}">
                    <td><input type="checkbox" ${oChecked} onclick="event.stopPropagation();ddToggleOffer(${reqId},${oid},event)"></td>
                    <td>${esc(o.vendor_name || '')}${isPending ? ' <span class="badge" style="background:var(--amber-light);color:var(--amber);font-size:9px">DRAFT</span>' : ''}</td>
                    <td class="mono">${subBadge}${esc(offeredMpn || '\u2014')}</td>
                    <td class="mono">${o.qty_available != null ? Number(o.qty_available).toLocaleString() : '\u2014'}</td>
                    <td class="mono" style="color:var(--teal)">${price}</td>
                    <td>${esc(o.lead_time || '\u2014')}</td>
                    <td>${esc(o.condition || '\u2014')}</td>
                    <td style="font-size:10px">${esc(o.status || '\u2014')}</td>
                    <td class="req-edit-cell" onclick="ddInlineEditOffer(${reqId},${oid},'notes',this)" style="font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis" title="${escAttr(o.notes || '')}">${esc(o.notes || '\u2014')}</td>
                    <td style="white-space:nowrap">${isPending ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();ddApproveOffer(${reqId},${oid})" title="Approve" style="padding:2px 6px;font-size:10px;color:var(--green)">\u2713</button><button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();ddRejectOffer(${reqId},${oid})" title="Reject" style="padding:2px 6px;font-size:10px;color:var(--red)">\u2715</button>` : `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();ddEditOffer(${reqId},${oid})" title="Edit" style="padding:2px 6px;font-size:10px">\u270e</button>`}</td>
                </tr>`;
            }
            html += '</tbody></table></div>';
        }

        html += '</div>';
    }

    // Update Build Quote button visibility
    const _oSelCount = (_ddSelectedOffers[reqId] || new Set()).size;
    const _bqBtn = document.getElementById('ddBuildQuoteSrc-' + reqId);
    if (_bqBtn) {
        _bqBtn.style.display = _oSelCount > 0 ? '' : 'none';
        _bqBtn.textContent = 'Build Quote (' + _oSelCount + ')';
    }

    dd.innerHTML = html;
}

function ddToggleGroupSightings(reqId, ids, checked) {
    const sel = _ddSelectedSightings[reqId];
    if (!sel) return;
    for (const id of ids) {
        if (checked) sel.add(id); else sel.delete(id);
    }
    _renderSourcingDrillDown(reqId);
    _updateDdBulkButton(reqId);
}

function _ddToggleGroupInlineOffers(reqId, ids, checked) {
    if (!_ddSelectedOffers[reqId]) _ddSelectedOffers[reqId] = new Set();
    const sel = _ddSelectedOffers[reqId];
    for (const id of ids) {
        if (checked) sel.add(id); else sel.delete(id);
    }
    _renderSourcingDrillDown(reqId);
}

function ddQuickRfq(reqId, vendorName, mpn) {
    setCurrentReqId(reqId);
    openBatchRfqModal([{ vendor_name: vendorName, parts: [mpn] }]);
}

function ddToggleSighting(reqId, sightingId) {
    const sel = _ddSelectedSightings[reqId];
    if (!sel) return;
    if (sel.has(sightingId)) sel.delete(sightingId);
    else sel.add(sightingId);
    // Update checkbox without full re-render
    const drow = document.getElementById('d-' + reqId);
    if (drow) {
        const cb = drow.querySelector(`input[type="checkbox"][onclick*="ddToggleSighting(${reqId},${sightingId})"]`);
        if (cb) cb.checked = sel.has(sightingId);
    }
    _updateDdBulkButton(reqId);
}

function _updateDdBulkButton(reqId) {
    const btn = document.getElementById('bulkRfqBtn-' + reqId);
    const btn2 = document.getElementById('ddBulkRfqBtn-' + reqId);
    const sel = _ddSelectedSightings[reqId];
    const count = sel ? sel.size : 0;
    // Group selected sightings by normalized vendor name
    const data = _ddSightingsCache[reqId] || {};
    const vendorMap = {}; // normalized name -> { hasEmail: bool, parts: Set }
    for (const [, group] of Object.entries(data)) {
        for (const s of (group.sightings || [])) {
            if (!sel || !sel.has(s.id)) continue;
            const vn = (s.vendor_name || '').trim().toLowerCase();
            if (!vn || vn === 'no seller listed') continue;
            const hasEmail = !!(s.vendor_email || (s.vendor_card && s.vendor_card.has_emails));
            if (!vendorMap[vn]) vendorMap[vn] = { hasEmail: false, parts: new Set() };
            if (hasEmail) vendorMap[vn].hasEmail = true;
            if (s.mpn) vendorMap[vn].parts.add(s.mpn);
        }
    }
    const totalVendors = Object.keys(vendorMap).length;
    const withEmail = Object.values(vendorMap).filter(v => v.hasEmail).length;
    const totalParts = new Set();
    Object.values(vendorMap).forEach(v => v.parts.forEach(p => totalParts.add(p)));
    const vLabel = totalVendors === 1 ? 'vendor' : 'vendors';
    const label = withEmail < totalVendors
        ? `Prepare RFQ (${withEmail} of ${totalVendors} ${vLabel})`
        : `Prepare RFQ (${totalVendors} ${vLabel})`;
    for (const b of [btn, btn2]) {
        if (!b) continue;
        b.style.display = count > 0 ? '' : 'none';
        b.textContent = label;
    }
    const hint = document.getElementById('ddBulkHint-' + reqId);
    if (hint) hint.style.display = count > 0 ? 'none' : '';

    // Update inline RFQ sticky bar
    _updateInlineRfqBar(reqId, totalVendors, totalParts.size, count);
}

function ddPromptVendorEmail(reqId, sightingId, vendorName) {
    // Show inline email input instead of prompt()
    const row = document.querySelector(`input[onclick*="ddToggleSighting(${reqId},${sightingId})"]`);
    const cell = row ? row.closest('tr')?.querySelector('td:nth-child(2)') : null;
    if (!cell) { _ddPromptFallback(reqId, sightingId, vendorName); return; }
    const existing = cell.querySelector('.dd-email-inline');
    if (existing) { existing.querySelector('input').focus(); return; }
    const wrap = document.createElement('span');
    wrap.className = 'dd-email-inline';
    wrap.style.cssText = 'display:inline-flex;gap:4px;margin-left:6px;align-items:center';
    wrap.innerHTML = `<input type="email" placeholder="email@vendor.com" style="width:140px;padding:2px 6px;border:1px solid var(--teal);border-radius:3px;font-size:11px">
        <button class="btn btn-sm" style="padding:1px 6px;font-size:10px" onclick="event.stopPropagation();_ddSaveEmail(${reqId},${sightingId},'${vendorName.replace(/'/g,"\\'")}',this.previousElementSibling.value)">Save</button>`;
    cell.appendChild(wrap);
    const inp = wrap.querySelector('input');
    inp.focus();
    inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); _ddSaveEmail(reqId, sightingId, vendorName, inp.value); }
        if (e.key === 'Escape') { wrap.remove(); }
    });
}
async function _ddSaveEmail(reqId, sightingId, vendorName, email) {
    if (!email || !email.trim()) return;
    const trimmed = email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) { showToast('Invalid email address', 'error'); return; }
    try {
        await apiFetch('/api/vendor-card/add-email', {
            method: 'POST', body: { vendor_name: vendorName, email: trimmed }
        });
        showToast(`Email added for ${vendorName}`, 'success');
        const data = _ddSightingsCache[reqId] || {};
        for (const [rId, group] of Object.entries(data)) {
            for (const s of (group.sightings || [])) {
                if (s.id === sightingId) { s.vendor_email = trimmed; break; }
            }
        }
        _renderSourcingDrillDown(reqId);
    } catch(e) {
        showToast('Failed to save email', 'error');
    }
}
function _ddPromptFallback(reqId, sightingId, vendorName) {
    promptInput('Enter Email', 'Email for ' + vendorName + ':', function(email) {
        if (email) _ddSaveEmail(reqId, sightingId, vendorName, email);
    }, {inputType: 'email', placeholder: 'vendor@example.com'});
}

function ddSendBulkRfq(reqId) {
    const sel = _ddSelectedSightings[reqId];
    if (!sel || !sel.size) { showToast('Select sightings first', 'warn'); return; }
    const data = _ddSightingsCache[reqId] || {};
    // Collect selected sightings and group by vendor
    const groups = {};
    for (const [rId, group] of Object.entries(data)) {
        for (const s of (group.sightings || [])) {
            if (!sel.has(s.id)) continue;
            const vKey = (s.vendor_name || '').trim().toLowerCase();
            if (!vKey || vKey === 'no seller listed') continue;
            if (!groups[vKey]) groups[vKey] = { vendor_name: s.vendor_name, parts: [] };
            const part = s.mpn_matched || group.label;
            if (!groups[vKey].parts.includes(part)) groups[vKey].parts.push(part);
        }
    }
    const vendorGroups = Object.values(groups);
    if (!vendorGroups.length) { showToast('No valid vendors selected', 'error'); return; }
    currentReqId = reqId;
    openBatchRfqModal(vendorGroups);
}

// ── Inline RFQ Bar ──────────────────────────────────────────────────────
// Shows a sticky bottom bar when sightings are selected, providing a clear
// path to sending RFQs without the hidden drawer pattern.
// Called by: _updateDdBulkButton (when sighting selection changes)
// Depends on: _ddSelectedSightings, _ddSightingsCache, ddSendBulkRfq

function _updateInlineRfqBar(reqId, vendorCount, partCount, sightingCount) {
    const existingBar = document.getElementById('inlineRfqBar-' + reqId);

    if (sightingCount === 0) {
        if (existingBar) existingBar.remove();
        return;
    }

    const vLabel = vendorCount === 1 ? 'vendor' : 'vendors';
    const pLabel = partCount === 1 ? 'part' : 'parts';
    const barHtml = `
        <div class="rfq-inline-bar" id="inlineRfqBar-${reqId}">
            <span class="rfq-bar-count">${sightingCount} selected</span>
            <span style="color:var(--muted);font-size:12px">${vendorCount} ${vLabel} \u00b7 ${partCount} ${pLabel}</span>
            <span style="flex:1"></span>
            <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();_clearSightingSelection(${reqId})" title="Clear selection">Clear</button>
            <button class="btn btn-primary" onclick="event.stopPropagation();ddSendBulkRfq(${reqId})" title="Compose and send RFQs to selected vendors">Send RFQs \u2192</button>
        </div>`;

    if (existingBar) {
        existingBar.outerHTML = barHtml;
    } else {
        // Append to the drill-down panel
        const drow = document.getElementById('d-' + reqId);
        const panel = drow?.querySelector('.dd-panel');
        if (panel) panel.insertAdjacentHTML('beforeend', barHtml);
    }
}

function _clearSightingSelection(reqId) {
    const sel = _ddSelectedSightings[reqId];
    if (sel) sel.clear();
    _renderSourcingDrillDown(reqId);
    _updateDdBulkButton(reqId);
}

function ddExportSightingsCsv(reqId) {
    const data = _ddSightingsCache[reqId] || {};
    const rows = [['MPN','Vendor','Qty','Price','Source','Condition','Lead Time','Date']];
    for (const [, group] of Object.entries(data)) {
        for (const s of (group.sightings || [])) {
            rows.push([
                s.mpn_matched || '',
                s.vendor_name || '',
                s.qty_available != null ? String(s.qty_available) : '',
                s.unit_price != null ? String(s.unit_price) : '',
                s.source_type || '',
                s.condition || '',
                s.lead_time || '',
                s.created_at ? new Date(s.created_at).toLocaleDateString() : '',
            ]);
        }
    }
    const csv = rows.map(r => r.map(c => '"' + String(c).replace(/"/g, '""') + '"').join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'sourcing-RFQ-' + reqId + '.csv';
    a.click();
    URL.revokeObjectURL(url);
    showToast('CSV exported', 'success');
}

// ── Re-search parts from sourcing drill-down ────────────────────────────

function _ddSearchOverlay(reqId, show, text) {
    const dd = (document.getElementById('d-' + reqId) || {}).querySelector?.('.dd-panel');
    if (!dd) return;
    let ov = dd.querySelector('.dd-search-overlay');
    if (show) {
        if (!ov) {
            ov = document.createElement('div');
            ov.className = 'dd-search-overlay';
            dd.style.position = 'relative';
            dd.appendChild(ov);
        }
        ov.innerHTML = `<span class="dd-search-spinner"></span> ${esc(text || 'Searching\u2026')}`;
        ov.style.display = 'flex';
    } else if (ov) {
        ov.style.display = 'none';
    }
}

async function ddResearchPart(reqId, requirementId) {
    _ddSearchOverlay(reqId, true, 'Searching part\u2026');
    try {
        const body = { requirement_ids: [requirementId] };
        await apiFetch(`/api/requisitions/${reqId}/search`, { method: 'POST', body });
        // Invalidate caches and re-render
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].sightings;
        delete _ddSightingsCache[reqId];
        delete _ddScoreCache[reqId];
        // Clear tier expand/collapse state for this requisition
        for (const k of Object.keys(_ddTierState)) { if (k.startsWith(reqId + '-')) delete _ddTierState[k]; }
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) reqInfo.last_searched_at = new Date().toISOString();
        // Re-load sightings and re-render
        const data = await apiFetch(`/api/requisitions/${reqId}/sightings`);
        _ddSightingsCache[reqId] = data;
        if (!_ddSelectedSightings[reqId]) _ddSelectedSightings[reqId] = new Set();
        _ddSearchOverlay(reqId, false);
        _renderSourcingDrillDown(reqId);
        showToast('Search complete', 'success');
    } catch(e) {
        _ddSearchOverlay(reqId, false);
        showToast('Search failed — ' + friendlyError(e, 'please try again'), 'error');
    }
}

async function ddResearchAll(reqId) {
    const btn = event ? event.target.closest('button') : null;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="dd-search-spinner" style="width:12px;height:12px;border-width:2px"></span> Searching\u2026'; }
    _ddSearchOverlay(reqId, true, 'Searching all parts\u2026');
    try {
        const reqs = _ddReqCache[reqId] || await apiFetch(`/api/requisitions/${reqId}/requirements`);
        _ddReqCache[reqId] = reqs;
        if (!reqs.length) { showToast('No parts to search', 'warn'); return; }
        const body = { requirement_ids: reqs.map(r => r.id) };
        await apiFetch(`/api/requisitions/${reqId}/search`, { method: 'POST', body });
        // Invalidate and re-load
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].sightings;
        delete _ddSightingsCache[reqId];
        delete _ddScoreCache[reqId];
        // Clear tier expand/collapse state for this requisition
        for (const k of Object.keys(_ddTierState)) { if (k.startsWith(reqId + '-')) delete _ddTierState[k]; }
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) reqInfo.last_searched_at = new Date().toISOString();
        const data = await apiFetch(`/api/requisitions/${reqId}/sightings`);
        _ddSightingsCache[reqId] = data;
        if (!_ddSelectedSightings[reqId]) _ddSelectedSightings[reqId] = new Set();
        _ddSearchOverlay(reqId, false);
        _renderSourcingDrillDown(reqId);
        renderReqList();
        showToast('All parts re-searched', 'success');
    } catch(e) {
        _ddSearchOverlay(reqId, false);
        showToast('Search failed — ' + friendlyError(e, 'please try again'), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '&#x1f50d; Search All'; }
    }
}

// ── Log Offer Modal ─────────────────────────────────────────────────────
async function openLogOfferFromList(reqId) {
    const loReqId = document.getElementById('loReqId'); if (loReqId) loReqId.value = reqId;
    // Show RFQ context banner
    const reqInfo = _reqListData.find(r => r.id === reqId);
    const ctxEl = document.getElementById('loReqContext');
    if (ctxEl && reqInfo) {
        const custName = reqInfo.customer_name || reqInfo.name || 'REQ-' + String(reqId).padStart(3, '0');
        ctxEl.innerHTML = `<span style="font-size:11px;color:var(--muted)">For</span> <b>${esc(custName)}</b> <span class="mono" style="font-size:11px;color:var(--muted)">(REQ-${String(reqId).padStart(3,'0')})</span>`;
        ctxEl.style.display = '';
    } else if (ctxEl) {
        ctxEl.style.display = 'none';
    }
    // Load requirements to populate part picker
    const reqs = _ddReqCache[reqId] || await apiFetch(`/api/requisitions/${reqId}/requirements`).catch(e => { showToast('Failed to load requirements','warn'); return []; });
    _ddReqCache[reqId] = reqs;
    const sel = document.getElementById('loReqPart');
    if (sel) {
        sel.innerHTML = '<option value="">Select part...</option>';
        for (const r of (reqs || [])) {
            sel.innerHTML += `<option value="${r.id}" data-mpn="${escAttr(r.primary_mpn || '')}">${esc(r.primary_mpn || 'Part #' + r.id)}${r.target_qty ? ' (qty ' + r.target_qty + ')' : ''}</option>`;
        }
        // Auto-select if only one part
        if (reqs && reqs.length === 1) sel.value = String(reqs[0].id);
    }
    // Clear form fields
    const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    _s('loVendor', ''); _s('loQty', ''); _s('loPrice', ''); _s('loLead', '');
    _s('loMoq', ''); _s('loCond', 'new'); _s('loDc', ''); _s('loPkg', '');
    _s('loMfr', ''); _s('loWarranty', ''); _s('loCOO', ''); _s('loNotes', '');
    openModal('logOfferModal', 'loVendor');
}

function closeLogOfferModal() {
    closeModal('logOfferModal');
    const dd = document.getElementById('loVendorSuggestions');
    if (dd) dd.classList.remove('open');
}

// ── Vendor autocomplete for Log Offer ───────────────────────────────────
let _loVendorDebounce = null;
let _loVendorCache = {};
let _loAcIndex = -1;
let _loVendorCardId = null;

function _initLoVendorAutocomplete() {
    const input = document.getElementById('loVendor');
    const dropdown = document.getElementById('loVendorSuggestions');
    if (!input || !dropdown) return;
    input.addEventListener('input', () => {
        clearTimeout(_loVendorDebounce);
        _loVendorCardId = null; // Clear selection on manual edit
        const q = input.value.trim();
        if (q.length < 2) { dropdown.classList.remove('open'); return; }
        _loVendorDebounce = setTimeout(() => _loVendorSearch(q), 250);
    });
    input.addEventListener('keydown', (e) => {
        const items = dropdown.querySelectorAll('.ac-item');
        if (!items.length || !dropdown.classList.contains('open')) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); _loAcIndex = Math.min(_loAcIndex + 1, items.length - 1); _loHighlight(items); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); _loAcIndex = Math.max(_loAcIndex - 1, 0); _loHighlight(items); }
        else if (e.key === 'Enter' && _loAcIndex >= 0) { e.preventDefault(); items[_loAcIndex].click(); }
        else if (e.key === 'Escape') { dropdown.classList.remove('open'); }
    });
    document.addEventListener('click', (e) => {
        if (!dropdown.contains(e.target) && e.target !== input) dropdown.classList.remove('open');
    });
}

function _loHighlight(items) {
    items.forEach((it, i) => it.classList.toggle('ac-active', i === _loAcIndex));
}

async function _loVendorSearch(q) {
    const dropdown = document.getElementById('loVendorSuggestions');
    if (_loVendorCache[q]) { _loRenderSuggestions(_loVendorCache[q]); return; }
    try {
        const data = await apiFetch(`/api/autocomplete/names?q=${encodeURIComponent(q)}&limit=10`);
        const vendors = (data || []).filter(r => r.type === 'vendor').filter(r => r.id && r.name);
        _loVendorCache[q] = vendors;
        _loRenderSuggestions(vendors);
    } catch {
        dropdown.classList.remove('open');
    }
}

function _loRenderSuggestions(vendors) {
    const dropdown = document.getElementById('loVendorSuggestions');
    const input = document.getElementById('loVendor');
    _loAcIndex = -1;
    if (!vendors.length) {
        dropdown.innerHTML = '<div class="ac-empty">No vendors found</div>';
        dropdown.classList.add('open');
        return;
    }
    dropdown.innerHTML = vendors.map(v => `<div class="ac-item" data-id="${v.id}">${esc(v.name)}</div>`).join('');
    dropdown.classList.add('open');
    dropdown.querySelectorAll('.ac-item').forEach(item => {
        item.addEventListener('click', () => {
            input.value = item.textContent;
            _loVendorCardId = parseInt(item.dataset.id) || null;
            dropdown.classList.remove('open');
            // Focus next field
            const qty = document.getElementById('loQty');
            if (qty) qty.focus();
        });
    });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', _initLoVendorAutocomplete);

async function submitLogOffer() {
    const _v = id => document.getElementById(id)?.value || '';
    const reqId = parseInt(_v('loReqId'));
    const partSel = document.getElementById('loReqPart');
    const reqPartId = partSel?.value ? parseInt(partSel.value) : null;
    const mpn = partSel?.selectedOptions[0]?.dataset?.mpn || partSel?.selectedOptions[0]?.textContent || '';
    const vendor = _v('loVendor').trim();
    if (!vendor) { showToast('Vendor name is required', 'error'); return; }
    if (!mpn) { showToast('Select a part', 'error'); return; }
    const btn = document.getElementById('loSubmitBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }
    try {
        const body = {
            mpn: mpn,
            vendor_name: vendor,
            vendor_card_id: _loVendorCardId || null,
            requirement_id: reqPartId,
            qty_available: parseInt(_v('loQty')) || null,
            unit_price: parseFloat(_v('loPrice')) || null,
            lead_time: _v('loLead').trim() || null,
            moq: parseInt(_v('loMoq')) || null,
            condition: _v('loCond') || 'new',
            date_code: _v('loDc').trim() || null,
            packaging: _v('loPkg').trim() || null,
            manufacturer: _v('loMfr').trim() || null,
            warranty: _v('loWarranty').trim() || null,
            country_of_origin: _v('loCOO').trim() || null,
            notes: _v('loNotes').trim() || null,
            source: 'manual',
            status: 'active',
        };
        await apiFetch(`/api/requisitions/${reqId}/offers`, { method: 'POST', body });
        _loVendorCardId = null;
        closeLogOfferModal();
        showToast('Offer logged', 'success');
        // Invalidate caches and refresh list
        if (_ddTabCache[reqId]) { delete _ddTabCache[reqId].offers; }
        // Update offer count in list data
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) {
            reqInfo.reply_count = (reqInfo.reply_count || 0) + 1;
            reqInfo.offer_count = (reqInfo.offer_count || 0) + 1;
            reqInfo.has_new_offers = true;
        }
        renderReqList();
    } catch(e) {
        showToast('Couldn\'t log offer — ' + friendlyError(e, 'please try again'), 'error');
    } finally {
        btn.disabled = false; btn.textContent = 'Log Offer';
    }
}

function renderReqList() {
    _renderBreadcrumb();
    // Deal board view uses its own renderer
    if (_currentMainView === 'deals') { _renderDealBoard(); return; }
    // Remember which drill-downs were open so we can restore them after re-render
    const _openDrillIds = [...document.querySelectorAll('.drow.open')].map(r => parseInt(r.id.replace('d-', ''))).filter(Boolean);
    const el = document.getElementById('reqList');
    let data = _reqListData;
    // When server search is active, skip status/text filters (server already filtered)
    if (!_serverSearchActive) {
        if (_reqStatusFilter === 'all') {
            const hide = ['archived', 'won', 'lost', 'closed'];
            data = data.filter(r => !hide.includes(r.status));
        } else if (_reqStatusFilter === 'archive') {
            // Backend already returned only archived/won/lost
        } else if (_reqStatusFilter === 'quoted') {
            data = data.filter(r => r.status === 'quoting' || r.status === 'quoted');
        } else if (_reqStatusFilter === 'active') {
            data = data.filter(r => r.status === 'active');
        } else {
            data = data.filter(r => r.status === _reqStatusFilter);
        }
    }
    if (_filterUserId) {
        data = data.filter(r => r.created_by === _filterUserId || r.sales_user_id === _filterUserId);
    } else if (_myReqsOnly && window.userId) {
        data = data.filter(r => r.created_by === window.userId);
    }
    // Apply filter panel filters
    data = applyDropdownFilters(data);

    // Sort — column sort takes priority, then dropdown sort
    if (_reqSortCol) {
        data = [...data].sort((a, b) => {
            let va, vb;
            switch (_reqSortCol) {
                case 'name': va = (a.customer_display || a.name || ''); vb = (b.customer_display || b.name || ''); break;
                case 'reqs': va = a.requirement_count || 0; vb = b.requirement_count || 0; break;
                case 'sourced': va = a.sourced_count || 0; vb = b.sourced_count || 0; break;
                case 'offers': va = a.reply_count || 0; vb = b.reply_count || 0; break;
                case 'quote': { const qo = {won:4,sent:3,revised:2,draft:1,lost:0}; va = qo[a.quote_status] ?? -1; vb = qo[b.quote_status] ?? -1; break; }
                case 'status': va = a.status || ''; vb = b.status || ''; break;
                case 'sales': va = a.created_by_name || ''; vb = b.created_by_name || ''; break;
                case 'age': va = a.created_at || ''; vb = b.created_at || ''; break;
                case 'deadline': va = a.deadline === 'ASAP' ? '9999-12-31' : (a.deadline || '9999-12-31'); vb = b.deadline === 'ASAP' ? '9999-12-31' : (b.deadline || '9999-12-31'); break;
                case 'sent': va = a.rfq_sent_count || 0; vb = b.rfq_sent_count || 0; break;
                case 'resp': { const sa = a.rfq_sent_count || 0; const sb = b.rfq_sent_count || 0; va = sa > 0 ? (a.reply_count || 0) / sa : 0; vb = sb > 0 ? (b.reply_count || 0) / sb : 0; break; }
                case 'searched': va = a.last_searched_at || ''; vb = b.last_searched_at || ''; break;
                case 'matches': va = a.proactive_match_count || 0; vb = b.proactive_match_count || 0; break;
                case 'score': va = a.sourcing_score || 0; vb = b.sourcing_score || 0; break;
                case 'coverage': {
                    const ta = a.requirement_count || 0; const tb = b.requirement_count || 0;
                    va = ta > 0 ? (a.offer_count || 0) / ta : 0;
                    vb = tb > 0 ? (b.offer_count || 0) / tb : 0;
                    break;
                }
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _reqSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _reqSortDir === 'asc' ? va - vb : vb - va;
        });
    } else {
        const sort = _reqListSort;
        if (sort === 'oldest') data = [...data].sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
        else if (sort === 'name-az') data = [...data].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        else if (sort === 'name-za') data = [...data].sort((a, b) => (b.name || '').localeCompare(a.name || ''));
        else if (sort === 'parts') data = [...data].sort((a, b) => (b.requirement_count || 0) - (a.requirement_count || 0));
        else if (sort === 'replies') data = [...data].sort((a, b) => (b.reply_count || 0) - (a.reply_count || 0));
        else if (sort === 'customer') data = [...data].sort((a, b) => (a.customer_display || '').localeCompare(b.customer_display || ''));
        else if (sort === 'last-searched') data = [...data].sort((a, b) => new Date(b.last_searched_at || 0) - new Date(a.last_searched_at || 0));
        else data = [...data].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    }

    // Update counts
    const v = _currentMainView;
    const countEl = document.getElementById('reqStatusCount');
    if (countEl) countEl.textContent = `${data.length}`;
    // Show shared count hint for sourcing view
    const hintEl = document.getElementById('viewHint');
    if (hintEl) hintEl.remove();

    if (!data.length) {
        if (_currentMainView === 'archive') {
            el.innerHTML = '<div class="empty" style="text-align:center;padding:40px 20px"><p style="font-size:14px;font-weight:600;margin-bottom:8px">No archived requisitions</p><p style="font-size:12px;color:var(--muted)">Completed or closed requisitions will appear here. Use the <b>Archive</b> button on an open req to move it here.</p></div>';
        } else {
            const viewLabel = (v === 'reqs' || v === 'deals') ? 'pipeline' : '';
            const labels = {all:'',draft:'Draft',active:'Active',offers:'Offers',quoted:'Quoted'};
            el.innerHTML = '<p class="empty">No ' + (labels[_reqStatusFilter] || viewLabel) + ' requisitions</p>';
        }
        return;
    }

    // Tab-aware table headers
    const thClass = (col) => _reqSortCol === col ? ' class="sorted"' : '';
    const sa = (col) => `<span class="sort-arrow">${_sortArrow(col)}</span>`;
    const _thIcons = `<th style="width:140px;text-align:right"><select id="userFilterSelect" class="vflt" onchange="setUserFilter(this.value)" title="Filter by user" style="font-size:10px;max-width:100px"></select> <span style="position:relative;display:inline-block"><button class="btn-icon" onclick="event.stopPropagation();_toggleColGear()" title="Show/hide columns" style="font-size:14px;cursor:pointer;background:none;border:none;color:var(--muted);padding:0 2px">&#x2699;</button><span id="colGearWrap"></span></span></th>`;
    let thead;
    if (v === 'archive') {
        thead = `<thead><tr>
            <th style="width:36px;cursor:pointer;font-size:10px" onclick="toggleAllDrillRows()" id="ddToggleAll">\u25b6</th>
            <th onclick="sortReqList('name')"${thClass('name')} style="min-width:200px">Requirement ${sa('name')}</th>
            <th onclick="sortReqList('reqs')"${thClass('reqs')}>Parts ${sa('reqs')}</th>
            <th onclick="sortReqList('offers')"${thClass('offers')}>Offers ${sa('offers')}</th>
            <th onclick="sortReqList('status')"${thClass('status')} title="Final outcome — Won, Lost, or Closed">Outcome ${sa('status')}</th>
            <th onclick="sortReqList('matches')"${thClass('matches')} title="Proactive material matches found">Matches ${sa('matches')}</th>
            <th onclick="sortReqList('sales')"${thClass('sales')}>Sales ${sa('sales')}</th>
            <th onclick="sortReqList('age')"${thClass('age')}>Age ${sa('age')}</th>
            ${_thIcons}
        </tr></thead>`;
    } else {
        // Pipeline view (reqs/deals): Part coverage, RFQs, response rate prominent
        thead = `<thead><tr>
            <th style="width:50px;font-size:10px"><input type="checkbox" id="batchSelectAll" onclick="_toggleBatchSelectAll(this)" title="Select all" style="vertical-align:middle;margin-right:4px"><span style="cursor:pointer" onclick="toggleAllDrillRows()" id="ddToggleAll">\u25b6</span></th>
            <th onclick="sortReqList('name')"${thClass('name')} style="min-width:200px">Customer ${sa('name')}</th>
            <th onclick="sortReqList('reqs')"${thClass('reqs')}>Parts ${sa('reqs')}</th>
            <th onclick="sortReqList('sourced')"${thClass('sourced')} title="Parts with at least one supplier sighting found">Sourced ${sa('sourced')}</th>
            <th onclick="sortReqList('coverage')"${thClass('coverage')} style="min-width:70px" title="Total offers vs total parts — can exceed 100% when multiple offers cover the same part">Coverage ${sa('coverage')}</th>
            <th onclick="sortReqList('sent')"${thClass('sent')} title="RFQs sent to vendors">RFQs ${sa('sent')}</th>
            <th onclick="sortReqList('resp')"${thClass('resp')} title="Vendor response rate">Response ${sa('resp')}</th>
            <th onclick="sortReqList('offers')"${thClass('offers')} title="Confirmed vendor offers">Offers ${sa('offers')}</th>
            <th onclick="sortReqList('sales')"${thClass('sales')}>Sales ${sa('sales')}</th>
            <th onclick="sortReqList('age')"${thClass('age')}>Age ${sa('age')}</th>
            ${_thIcons}
        </tr></thead>`;
    }

    // Priority lane grouping for sales/sourcing views (skip if column sort is active)
    let rowsHtml;
    if (v === 'archive' && !_reqSortCol) {
        // Group by customer when no column sort is active
        const groups = new Map();
        for (const r of data) {
            const key = r.company_id || r.customer_display || 'Unknown';
            if (!groups.has(key)) groups.set(key, { label: r.customer_display || r.name || 'Unknown', company_id: r.company_id, reqs: [] });
            groups.get(key).reqs.push(r);
        }
        let html = '';
        for (const [key, g] of groups) {
            const isOpen = _archiveGroupsOpen.has(key);
            const wonTotal = g.reqs.reduce((s, r) => s + (r.quote_won_value || 0), 0);
            const wonStr = wonTotal > 0 ? ` | Won: ${fmtDollars(wonTotal)}` : '';
            html += `<tr class="archive-group-header" onclick="toggleArchiveGroup('${String(key).replace(/'/g, "\\'")}')">
                <td colspan="9"><span style="margin-right:6px">${isOpen ? '\u25bc' : '\u25b6'}</span><b>${esc(g.label)}</b> <span style="font-size:11px;color:var(--muted)">(${g.reqs.length} req${g.reqs.length !== 1 ? 's' : ''}${wonStr})</span></td>
            </tr>`;
            if (isOpen) html += g.reqs.map(r => _renderReqRow(r)).join('');
        }
        rowsHtml = html;
    } else {
        rowsHtml = data.map(r => _renderReqRow(r)).join('');
    }
    var loadMoreHtml = '';
    if (_currentMainView === 'archive' && _archiveTotal > _archivePageSize) {
        const totalPages = Math.ceil(_archiveTotal / _archivePageSize);
        let pgHtml = `<span style="font-size:12px;color:var(--muted)">${_archiveTotal} archived &middot; Page ${_archivePage} of ${totalPages}</span>`;
        if (_archivePage > 1) pgHtml = `<button class="btn btn-ghost btn-sm" onclick="archiveGoPage(${_archivePage - 1})">&laquo; Prev</button> ` + pgHtml;
        if (_archivePage < totalPages) pgHtml += ` <button class="btn btn-ghost btn-sm" onclick="archiveGoPage(${_archivePage + 1})">Next &raquo;</button>`;
        loadMoreHtml = `<div style="text-align:center;padding:12px;display:flex;align-items:center;justify-content:center;gap:8px">${pgHtml}</div>`;
    }
    // Mobile: render cards instead of table
    if (window.__isMobile) {
        renderMobileReqList(data, loadMoreHtml);
        _populateUserFilter();
        _updateToolbarStats();
        return;
    } else {
        el.innerHTML = `<table class="tbl">${thead}<tbody>${rowsHtml}</tbody></table>${loadMoreHtml}`;
    }
    _populateUserFilter();
    _updateToolbarStats();
    _applyColVisCSS();
    // Restore previously open drill-downs (CSS only to avoid content reload flash)
    if (_openDrillIds.length) {
        const stillPresent = _openDrillIds.filter(id => _reqListData.some(r => r.id === id));
        if (stillPresent.length) {
            setTimeout(() => {
                stillPresent.forEach(id => {
                    const drow = document.getElementById('d-' + id);
                    const arrow = document.getElementById('a-' + id);
                    if (drow && !drow.classList.contains('open')) {
                        drow.classList.add('open');
                        if (arrow) arrow.classList.add('open');
                        // Re-bind context panel to the restored drill-down
                        const reqInfo = _reqListData.find(r => r.id === id);
                        const ctxLabel = reqInfo ? (reqInfo.customer_display || reqInfo.name || 'Req #' + id) : 'Req #' + id;
                        bindContextPanel('requisition', id, ctxLabel);
                        // Reload sub-tab content into the panel
                        const defaultTab = _ddActiveTab[id] || _ddDefaultTab(_currentMainView);
                        _ddActiveTab[id] = defaultTab;
                        drow.querySelectorAll('.dd-tab').forEach(t => t.classList.toggle('on', t.dataset.tab === defaultTab));
                        const panel = drow.querySelector('.dd-panel');
                        if (panel) _loadDdSubTab(id, defaultTab, panel);
                    }
                });
                _updateDrillToggleLabel();
            }, 50);
        }
    }
}

// ── Deadline Urgency Helper ───────────────────────────────────────────────

function _isDeadlineUrgent(r, now) {
    if (!r.deadline || r.deadline === 'ASAP') return r.deadline === 'ASAP' ? 'soon' : 'none';
    const d = new Date(r.deadline + 'T12:00:00Z');
    const diff = Math.round((d - now) / 86400000);
    if (diff < 0) return 'overdue';
    if (diff === 0) return 'today';
    if (diff <= 3) return 'soon';
    return 'none';
}

function setToolbarQuickFilter(key) {
    _toolbarQuickFilter = (_toolbarQuickFilter === key) ? '' : key;
    renderReqList();
}

function toggleArchiveGroup(key) {
    // Convert back from string for numeric company_id
    const k = isNaN(key) ? key : Number(key);
    if (_archiveGroupsOpen.has(k)) _archiveGroupsOpen.delete(k);
    else _archiveGroupsOpen.add(k);
    renderReqList();
}

function archiveGoPage(page) {
    _archivePage = page;
    _archiveGroupsOpen.clear();
    loadRequisitions();
    document.getElementById('reqList')?.scrollTo(0, 0);
}

function _updateToolbarStats() {
    const all = _reqListData;
    const now = new Date(); now.setHours(0,0,0,0);

    let nGreen = 0, nYellow = 0;
    for (const r of all) {
        if ((r.offer_count || 0) > 0 || (r.reply_count || 0) > 0) nGreen++;
        const dl = r.deadline;
        if (!dl) continue;
        const isAsap = String(dl).toUpperCase() === 'ASAP';
        if (isAsap) continue;
        const d = new Date(dl); d.setHours(0,0,0,0);
        const diff = Math.round((d - now) / 86400000);
        if (diff <= 3) nYellow++;  // overdue + today + next 3 days
    }

    const qf = _toolbarQuickFilter;
    const html =
        `<span class="tb-stat${qf === 'green' ? ' active' : ''}" onclick="setToolbarQuickFilter('green')" title="Requisitions with vendor offers or replies — click to filter"><span class="tb-dot tb-dot-green"></span><span class="tb-ct">${nGreen}</span> Offers</span>` +
        `<span class="tb-stat${qf === 'yellow' ? ' active' : ''}" onclick="setToolbarQuickFilter('yellow')" title="Requisitions with bid deadline within 3 days — click to filter"><span class="tb-dot tb-dot-amber"></span><span class="tb-ct">${nYellow}</span> Due</span>`;
    const el = document.getElementById('toolbarStats');
    if (el) el.innerHTML = html;
    const mel = document.getElementById('mobileToolbarStats');
    if (mel) mel.innerHTML = html;
}

// ── Batch Selection State ──────────────────────────────────────────────
// Tracks selected requisition IDs for batch archive/assign operations.
// Called by: checkbox onclick in _renderReqRow, batch action bar buttons
// Depends on: apiFetch, showToast, renderReqList
const _batchSelectedReqs = new Set();

function _toggleBatchSelect(reqId, checkbox) {
    if (checkbox.checked) _batchSelectedReqs.add(reqId);
    else _batchSelectedReqs.delete(reqId);
    _updateBatchActionBar();
}

function _toggleBatchSelectAll(selectAllCb) {
    const checkboxes = document.querySelectorAll('.batch-req-cb');
    checkboxes.forEach(cb => {
        const id = parseInt(cb.dataset.reqId);
        cb.checked = selectAllCb.checked;
        if (selectAllCb.checked) _batchSelectedReqs.add(id);
        else _batchSelectedReqs.delete(id);
    });
    _updateBatchActionBar();
}

function _updateBatchActionBar() {
    const count = _batchSelectedReqs.size;
    let bar = document.getElementById('batchActionBar');
    if (count === 0) {
        if (bar) bar.remove();
        return;
    }
    const html = `<div id="batchActionBar" class="rfq-inline-bar" style="position:sticky;bottom:0;z-index:100;background:var(--bg1);border-top:2px solid var(--teal);padding:8px 16px;display:flex;align-items:center;gap:10px;font-size:12px">
        <strong>${count} selected</strong>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" onclick="_clearBatchSelection()">Clear</button>
        <button class="btn btn-sm" style="background:var(--amber-light);color:#92400e" onclick="_batchArchiveSelected()">Archive (${count})</button>
    </div>`;
    if (bar) {
        bar.outerHTML = html;
    } else {
        const reqList = document.getElementById('reqList');
        if (reqList) reqList.insertAdjacentHTML('afterend', html);
    }
}

function _clearBatchSelection() {
    _batchSelectedReqs.clear();
    document.querySelectorAll('.batch-req-cb').forEach(cb => cb.checked = false);
    const selectAll = document.getElementById('batchSelectAll');
    if (selectAll) selectAll.checked = false;
    _updateBatchActionBar();
}

async function _batchArchiveSelected() {
    const ids = [..._batchSelectedReqs];
    if (!ids.length) return;
    try {
        const data = await apiFetch('/api/requisitions/batch-archive', {
            method: 'PUT', body: { ids }
        });
        const cnt = data.archived_count;
        _batchSelectedReqs.clear();
        loadRequisitions();
        showToast(`Archived ${cnt} requisition${cnt !== 1 ? 's' : ''}`, 'success', { duration: 8000, action: { label: 'Undo', fn: async () => {
            try {
                // Unarchive each by toggling archive back
                for (const id of ids) {
                    await apiFetch(`/api/requisitions/${id}/archive`, { method: 'PUT' });
                }
                showToast(`Restored ${ids.length} requisition${ids.length !== 1 ? 's' : ''}`, 'success');
                loadRequisitions();
            } catch (ue) { showToast('Undo failed — ' + friendlyError(ue), 'error'); }
        }}});
    } catch (e) {
        showToast('Batch archive failed: ' + e.message, 'error');
    }
}

function _renderReqRow(r) {
    const total = r.requirement_count || 0;
    const sourced = r.sourced_count || 0;
    const offers = r.offer_count || 0;
    const pct = total > 0 ? Math.round((sourced / total) * 100) : 0;
    const v = _currentMainView;

    // Status badge mapping
    const badgeMap = {draft:'b-draft',active:'b-src',sourcing:'b-src',closed:'b-comp',offers:'b-off',quoted:'b-qtd',quoting:'b-qtd',archived:'b-draft',won:'b-off',lost:'b-draft'};
    const bc = badgeMap[r.status] || 'b-draft';
    const chipMap = {draft:'draft',active:'sourcing',sourcing:'sourcing',offers:'offers',quoted:'quoted',quoting:'quoted',won:'won',lost:'lost',closed:'draft',archived:'draft'};
    const chipCls = chipMap[r.status] || 'draft';

    // Age — days since created
    let age = '';
    if (r.created_at) {
        const days = Math.floor((Date.now() - new Date(r.created_at).getTime()) / 86400000);
        age = days === 0 ? 'Today' : days === 1 ? '1d' : days + 'd';
    }

    // Bid Due — v7 deadline alert system
    let dl = '', dlClass = '';
    if (r.deadline === 'ASAP') {
        dl = '<span class="dl dl-asap">ASAP</span>';
    } else if (r.deadline) {
        const d = new Date(r.deadline + 'T12:00:00Z');
        const now = new Date(); now.setHours(0,0,0,0);
        const diff = Math.round((d - now) / 86400000);
        const fmt = fmtDate(r.deadline);
        if (diff < 0) { dl = `<span class="dl dl-u">\ud83d\udd34 OVERDUE ${fmt}</span>`; dlClass = ' dl-row-overdue'; }
        else if (diff === 0) { dl = `<span class="dl dl-u dl-flash">\ud83d\udd34 DUE TODAY</span>`; dlClass = ' dl-row-today'; }
        else if (diff <= 3) { dl = `<span class="dl dl-w">\u26a0\ufe0f ${fmt}</span>`; dlClass = ' dl-row-warn'; }
        else dl = `<span class="dl dl-ok">\u2713 ${fmt}</span>`;
    } else {
        dl = '<span class="dl dl-set" title="Click to set deadline">+ Set date</span>';
    }

    // Customer display — dedup "Company — Company"
    let cust = r.customer_display || '';
    const dp = cust.split(' \u2014 ');
    if (dp.length === 2 && dp[0].trim() === dp[1].trim()) cust = dp[0].trim();
    if (!cust) cust = r.name || '';

    // Activity indicator dot — new offers, recent replies, or recent updates
    let dot = '';
    if (r.has_new_offers && r.latest_offer_at) {
        const h = (Date.now() - new Date(r.latest_offer_at).getTime()) / 3600000;
        if (h < 12) dot = ' <span class="new-offers-dot" title="New offers received"></span>';
        else if (h < 96) dot = ' <span class="new-offers-dot red" title="New offers (review pending)"></span>';
    } else if (r.latest_reply_at) {
        const rh = (Date.now() - new Date(r.latest_reply_at).getTime()) / 3600000;
        if (rh < 4) dot = ' <span class="new-offers-dot" title="New vendor reply ' + _timeAgo(r.latest_reply_at) + '"></span>';
        else if (rh < 24) dot = ' <span class="new-offers-dot amber" title="Vendor reply ' + _timeAgo(r.latest_reply_at) + '"></span>';
    }

    // Blocker indicator — stalled requisitions get a small warning chip
    let blockerChip = '';
    if (v !== 'archive' && r.status !== 'draft' && r.status !== 'won' && r.status !== 'lost') {
        const _ageDays = r.created_at ? Math.floor((Date.now() - new Date(r.created_at).getTime()) / 86400000) : 0;
        const _oCount = r.offer_count || 0;
        const _sigs = r.sourcing_signals;
        if (_ageDays > 5 && _oCount === 0 && total > 0 && (!_sigs || (_sigs.sources && _sigs.sources.level === 'low' && _sigs.rfqs && _sigs.rfqs.level === 'low'))) {
            blockerChip = ' <span class="blocker-chip" title="Stalled: no offers after 5+ days, low sourcing activity">Stalled</span>';
        } else if (_ageDays > 3 && _oCount === 0 && (r.rfq_sent_count || 0) === 0 && total > 0) {
            blockerChip = ' <span class="blocker-chip warn" title="No RFQs sent after 3+ days">No RFQs</span>';
        }
    }

    // Name cell — editable on active view, read-only on archive
    const statusChip = `<span class="status-chip status-chip-${chipCls}" style="margin-left:6px;font-size:9px;vertical-align:middle">${_statusLabels[r.status] || r.status}</span>`;
    const nameCell = v !== 'archive'
        ? `<td><b class="cust-link dd-edit" onclick="event.stopPropagation();editReqCustomer(${r.id},this)" title="Click to edit customer">${esc(cust)}</b>${dot} <span class="dd-edit" style="font-size:10px;color:var(--muted);cursor:pointer" onclick="event.stopPropagation();editReqName(${r.id},this)" title="Click to edit requisition name">${esc(r.name || '')}</span>${statusChip}${blockerChip}</td>`
        : `<td><b class="cust-link" onclick="event.stopPropagation();toggleDrillDown(${r.id})" title="Click to expand details">${esc(cust)}</b>${dot} <span style="font-size:10px;color:var(--muted)">${esc(r.name || '')}</span></td>`;

    // Last Searched — relative timestamp with absolute tooltip
    let searched = '';
    if (r.last_searched_at) {
        const h = (Date.now() - new Date(r.last_searched_at).getTime()) / 3600000;
        const rel = h < 1 ? '<' + Math.max(1, Math.round(h * 60)) + 'm ago'
            : h < 24 ? Math.round(h) + 'h ago'
            : Math.round(h / 24) + 'd ago';
        const abs = fmtDateTime(r.last_searched_at);
        searched = `<span title="${escAttr(abs)}">${rel}</span>`;
    } else {
        searched = '<span style="color:var(--muted)">\u2014</span>';
    }

    // Per-tab data cells and actions
    let dataCells, actions, colspan;

    if (v === 'archive') {
        // Archive: Parts, Offers, Outcome · $value, Matches, Sales, Age
        const wonVal = r.quote_won_value ? ` <span style="font-size:10px;color:var(--green)">\u00b7 ${fmtDollars(r.quote_won_value)}</span>` : '';
        const pmCnt = r.proactive_match_count || 0;
        const offerCnt = r.offer_count || 0;
        const matchVal = offerCnt > 0 ? offerCnt : pmCnt;
        const matchBadge = matchVal > 0
            ? `<span style="color:var(--green);font-weight:600">${matchVal}</span>`
            : '<span style="color:var(--muted)">\u2014</span>';
        dataCells = `
            <td class="mono">${total}</td>
            <td class="mono">${offers}</td>
            <td style="white-space:nowrap"><span class="status-chip status-chip-${chipCls}">${_statusLabels[r.status] || r.status}</span>${wonVal}</td>
            <td class="mono" style="font-size:11px">${matchBadge}</td>
            <td>${esc(r.created_by_name || '')}</td>
            <td class="mono" style="font-size:11px">${age}</td>`;
        actions = `<td style="white-space:nowrap"><button class="btn btn-sm" onclick="event.stopPropagation();archiveFromList(${r.id})" title="Restore from archive">&#x21a9; Restore</button> <button class="btn btn-sm" onclick="event.stopPropagation();cloneFromList(${r.id})" title="Clone as new draft">&#x1f4cb; Clone</button> <button class="btn btn-sm" onclick="event.stopPropagation();requoteFromList(${r.id})" title="Re-quote this RFQ">&#x1f4dd; Re-quote</button></td>`;
        colspan = 9;
    } else if (v === 'sales') {
        // Sales view: Parts, Quote (with value), Offers, Bid Due, Sales, Age
        let qCell = '<span style="color:var(--muted)" title="No quote created yet">\u2014</span>';
        if (r.quote_status === 'won') qCell = `<span style="color:var(--green);font-weight:600">Won${r.quote_won_value ? ' ' + fmtDollars(r.quote_won_value) : ''}</span>`;
        else if (r.quote_status === 'lost') qCell = '<span style="color:var(--red)">Lost</span>';
        else if (r.quote_status === 'sent') qCell = `<span style="color:var(--blue)">Sent ${fmtRelative(r.quote_sent_at)}</span>`;
        else if (r.quote_status === 'revised') qCell = '<span style="color:var(--amber)">Revised</span>';
        else if (r.quote_status === 'draft') qCell = '<span style="color:var(--muted)">Draft</span>';

        let offCell = '<span style="color:var(--muted)">\u2014</span>';
        const _oCnt = r.offer_count || 0;
        const _rCnt = r.reply_count || 0;
        if (_oCnt > 0) {
            offCell = `<b>${_oCnt}</b>`;
        } else if (_rCnt > 0) {
            offCell = `<span style="color:var(--amber)">${_rCnt} reply</span>`;
        }

        // Coverage: offers vs total parts (can exceed 100% with multiple offers per part)
        const _covOffer = r.offer_count || 0;
        const _covPct = total > 0 ? Math.round((_covOffer / total) * 100) : 0;
        const _covBarPct = Math.min(_covPct, 100);
        const _covLabel = _covPct > 100 ? _covPct + '% (multi)' : _covPct + '%';
        const _covTip = _covOffer + ' offer' + (_covOffer !== 1 ? 's' : '') + ' across ' + total + ' part' + (total !== 1 ? 's' : '') + (_covPct > 100 ? ' \u2014 multiple offers per part' : '');
        let covCell;
        if (total === 0) covCell = '<span style="color:var(--muted)">\u2014</span>';
        else {
            const covColor = _covPct >= 80 ? 'var(--green)' : _covPct >= 40 ? 'var(--amber)' : 'var(--red)';
            covCell = `<div style="display:flex;align-items:center;gap:4px" title="${_covTip}"><div style="flex:1;height:4px;background:var(--bg3,#e2e8f0);border-radius:2px;overflow:hidden;min-width:28px"><div style="height:100%;width:${_covBarPct}%;background:${covColor};border-radius:2px"></div></div><span class="mono" style="font-size:10px">${_covLabel}</span></div>`;
        }

        dataCells = `
            <td class="mono" style="text-align:right">${total}</td>
            <td style="font-size:11px;white-space:nowrap;min-width:70px">${covCell}</td>
            <td style="font-size:11px;white-space:nowrap">${qCell}</td>
            <td style="font-size:11px;white-space:nowrap;text-align:right">${offCell}</td>
            <td class="dl-cell" onclick="event.stopPropagation();editDeadline(${r.id},this)" title="Click to edit deadline">${dl}</td>
            <td class="mono" style="font-size:11px;text-align:right">${age}</td>`;

        // Sales actions: context-aware primary action
        let salesBtn;
        if (r.has_new_offers && (r.offer_count || 0) > 0) {
            salesBtn = `<button class="btn btn-g btn-sm btn-flash" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')" title="Review new offers">Review Offers (${r.offer_count})</button>`;
        } else if (r.quote_status === 'draft') {
            salesBtn = `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'quotes')" title="Finish and send quote">Send Quote</button>`;
        } else if (r.quote_status === 'sent') {
            salesBtn = `<button class="btn btn-g btn-sm" style="font-size:10px;padding:2px 6px" onclick="event.stopPropagation();markReqOutcome(${r.id},'won')" title="Mark as Won">\u2713 Won</button><button class="btn btn-sm" style="font-size:10px;padding:2px 6px;color:var(--red)" onclick="event.stopPropagation();markReqOutcome(${r.id},'lost')" title="Mark as Lost">\u2715 Lost</button>`;
        } else if ((r.offer_count || 0) > 0) {
            salesBtn = `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'quotes')" title="Build a quote from offers">Build Quote</button>`;
        } else if (r.status === 'draft') {
            salesBtn = `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();inlineSourceAll(${r.id})" title="Start sourcing">&#x25b6; Source</button>`;
        } else {
            salesBtn = `<button class="btn btn-y btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'sightings')" title="View sourcing progress">Sourcing</button>`;
        }
        actions = `<td style="white-space:nowrap">${salesBtn} <button class="btn-archive" onclick="event.stopPropagation();archiveFromList(${r.id})" title="Archive">&#x1f4e5;</button></td>`;
        colspan = 9;
    } else {
        // Sourcing view: Parts, Sourced bar, RFQs sent, Response rate, Offers, Age
        const _srcPct = total > 0 ? Math.round((sourced / total) * 100) : 0;
        let srcCell;
        if (total === 0) srcCell = '<span style="color:var(--muted)" title="No parts added yet">\u2014</span>';
        else {
            const barColor = _srcPct >= 80 ? 'var(--green)' : _srcPct >= 40 ? 'var(--amber)' : 'var(--red)';
            srcCell = `<div style="display:flex;align-items:center;gap:4px" title="${sourced} of ${total} parts have supplier sightings"><div style="flex:1;height:4px;background:var(--bg3,#e2e8f0);border-radius:2px;overflow:hidden;min-width:32px"><div style="height:100%;width:${_srcPct}%;background:${barColor};border-radius:2px"></div></div><span class="mono" style="font-size:10px">${sourced}/${total}</span></div>`;
        }

        const sent = r.rfq_sent_count || 0;
        const replied = r.reply_count || 0;
        const respPct = sent > 0 ? Math.round((replied / sent) * 100) : 0;
        const respCell = sent > 0
            ? `<span class="mono" style="font-size:11px">${respPct}% <span style="color:var(--muted)">(${replied}/${sent})</span></span>`
            : '<span style="color:var(--muted)">\u2014</span>';

        let offCell = '<span style="color:var(--muted)">\u2014</span>';
        const _oCnt = r.offer_count || 0;
        if (_oCnt > 0) {
            let qsBadge = '';
            if (r.quote_status === 'won') qsBadge = ' <span class="badge" style="background:#dcfce7;color:#166534;font-size:8px;padding:1px 4px">Won</span>';
            else if (r.quote_status === 'sent') qsBadge = ' <span class="badge" style="background:#dbeafe;color:#1e40af;font-size:8px;padding:1px 4px">Quoted</span>';
            else if (r.quote_status === 'draft') qsBadge = ' <span class="badge" style="background:#f3f4f6;color:#6b7280;font-size:8px;padding:1px 4px">Draft Q</span>';
            else if (r.quote_status === 'lost') qsBadge = ' <span class="badge" style="background:#fee2e2;color:#991b1b;font-size:8px;padding:1px 4px">Lost</span>';
            offCell = `<b>${_oCnt}</b>${qsBadge}`;
        }

        // Coverage: offers vs total parts (can exceed 100% with multiple offers per part)
        const _srcCovPct = total > 0 ? Math.round((_oCnt / total) * 100) : 0;
        const _srcCovBarPct = Math.min(_srcCovPct, 100);
        const _srcCovLabel = _srcCovPct > 100 ? _srcCovPct + '% (multi)' : _srcCovPct + '%';
        const _srcCovTip = _oCnt + ' offer' + (_oCnt !== 1 ? 's' : '') + ' across ' + total + ' part' + (total !== 1 ? 's' : '') + (_srcCovPct > 100 ? ' \u2014 multiple offers per part' : '');
        let srcCovCell;
        if (total === 0) srcCovCell = '<span style="color:var(--muted)">\u2014</span>';
        else {
            const covColor = _srcCovPct >= 80 ? 'var(--green)' : _srcCovPct >= 40 ? 'var(--amber)' : 'var(--red)';
            srcCovCell = `<div style="display:flex;align-items:center;gap:4px" title="${_srcCovTip}"><div style="flex:1;height:4px;background:var(--bg3,#e2e8f0);border-radius:2px;overflow:hidden;min-width:28px"><div style="height:100%;width:${_srcCovBarPct}%;background:${covColor};border-radius:2px"></div></div><span class="mono" style="font-size:10px">${_srcCovLabel}</span></div>`;
        }

        dataCells = `
            <td class="mono">${total}</td>
            <td style="font-size:11px;white-space:nowrap;min-width:80px">${srcCell}</td>
            <td style="font-size:11px;white-space:nowrap;min-width:70px">${srcCovCell}</td>
            <td class="mono" style="font-size:11px">${sent}</td>
            <td style="font-size:11px;white-space:nowrap">${respCell}</td>
            <td style="font-size:11px;white-space:nowrap">${offCell}</td>
            <td>${esc(r.created_by_name || '')}</td>
            <td class="mono" style="font-size:11px">${age}</td>`;

        // Purchasing actions: context-aware primary action
        let srcBtn;
        if (r.status === 'draft') {
            srcBtn = `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();inlineSourceAll(${r.id})" title="Search supplier APIs for parts">&#x25b6; Source All</button>`;
        } else if (_oCnt > 0 && r.has_new_offers) {
            srcBtn = `<button class="btn btn-g btn-sm btn-flash" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')" title="New offers to review">Offers (${_oCnt})</button>`;
        } else if (sourced > 0 && sent === 0) {
            srcBtn = `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'sightings')" title="Sightings found — select vendors and send RFQs">Send RFQs</button>`;
        } else if (sent > 0 && _oCnt === 0) {
            const awaitLabel = replied > 0 ? replied + ' Replies' : 'Awaiting';
            const rfqAge = r.latest_rfq_sent_at ? _timeAgo(r.latest_rfq_sent_at) : '';
            const awaitTitle = rfqAge ? `RFQs sent ${rfqAge}, waiting for responses` : 'RFQs sent, waiting for responses';
            srcBtn = `<button class="btn btn-y btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'activity')" title="${escAttr(awaitTitle)}">${awaitLabel}${rfqAge ? ' <span style="font-size:9px;opacity:.7">(' + rfqAge + ')</span>' : ''}</button>`;
        } else if (_oCnt > 0) {
            srcBtn = `<button class="btn btn-g btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')" title="View confirmed offers">Offers (${_oCnt})</button>`;
        } else {
            srcBtn = `<button class="btn btn-y btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'sightings')" title="View sourcing progress">Sourcing</button>`;
        }
        actions = `<td style="white-space:nowrap">${srcBtn} <button class="btn btn-sm" onclick="event.stopPropagation();ddResearchAll(${r.id})" title="Re-search all suppliers">&#x1f50d;</button> <button class="btn-archive" onclick="event.stopPropagation();archiveFromList(${r.id})" title="Archive">&#x1f4e5;</button></td>`;
        colspan = 10;
    }

    // Build drill-down header: action buttons vary by tab
    let ddHeader;
    if (v === 'archive') {
        ddHeader = `<div style="margin-bottom:2px"><span style="font-size:12px;font-weight:700">${total} part${total !== 1 ? 's' : ''}</span></div>`;
    } else {
        const lastSearch = r.last_searched_at ? _timeAgo(r.last_searched_at) : 'never';
        // Status strip data — sourcing progress, RFQ coverage, offer status
        const _ssParts = total;
        const _ssSourced = sourced;
        const _ssSent = r.rfq_sent_count || 0;
        const _ssReplied = r.reply_count || 0;
        const _ssOffers = r.offer_count || 0;
        const _ssCovPct = _ssParts > 0 ? Math.round((_ssOffers / _ssParts) * 100) : 0;
        const statusItems = [
            { label: 'Parts', value: _ssParts },
            { label: 'Sourced', value: `${_ssSourced}/${_ssParts}`, color: _ssSourced >= _ssParts && _ssParts > 0 ? 'var(--green)' : _ssSourced > 0 ? 'var(--amber)' : 'var(--muted)' },
            { label: 'RFQs', value: _ssSent },
            { label: 'Replies', value: _ssSent > 0 ? `${_ssReplied}/${_ssSent}` : '\u2014', color: _ssReplied > 0 ? 'var(--green)' : 'var(--muted)' },
            { label: 'Coverage', value: _ssCovPct > 100 ? _ssCovPct + '% (multi)' : _ssCovPct + '%', color: _ssCovPct >= 80 ? 'var(--green)' : _ssCovPct >= 40 ? 'var(--amber)' : 'var(--red)', title: _ssOffers + ' offer' + (_ssOffers !== 1 ? 's' : '') + ' / ' + _ssParts + ' part' + (_ssParts !== 1 ? 's' : '') },
        ];
        const ssHtml = renderStatusStrip(statusItems);

        // Blocker strip — deadline, stalled, missing data
        const blockers = [];
        if (r.deadline) {
            const _d = r.deadline === 'ASAP' ? null : new Date(r.deadline + 'T12:00:00Z');
            const _now = new Date(); _now.setHours(0,0,0,0);
            if (r.deadline === 'ASAP') blockers.push({ text: 'ASAP deadline', level: 'warn' });
            else if (_d && _d < _now) blockers.push({ text: 'OVERDUE: ' + fmtDate(r.deadline), level: 'error' });
            else if (_d && (_d - _now) / 86400000 <= 2) blockers.push({ text: 'Due in ' + Math.round((_d - _now) / 86400000) + 'd', level: 'warn' });
        }
        if (_ssParts > 0 && _ssSourced === 0 && _ssSent === 0) blockers.push({ text: 'Not sourced yet', level: 'info' });
        if (_ssSent > 0 && _ssReplied === 0) blockers.push({ text: 'No vendor replies', level: 'warn' });
        const bsHtml = renderBlockerStrip(blockers);

        ddHeader = `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:1px">
            <span style="font-size:11px;font-weight:600;white-space:nowrap">${total} part${total !== 1 ? 's' : ''} <span style="font-weight:400;font-size:10px;color:var(--muted)">\u00b7 ${lastSearch}</span></span>
            ${ssHtml}
            <div style="display:flex;gap:3px;align-items:center;flex-shrink:0">
                <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();ddResearchAll(${r.id})" title="Search all supplier APIs for parts">Source</button>
                <button class="btn btn-sm" onclick="event.stopPropagation();openLogOfferFromList(${r.id})" title="Log a confirmed vendor offer">+ Offer</button>
                <button class="btn btn-sm" onclick="event.stopPropagation();addDrillRow(${r.id})" title="Add part">+ Part</button>
                <button class="btn btn-sm" style="padding:3px 6px" onclick="event.stopPropagation();ddUploadFile(${r.id})" title="Upload CSV/Excel">&#x1f4c1;</button>
                <button class="btn btn-sm" style="padding:3px 6px" onclick="event.stopPropagation();ddPasteRows(${r.id})" title="Paste from spreadsheet">&#x1f4cb;</button>
                <button class="btn btn-primary btn-sm" id="bulkRfqBtn-${r.id}" style="display:none" onclick="event.stopPropagation();ddSendBulkRfq(${r.id})">Prepare RFQ (0)</button>
            </div>
        </div>
        ${bsHtml}`;
    }

    const _urgency = _isDeadlineUrgent(r, new Date());
    const _rowBg = (_urgency === 'overdue' || _urgency === 'today' || _urgency === 'soon')
        ? 'background:#FEF2F2;border-left:3px solid #FECACA' : 'background:#fff';
    const batchChecked = _batchSelectedReqs.has(r.id) ? ' checked' : '';
    return `<tr class="rrow${dlClass}" style="${_rowBg}" onclick="toggleDrillDown(${r.id})">
        <td><input type="checkbox" class="batch-req-cb" data-req-id="${r.id}"${batchChecked} onclick="event.stopPropagation();_toggleBatchSelect(${r.id},this)" style="margin-right:4px;vertical-align:middle"><button class="ea" id="a-${r.id}">\u25b6</button></td>
        ${nameCell}
        ${dataCells}
        ${actions}
    </tr>
    <tr class="drow" id="d-${r.id}"><td colspan="${colspan}">
        ${ddHeader}
        <div class="dd-tabs">${_renderDdTabPills(r.id)}</div>
        <div class="dd-panel"><span style="font-size:11px;color:var(--muted)">${total} part${total !== 1 ? 's' : ''} \u2014 click row or arrow to expand</span></div>
    </td></tr>`;
}

// ── Mobile card renderer for RFQ list ─────────────────────────────────
// ── Mobile Requisition List — card layout with summary stats ───────────
// All user-supplied data is escaped via esc() to prevent XSS.
function renderMobileReqList(data, loadMoreHtml) {
    const el = document.getElementById('reqList');
    if (!el) return;

    // Summary stats: count by bucket
    const allReqs = _reqListData;
    let openCount = 0, sourcingCount = 0, archivedCount = 0;
    for (const r of allReqs) {
        const s = r.status;
        if (s === 'archived' || s === 'won' || s === 'lost' || s === 'closed') archivedCount++;
        else if (s === 'active' || s === 'sourcing') sourcingCount++;
        else openCount++;
    }

    const summaryHtml = '<div class="m-summary m-req-summary">'
        + '<div class="m-summary-stat"><div class="m-summary-num">' + openCount + '</div><div class="m-summary-label">Open</div></div>'
        + '<div class="m-summary-stat"><div class="m-summary-num">' + sourcingCount + '</div><div class="m-summary-label">Sourcing</div></div>'
        + '<div class="m-summary-stat"><div class="m-summary-num">' + archivedCount + '</div><div class="m-summary-label">Archived</div></div>'
        + '</div>';

    // Cards — all user content is esc()-encoded inside _renderReqCardMobile
    const cardsHtml = data.length
        ? data.map(function(r) { return _renderReqCardMobile(r); }).join('')
        : '<div class="m-empty">No requisitions found</div>';

    el.innerHTML = summaryHtml + '<div class="m-req-cards">' + cardsHtml + '</div>' + (loadMoreHtml || '');

    // Sync pill tabs to current view
    _syncMobileReqPills();
}

function _syncMobileReqPills() {
    var pills = document.querySelectorAll('#mobileReqPills .m-tab-pill');
    pills.forEach(function(p) { p.classList.toggle('active', p.dataset.view === _currentMainView); });
}

function mobileReqPillTap(view, btn) {
    setMainView(view);
    var pills = document.querySelectorAll('#mobileReqPills .m-tab-pill');
    pills.forEach(function(p) { p.classList.remove('active'); });
    if (btn) btn.classList.add('active');
}

function _renderReqCardMobile(r) {
    var total = r.requirement_count || 0;

    // Left border color by status
    var borderMap = {active:'m-req-border-blue',sourcing:'m-req-border-blue',offers:'m-req-border-amber',offers_received:'m-req-border-amber',quoted:'m-req-border-green',quoting:'m-req-border-green',draft:'m-req-border-gray',won:'m-req-border-green',lost:'m-req-border-gray',archived:'m-req-border-gray',closed:'m-req-border-gray'};
    var borderCls = borderMap[r.status] || 'm-req-border-gray';

    // Status chip
    var chipMap = {draft:'m-chip',active:'m-chip-blue',sourcing:'m-chip-blue',closed:'m-chip',offers:'m-chip-amber',offers_received:'m-chip-amber',quoted:'m-chip-green',quoting:'m-chip-purple',archived:'m-chip',won:'m-chip-green',lost:'m-chip-red'};
    var chipCls = chipMap[r.status] || 'm-chip';
    var labelMap = {draft:'Draft',active:'Active',sourcing:'Active',closed:'Closed',offers:'Offers',offers_received:'Offers',quoted:'Quoted',quoting:'Quoting',archived:'Archived',won:'Won',lost:'Lost'};
    var statusLabel = labelMap[r.status] || esc(r.status || '');

    // Customer + buyer initials
    var cust = r.customer_display || '';
    var dp = cust.split(' \u2014 ');
    if (dp.length === 2 && dp[0].trim() === dp[1].trim()) cust = dp[0].trim();
    if (!cust) cust = '';
    var buyerName = r.created_by_name || '';
    var initials = '';
    if (buyerName) {
        var parts = buyerName.trim().split(/\s+/);
        initials = parts.map(function(p) { return p[0]; }).join('').toUpperCase().slice(0, 2);
    }
    var subtitle = (cust ? esc(cust) : '') + (cust && initials ? ' &middot; ' : '') + (initials ? '<span class="m-req-initials">' + esc(initials) + '</span>' : '');

    // Date
    var dateStr = '';
    if (r.created_at) {
        var days = Math.floor((Date.now() - new Date(r.created_at).getTime()) / 86400000);
        dateStr = days === 0 ? 'Today' : days === 1 ? '1d' : days + 'd';
    }

    return '<div class="m-card m-req-card ' + borderCls + '" onclick="toggleDrillDown(' + r.id + ')">'
        + '<div class="m-card-title">' + esc(r.name || 'Untitled') + '</div>'
        + '<div class="m-card-subtitle">' + (subtitle || '&mdash;') + '</div>'
        + '<div class="m-req-card-footer">'
        + '<span class="m-card-meta">' + esc(dateStr) + '</span>'
        + '<span class="m-chip ' + chipCls + '">' + statusLabel + '</span>'
        + (total > 0 ? '<span class="m-req-badge">' + total + ' part' + (total !== 1 ? 's' : '') + '</span>' : '')
        + '<span class="m-card-chevron">\u203a</span>'
        + '</div>'
        + '</div>';
}

// ── Inline Deadline Editor ───────────────────────────────────────────────
function editDeadline(reqId, td) {
    if (td.querySelector('input')) return; // Already editing
    const r = _reqListData.find(x => x.id === reqId);
    const cur = r?.deadline || '';
    const isAsap = cur === 'ASAP';
    td.innerHTML = `<div style="display:flex;align-items:center;gap:4px" onclick="event.stopPropagation()">
        <input type="date" value="${isAsap ? '' : cur}" style="font-size:11px;padding:2px 4px;border:1px solid var(--border);border-radius:4px;width:120px"
            onchange="saveDeadline(${reqId},this.value,false)" onkeydown="if(event.key==='Escape'){renderReqList()}">
        <button class="btn btn-sm" style="font-size:10px;padding:1px 5px" onclick="saveDeadline(${reqId},'ASAP',true)"${isAsap ? ' disabled' : ''}>ASAP</button>
        ${cur ? '<button class="btn btn-sm" style="font-size:10px;padding:1px 5px;color:var(--red)" onclick="saveDeadline('+reqId+',null,false)" title="Clear deadline">&times;</button>' : ''}
    </div>`;
    const inp = td.querySelector('input[type=date]');
    if (inp) inp.focus();
}

async function saveDeadline(reqId, value, isAsap) {
    const deadline = isAsap ? 'ASAP' : (value || null);
    try {
        await apiFetch(`/api/requisitions/${reqId}`, { method: 'PUT', body: { deadline } });
        const r = _reqListData.find(x => x.id === reqId);
        if (r) r.deadline = deadline;
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].details;
        renderReqList();
        // Update detail header if viewing this req
        const dlEl = document.getElementById('detailDeadline');
        if (dlEl && currentReqId === reqId) _renderDetailDeadline(dlEl, deadline);
        showToast(deadline ? `Deadline set to ${deadline}` : 'Deadline cleared', 'success');
    } catch (e) { showToast('Failed to update deadline', 'error'); }
}

function _renderDetailDeadline(el, deadline) {
    if (deadline === 'ASAP') {
        el.innerHTML = '<span class="dl dl-asap">ASAP</span>';
    } else if (deadline) {
        const d = new Date(deadline);
        const now = new Date(); now.setHours(0,0,0,0);
        const diff = Math.round((d - now) / 86400000);
        const fmt = fmtDate(deadline);
        if (diff < 0) el.innerHTML = `<span class="dl dl-u">\ud83d\udd34 OVERDUE ${fmt}</span>`;
        else if (diff === 0) el.innerHTML = `<span class="dl dl-u dl-flash">\ud83d\udd34 DUE TODAY</span>`;
        else if (diff <= 3) el.innerHTML = `<span class="dl dl-w">\u26a0\ufe0f Due ${fmt}</span>`;
        else el.innerHTML = `<span class="dl dl-ok">Due ${fmt}</span>`;
    } else {
        el.innerHTML = '<span class="dl dl-set">+ Set deadline</span>';
    }
}

// ── Inline RFQ Name Editor ───────────────────────────────────────────────
function editReqName(reqId, span) {
    if (span.querySelector('input')) return;
    const r = _reqListData.find(x => x.id === reqId);
    const cur = r?.name || '';
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.value = cur;
    inp.placeholder = 'Requirement name';
    inp.style.cssText = 'font-size:10px;padding:2px 4px;border:1px solid var(--border);border-radius:4px;width:120px';
    inp.onclick = e => e.stopPropagation();
    let _done = false;
    const save = async () => {
        if (_done) return;
        _done = true;
        const val = inp.value.trim();
        if (val !== cur) {
            try {
                await apiFetch(`/api/requisitions/${reqId}`, { method: 'PUT', body: { name: val } });
                const rx = _reqListData.find(x => x.id === reqId);
                if (rx) rx.name = val;
                showToast(val ? `Name set to "${val}"` : 'Name cleared', 'success');
            } catch (e) { showToast('Failed to update name', 'error'); }
        }
        renderReqList();
    };
    inp.addEventListener('blur', () => { setTimeout(save, 0); });
    inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); _done = true; renderReqList(); }
    });
    span.textContent = '';
    span.appendChild(inp);
    inp.focus();
    inp.select();
}

// ── Inline Customer Editor (Typeahead) ───────────────────────────────────
function editReqCustomer(reqId, el) {
    if (el.querySelector('input')) return;
    const r = _reqListData.find(x => x.id === reqId);
    const curLabel = r?.customer_site_name || '';

    // Build wrapper
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;display:inline-block';
    wrap.onclick = e => e.stopPropagation();

    const inp = document.createElement('input');
    inp.type = 'text';
    inp.value = curLabel;
    inp.placeholder = 'Customer';
    inp.style.cssText = 'font-size:12px;font-weight:700;padding:2px 4px;border:1px solid var(--border);border-radius:4px;width:180px';

    const list = document.createElement('div');
    list.className = 'site-typeahead-list show';
    list.style.cssText = 'position:absolute;top:100%;left:0;z-index:999;min-width:220px;max-height:200px;overflow-y:auto;background:var(--bg);border:1px solid var(--border);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15)';

    wrap.appendChild(inp);
    wrap.appendChild(list);

    let hlIdx = -1;

    const ensureCache = async () => {
        if (!_siteListCache) await loadSiteOptions();
    };

    const render = (q) => {
        if (!_siteListCache) { list.innerHTML = '<div style="padding:6px 8px;font-size:11px;color:var(--muted)">Loading…</div>'; return; }
        const lq = q.toLowerCase().trim();
        const matches = lq ? _siteListCache.filter(s => s.label.toLowerCase().includes(lq)).slice(0, 6) : _siteListCache.slice(0, 6);
        hlIdx = -1;
        list.innerHTML = matches.map((s, i) =>
            `<div class="site-typeahead-item" data-idx="${i}" data-site-id="${s.id || ''}" data-label="${escAttr(s.label)}" style="padding:4px 8px;font-size:11px;cursor:pointer">${esc(s.label)}</div>`
        ).join('') || '<div style="padding:6px 8px;font-size:11px;color:var(--muted)">No matches</div>';
        list.querySelectorAll('.site-typeahead-item').forEach(item => {
            item.onmousedown = e => { e.preventDefault(); selectItem(item); };
        });
    };

    const selectItem = async (item) => {
        const siteId = item.dataset.siteId ? parseInt(item.dataset.siteId) : null;
        const label = item.dataset.label;
        if (!siteId) return;
        try {
            await apiFetch(`/api/requisitions/${reqId}`, { method: 'PUT', body: { customer_site_id: siteId } });
            const rx = _reqListData.find(x => x.id === reqId);
            if (rx) { rx.customer_site_id = siteId; rx.customer_site_name = label; }
            renderReqList();
            showToast(`Customer set to "${label}"`, 'success');
        } catch (e) { showToast('Failed to update customer', 'error'); }
    };

    const highlight = (idx) => {
        const items = list.querySelectorAll('.site-typeahead-item');
        items.forEach((el, i) => el.style.background = i === idx ? 'var(--bg2,#f0f0f0)' : '');
        hlIdx = idx;
    };

    inp.addEventListener('input', () => render(inp.value));
    inp.addEventListener('blur', () => { setTimeout(() => renderReqList(), 150); });
    inp.addEventListener('keydown', e => {
        const items = list.querySelectorAll('.site-typeahead-item');
        if (e.key === 'ArrowDown') { e.preventDefault(); highlight(Math.min(hlIdx + 1, items.length - 1)); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(Math.max(hlIdx - 1, 0)); }
        else if (e.key === 'Enter') { e.preventDefault(); if (hlIdx >= 0 && items[hlIdx]) selectItem(items[hlIdx]); }
        else if (e.key === 'Escape') { e.preventDefault(); renderReqList(); }
    });

    el.textContent = '';
    el.appendChild(wrap);
    ensureCache().then(() => render(inp.value));
    inp.focus();
    inp.select();
}

// ── v7 My Accounts Toggle ───────────────────────────────────────────────
let _activeFilters = {};
let _toolbarQuickFilter = '';

function toggleMyAccounts(btn) {
    // Legacy — redirect to user filter with current user
    if (_filterUserId) { setUserFilter(''); } else { setUserFilter(String(window.userId)); }
}

function setUserFilter(val) {
    _filterUserId = val ? parseInt(val) : null;
    _myReqsOnly = !!_filterUserId;
    if (_filterUserId) _activeFilters['my_accounts'] = true;
    else delete _activeFilters['my_accounts'];
    // Sync all user filter dropdowns
    document.querySelectorAll('#userFilterSelect, #mobileUserFilterSelect').forEach(sel => {
        if (sel) sel.value = val || '';
    });
    renderReqList();
    // Re-populate dropdowns after render rebuilt the table header
    _populateUserFilter();
}

async function _populateUserFilter() {
    const sels = document.querySelectorAll('#userFilterSelect, #mobileUserFilterSelect');
    if (!sels.length) return;
    let users = window._userFilterList;
    if (!users) {
        try { users = await apiFetch('/api/users/list'); } catch(e) { users = []; }
        window._userFilterList = users;
    }
    const opts = '<option value="">All Users</option>' +
        users.map(u => '<option value="' + u.id + '"' +
            (_filterUserId === u.id ? ' selected' : '') +
            '>' + esc(u.name) + '</option>').join('');
    sels.forEach(sel => { sel.innerHTML = opts; });
}

// ── API Health Tooltip — defined at top of file (line ~41) ──────────────

function clearAllFilters() {
    _activeFilters = {};
    _myReqsOnly = false;
    _toolbarQuickFilter = '';
    const btn = document.getElementById('myAccountsBtn');
    if (btn) btn.classList.remove('on');
    const mobBtn = document.getElementById('mobileMyAccountsBtn');
    if (mobBtn) mobBtn.classList.remove('on');
    renderReqList();
}

function applyDropdownFilters(data) {
    if (!Object.keys(_activeFilters).length && !_toolbarQuickFilter) return data;
    let filtered = data;

    // Needs Review — requisitions with vendor responses needing human review
    if (_activeFilters['has_review']) {
        filtered = filtered.filter(r => r.needs_review_count > 0);
    }
    // High Value — total target value > $10k
    if (_activeFilters['high_value']) {
        filtered = filtered.filter(r => r.total_target_value > 10000);
    }
    // Has Quote — status is quoting or quoted
    if (_activeFilters['has_quote']) {
        filtered = filtered.filter(r => r.status === 'quoting' || r.status === 'quoted');
    }
    // Sales person
    const salesFilters = Object.keys(_activeFilters).filter(k => k.startsWith('sales_'));
    if (salesFilters.length) {
        const names = new Set(salesFilters.map(k => k.replace('sales_', '')));
        filtered = filtered.filter(r => names.has(r.created_by_name));
    }
    // Customer
    const custFilters = Object.keys(_activeFilters).filter(k => k.startsWith('cust_'));
    if (custFilters.length) {
        const custs = new Set(custFilters.map(k => k.replace('cust_', '')));
        filtered = filtered.filter(r => custs.has(r.customer_display));
    }
    // Toolbar pill quick filter
    if (_toolbarQuickFilter) {
        const now = new Date(); now.setHours(0,0,0,0);
        filtered = filtered.filter(r => {
            const dl = r.deadline;
            const isAsap = dl && String(dl).toUpperCase() === 'ASAP';
            const d = (dl && !isAsap) ? new Date(dl) : null;
            if (d) d.setHours(0,0,0,0);
            const diff = d ? Math.round((d - now) / 86400000) : null;
            switch (_toolbarQuickFilter) {
                case 'green': return (r.offer_count || 0) > 0 || (r.reply_count || 0) > 0;
                case 'yellow': return diff !== null && diff <= 3;
                default: return true;
            }
        });
    }
    return filtered;
}

// ── v7 Main View Switcher ───────────────────────────────────────────────
let _archiveAbort = null;   // AbortController for archive fetch
let _followUpsAbort = null; // AbortController for follow-ups fetch
let _pollAbort = null;      // AbortController for auto-poll replies

function _cancelTabInflight() {
    // Cancel in-flight requests from previous tab
    if (_archiveAbort) { try { _archiveAbort.abort(); } catch(e){} _archiveAbort = null; }
    if (_followUpsAbort) { try { _followUpsAbort.abort(); } catch(e){} _followUpsAbort = null; }
    if (_pollAbort) { try { _pollAbort.abort(); } catch(e){} _pollAbort = null; }
    if (_reqAbort) { try { _reqAbort.abort(); } catch(e){} _reqAbort = null; }
    // Cancel in-flight score fetches
    for (const k of Object.keys(_ddScoreAborts)) {
        try { _ddScoreAborts[k].abort(); } catch(e){}
    }
    _ddScoreAborts = {};
    // Cancel pending filter debounce timers
    for (const k of Object.keys(_ddFilterTimers)) {
        clearTimeout(_ddFilterTimers[k]);
        delete _ddFilterTimers[k];
    }
}

function setMainView(view, btn) {
    // Cancel any in-flight requests from previous tab
    _cancelTabInflight();

    // Ensure the requisition list container is visible (deals/archive/sales all render into view-list)
    showView('view-list');

    // Clear stale data from previous tab so we always fetch fresh for the new view
    _reqListData = [];
    _reqFullyLoaded = false;

    _currentMainView = view;
    // Persist view preference (not archive — that's a temporary view)
    if (view !== 'archive') localStorage.setItem('avail_main_view', view);
    // Reset per-RFQ active tab so each view opens its own default sub-tab
    for (const k of Object.keys(_ddActiveTab)) delete _ddActiveTab[k];
    document.querySelectorAll('#mainPills .fp').forEach(b => b.classList.remove('on'));
    document.querySelectorAll('#mobilePills .fp').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    // Sync whichever pill strip the click didn't originate from
    ['mainPills', 'mobilePills'].forEach(id => {
        const cont = document.getElementById(id);
        if (cont) cont.querySelectorAll('.fp').forEach(b => b.classList.toggle('on', b.dataset.view === view));
    });
    _toolbarQuickFilter = '';
    const maBtn = document.getElementById('myAccountsBtn');
    if (maBtn) maBtn.classList.remove('on');
    const maMobBtn = document.getElementById('mobileMyAccountsBtn');
    if (maMobBtn) maMobBtn.classList.remove('on');
    // Follow-ups panel: hide on view switch, will be re-shown by loadFollowUpsPanel
    const fuPanel = document.getElementById('followUpsPanel');
    if (fuPanel) fuPanel.style.display = 'none';
    // Show status filter pills on Pipeline (reqs), hide on archive/deals
    const stEl = document.getElementById('statusToggle');
    if (stEl) stEl.style.display = (view === 'reqs') ? '' : 'none';
    if (view === 'reqs') {
        _reqStatusFilter = 'all';
        _serverSearchActive = false;
        loadRequisitions();
        loadFollowUpsPanel();
    } else if (view === 'deals') {
        _reqStatusFilter = 'all';
        _serverSearchActive = false;
        loadRequisitions().then(() => _renderDealBoard());
    } else if (view === 'active' || view === 'rfq') {
        // Legacy: redirect old view names to Pipeline (reqs)
        _currentMainView = 'reqs';
        _reqStatusFilter = 'all';
        _serverSearchActive = false;
        loadRequisitions();
        loadFollowUpsPanel();
    } else if (view === 'archive') {
        _reqStatusFilter = 'archive';
        _serverSearchActive = false;
        _archivePage = 1;
        loadRequisitions();
    }
}

// ── Toolbar Controls ────────────────────────────────────────────────────

function setStatusFilter(sf, btn) {
    document.querySelectorAll('#statusToggle .fp').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    _reqStatusFilter = sf;
    renderReqList();
}


function toggleAllDrillRows() {
    const openRows = document.querySelectorAll('.drow.open');
    if (openRows.length > 0) {
        // Collapse all drill-downs (and archive groups if in archive mode)
        openRows.forEach(row => row.classList.remove('open'));
        document.querySelectorAll('.ea.open').forEach(a => a.classList.remove('open'));
        if (_currentMainView === 'archive') {
            _archiveGroupsOpen.clear();
            renderReqList();
        }
    } else {
        // In archive mode, first expand all customer groups so rows are visible
        if (_currentMainView === 'archive' && _archiveGroupsOpen.size === 0) {
            for (const r of _reqListData) {
                const key = r.company_id || r.customer_display || 'Unknown';
                _archiveGroupsOpen.add(key);
            }
            renderReqList();
            // Defer drill-down expansion until DOM is updated
            setTimeout(() => {
                document.querySelectorAll('.drow').forEach(row => {
                    const id = parseInt(row.id.replace('d-', ''));
                    if (id) toggleDrillDown(id);
                });
                _updateDrillToggleLabel();
            }, 50);
            return;
        }
        // Expand all drill-down rows
        document.querySelectorAll('.drow').forEach(row => {
            const id = parseInt(row.id.replace('d-', ''));
            if (id) toggleDrillDown(id);
        });
    }
    _updateDrillToggleLabel();
}

function _updateDrillToggleLabel() {
    const el = document.getElementById('ddToggleAll');
    if (!el) return;
    const anyOpen = document.querySelectorAll('.drow.open').length > 0;
    el.textContent = anyOpen ? '\u25bc Collapse' : '\u25b6 Expand';
}

function _collapseAllDrillDowns() {
    document.querySelectorAll('.drow.open').forEach(row => {
        row.classList.remove('open');
        var a = document.getElementById('a-' + row.id.replace('d-',''));
        if (a) a.classList.remove('open');
    });
    _updateDrillToggleLabel();
}


// ── v7 Main Search ──────────────────────────────────────────────────────
const debouncedMainSearch = debounce(function(val) {
    var ds = document.getElementById('mainSearch');
    var ms = document.getElementById('mobileMainSearch');
    // Use the passed value; fall back to whichever input has content
    var q = (typeof val === 'string' ? val : (ds?.value || ms?.value || '')).trim();
    // Keep both inputs in sync
    if (ds) ds.value = q;
    if (ms) ms.value = q;
    if (q.length >= 2) loadRequisitions(q);
    else if (q.length === 0) loadRequisitions();
}, 300);

function triggerMainSearch() {
    var ds = document.getElementById('mainSearch');
    var ms = document.getElementById('mobileMainSearch');
    const q = (ds?.value || ms?.value || '').trim();
    // Keep both inputs in sync
    if (ds) ds.value = q;
    if (ms) ms.value = q;
    if (q.length >= 2) loadRequisitions(q);
    else loadRequisitions();
}

// ── v7 Sidebar Navigation ───────────────────────────────────────────────
function _updateSbScrollArrows() {
    var nav = document.querySelector('.sidebar-nav');
    var up = document.getElementById('sbScrollUp');
    var down = document.getElementById('sbScrollDown');
    if (!nav || !up || !down) return;
    up.classList.toggle('visible', nav.scrollTop > 4);
    down.classList.toggle('visible', nav.scrollTop + nav.clientHeight < nav.scrollHeight - 4);
}
(function() {
    var nav = document.querySelector('.sidebar-nav');
    if (nav) {
        nav.addEventListener('scroll', _updateSbScrollArrows);
        new ResizeObserver(_updateSbScrollArrows).observe(nav);
    }
})();

function toggleSidebar() {
    document.body.classList.toggle('sb-open');
}

function toggleSidebarGroup(headerEl) {
    var group = headerEl.closest('.sb-nav-group');
    if (!group) return;
    var items = group.querySelector('.sb-group-items');
    if (!items) return;
    if (group.classList.contains('collapsed')) {
        group.classList.remove('collapsed');
        items.style.maxHeight = items.scrollHeight + 'px';
        items.style.opacity = '1';
        setTimeout(function() { items.style.maxHeight = 'none'; }, 260);
    } else {
        items.style.maxHeight = items.scrollHeight + 'px';
        items.offsetHeight; // force reflow
        items.style.maxHeight = '0';
        items.style.opacity = '0';
        group.classList.add('collapsed');
    }
}

export function sidebarNav(page, el) {
    safeSet('_lastActivityTs', String(Date.now()));
    document.querySelectorAll('.sb-nav-btn').forEach(i => i.classList.remove('active'));
    if (el) el.classList.add('active');
    var section = el && el.closest('[data-section]');
    if (section) {
        var gradient = document.querySelector('.sb-top-gradient');
        if (gradient) gradient.dataset.section = section.dataset.section;
    }
    // Close sidebar on mobile
    const sb = document.getElementById('sidebar');
    if (sb && sb.classList.contains('mobile-open')) toggleMobileSidebar();
    // Clean up UI state before switching views
    _collapseAllDrillDowns();
    unbindContextPanel();
    const routes = {
        reqs: () => { showList(); setMainPill('active'); },
        customers: () => window.showCustomers(),
        vendors: () => showVendors(),
        strategic: () => showStrategicVendors(),
        materials: () => showMaterials(),
        buyplans: () => window.showBuyPlans(),
        proactive: () => window.showProactiveOffers(),
        settings: () => window.showSettings(),
        prospecting: () => window.showSuggested(),
        suggested: () => window.showSuggested(),
        apihealth: () => window.showSettings('apihealth'),
    };
    try { if (routes[page]) routes[page](); }
    catch(e) { console.error('sidebarNav error:', page, e); }
}

export function navHighlight(btn) {
    document.querySelectorAll('.sb-nav-btn').forEach(i => i.classList.remove('active'));
    if (btn) btn.classList.add('active');
    var section = btn && btn.closest('[data-section]');
    if (section) {
        var gradient = document.querySelector('.sb-top-gradient');
        if (gradient) gradient.dataset.section = section.dataset.section;
    }
    // Only auto-close sidebar on mobile; keep pinned state on desktop
    if (window.innerWidth < 768) document.body.classList.remove('sb-open');
}

function setMainPill(view) {
    document.querySelectorAll('#mainPills .fp').forEach(b => {
        b.classList.toggle('on', b.dataset.view === view);
    });
    document.querySelectorAll('#mobilePills .fp').forEach(b => {
        b.classList.toggle('on', b.dataset.view === view);
    });
}



const searchRequisitions = debounce(query => loadRequisitions(query), 300);

async function sendFollowUp(contactId, vendorName) {
    confirmAction('Send Follow-Up', 'Send follow-up email to ' + vendorName + '?', async function() {
        if (sendFollowUp._busy) return; sendFollowUp._busy = true;
        try {
            const data = await apiFetch(`/api/follow-ups/${contactId}/send`, { method: 'POST', body: {} });
            showToast(data.message || `Follow-up sent to ${vendorName}`, 'success');
            if (typeof loadActivity === 'function') loadActivity();
            loadFollowUpsPanel();
        } catch (e) { showToast('Failed to send follow-up', 'error'); }
        finally { sendFollowUp._busy = false; }
    });
}

// ── Deal Board ─────────────────────────────────────────────────────────
// Renders a kanban-style board grouping requisitions by deal stage.
// Called by: setMainView('deals')
// Depends on: _reqListData, renderObjHeader, renderStatusStrip
function _renderDealBoard() {
    const el = document.getElementById('reqList');
    if (!el) return;
    const data = _reqListData.filter(r => r.status !== 'archived' && r.status !== 'closed');

    // Classify into deal stages
    const stages = [
        { key: 'gathering', label: 'Gathering Intel', icon: '\ud83d\udd0d', color: 'var(--blue)', items: [] },
        { key: 'rfq-out', label: 'RFQs Out', icon: '\ud83d\udce8', color: 'var(--amber)', items: [] },
        { key: 'offers-in', label: 'Offers In', icon: '\ud83d\udce5', color: 'var(--green)', items: [] },
        { key: 'quoting', label: 'Quoting', icon: '\ud83d\udcb0', color: 'var(--purple,#8b5cf6)', items: [] },
        { key: 'closing', label: 'Closing', icon: '\u2705', color: 'var(--green)', items: [] },
    ];

    for (const r of data) {
        const total = r.requirement_count || 0;
        const offers = r.offer_count || 0;
        const sent = r.rfq_sent_count || 0;
        const qs = r.quote_status;
        if (qs === 'sent' || qs === 'revised') stages[4].items.push(r);
        else if (qs === 'draft' || (offers > 0 && !qs)) stages[3].items.push(r);
        else if (offers > 0) stages[2].items.push(r);
        else if (sent > 0) stages[1].items.push(r);
        else stages[0].items.push(r);
    }

    // Render board
    let html = '<div class="deal-board">';
    for (const stage of stages) {
        html += `<div class="deal-col">
            <div class="deal-col-header">
                <span class="deal-col-icon">${stage.icon}</span>
                <span class="deal-col-title">${stage.label}</span>
                <span class="deal-col-count" style="background:${stage.color}">${stage.items.length}</span>
            </div>
            <div class="deal-col-body">`;
        if (stage.items.length === 0) {
            html += '<div class="deal-empty">No deals here</div>';
        }
        for (const r of stage.items) {
            const total = r.requirement_count || 0;
            const offers = r.offer_count || 0;
            const covPct = total > 0 ? Math.round((offers / total) * 100) : 0;
            const covBarPct = Math.min(covPct, 100);
            const covLabel = covPct > 100 ? covPct + '% (multi)' : covPct + '%';
            const covTip = offers + ' offer' + (offers !== 1 ? 's' : '') + ' across ' + total + ' part' + (total !== 1 ? 's' : '') + (covPct > 100 ? ' \u2014 multiple offers per part' : '');
            const covColor = covPct >= 80 ? 'var(--green)' : covPct >= 40 ? 'var(--amber)' : covPct > 0 ? 'var(--red)' : 'var(--muted)';
            const cust = r.customer_display || r.name || 'Unknown';

            // Deadline badge
            let dlBadge = '';
            if (r.deadline === 'ASAP') dlBadge = '<span class="deal-card-dl asap">ASAP</span>';
            else if (r.deadline) {
                const d = new Date(r.deadline + 'T12:00:00Z');
                const now = new Date(); now.setHours(0,0,0,0);
                const diff = Math.round((d - now) / 86400000);
                if (diff < 0) dlBadge = '<span class="deal-card-dl overdue">OVERDUE</span>';
                else if (diff <= 3) dlBadge = '<span class="deal-card-dl warn">' + fmtDate(r.deadline) + '</span>';
                else dlBadge = '<span class="deal-card-dl">' + fmtDate(r.deadline) + '</span>';
            }

            // Value
            let val = '';
            if (r.total_target_value > 0) val = '<span class="deal-card-val">' + fmtDollars(r.total_target_value) + '</span>';
            else if (r.quote_total > 0) val = '<span class="deal-card-val">' + fmtDollars(r.quote_total) + '</span>';

            html += `<div class="deal-card" onclick="_dealCardClick(${r.id})" title="Click to expand">
                <div class="deal-card-head">
                    <span class="deal-card-cust">${esc(cust)}</span>
                    ${dlBadge}
                </div>
                <div class="deal-card-meta">
                    <span>${total} part${total !== 1 ? 's' : ''}</span>
                    <span style="color:${covColor};font-weight:600" title="${covTip}">${covLabel} covered</span>
                    ${val}
                </div>
                <div class="deal-card-bar" title="${covTip}">
                    <div class="deal-card-bar-fill" style="width:${covBarPct}%;background:${covColor}"></div>
                </div>
            </div>`;
        }
        html += '</div></div>';
    }
    html += '</div>';
    el.innerHTML = html;
}

// ── Deal card click handler ──────────────────────────────────────────────
// Deal board has no drill-down rows, so switch to list view first, then expand.
async function _dealCardClick(reqId) {
    if (window.__isMobile) {
        _openMobileDrillDown(reqId);
        return;
    }
    const pipelineBtn = document.querySelector('#mainPills .fp[data-view="reqs"]');
    setMainView('reqs', pipelineBtn);
    const drow = await waitForElement('#d-' + reqId, 3000);
    if (drow) {
        toggleDrillDown(reqId);
    }
}

// ── Follow-Ups Dashboard Panel ───────────────────────────────────────────
async function loadFollowUpsPanel() {
    const panel = document.getElementById('followUpsPanel');
    if (!panel) return;
    if (_currentMainView === 'archive') { panel.style.display = 'none'; return; }
    if (_followUpsAbort) try { _followUpsAbort.abort(); } catch(e){}
    _followUpsAbort = new AbortController();
    try {
        const data = await apiFetch('/api/follow-ups', { signal: _followUpsAbort.signal });
        if (_currentMainView === 'archive') return; // stale — user switched tabs
        const followUps = data.follow_ups || [];
        if (!followUps.length) { panel.style.display = 'none'; return; }
        // Group by requisition
        const groups = {};
        for (const fu of followUps) {
            const key = fu.requisition_id || 0;
            if (!groups[key]) groups[key] = { name: fu.requisition_name || 'Unknown Requirement', items: [] };
            groups[key].items.push(fu);
        }
        let html = `<div class="card" style="margin:0 16px 12px;padding:12px;border-left:3px solid var(--amber)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-weight:700;font-size:13px;color:var(--amber)">Awaiting Vendor Replies (${followUps.length})</span>
                <button class="btn btn-warning btn-sm" id="bulkFollowUpBtn" onclick="sendBulkFollowUp()" style="font-size:10px;display:none">Send Selected</button>
            </div>`;
        for (const [rfqId, g] of Object.entries(groups)) {
            html += `<div style="margin-bottom:6px"><span style="font-weight:600;font-size:12px">${esc(g.name)}</span></div>`;
            for (const fu of g.items) {
                const dayColor = fu.days_waiting > 5 ? 'var(--red)' : fu.days_waiting > 2 ? 'var(--amber)' : 'var(--green)';
                html += `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px">
                    ${fu.contact_id ? `<input type="checkbox" class="fu-cb" data-contact-id="${fu.contact_id}" onchange="_updateBulkFollowUpBtn()">` : ''}
                    <span style="color:var(--text2)">${esc(fu.vendor_name)}</span>
                    <span style="color:var(--muted)">${esc(fu.vendor_email || '')}</span>
                    <span style="color:${dayColor};font-weight:600">${fu.days_waiting}d</span>
                    ${fu.parts && fu.parts.length ? `<span style="color:var(--muted)">${esc(Array.isArray(fu.parts) ? fu.parts.join(', ') : String(fu.parts))}</span>` : ''}
                    ${fu.contact_id ? `<button class="btn btn-ghost btn-sm" onclick="sendFollowUp(${fu.contact_id},'${escAttr(fu.vendor_name)}')" style="padding:1px 6px;font-size:10px">Send Now</button>` : ''}
                </div>`;
            }
        }
        html += '</div>';
        panel.innerHTML = html;
        panel.style.display = '';
    } catch(e) { if (e.name !== 'AbortError') panel.style.display = 'none'; }
}

function _updateBulkFollowUpBtn() {
    const checked = document.querySelectorAll('.fu-cb:checked').length;
    const btn = document.getElementById('bulkFollowUpBtn');
    if (btn) {
        btn.style.display = checked > 0 ? '' : 'none';
        btn.textContent = `Send ${checked} Follow-up${checked > 1 ? 's' : ''}`;
    }
}

async function sendBulkFollowUp() {
    const checked = [...document.querySelectorAll('.fu-cb:checked')];
    const contactIds = checked.map(cb => parseInt(cb.dataset.contactId)).filter(Boolean);
    if (!contactIds.length) return;
    const btn = document.getElementById('bulkFollowUpBtn');
    await guardBtn(btn, 'Sending…', async () => {
        const data = await apiFetch('/api/follow-ups/send-batch', {
            method: 'POST', body: { contact_ids: contactIds }
        });
        showToast(`Sent ${data.sent} of ${data.total} follow-ups`, data.sent > 0 ? 'success' : 'error');
        loadFollowUps();
    });
}

let _createReqPending = false;
async function createRequisition() {
    if (_createReqPending) return; // prevent double-submit
    const name = document.getElementById('nrName')?.value?.trim() || '';
    if (!name) { showToast('Please enter a requisition name', 'error'); return; }
    const siteId = document.getElementById('nrSiteId')?.value || null;
    if (!siteId) { showToast('Please select a customer account', 'error'); return; }
    const isAsap = document.getElementById('nrAsap')?.checked;
    const dlVal = document.getElementById('nrDeadline')?.value || '';
    const deadline = isAsap ? 'ASAP' : (dlVal || null);
    _createReqPending = true;
    try {
        const data = await apiFetch('/api/requisitions', {
            method: 'POST', body: { name, customer_site_id: parseInt(siteId), deadline }
        });
        closeModal('newReqModal');
        const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
        _s('nrName', 'value', ''); _s('nrSiteSearch', 'value', ''); _s('nrSiteId', 'value', '');
        _s('nrDeadline', 'value', ''); _s('nrDeadline', 'disabled', false); _s('nrAsap', 'checked', false);
        const nrSS = document.getElementById('nrSiteSelected'); if (nrSS) { nrSS.classList.add('u-hidden'); nrSS.style.display = ''; }
        const nrCF = document.getElementById('nrContactField'); if (nrCF) { nrCF.classList.add('u-hidden'); nrCF.style.display = ''; }
        const nrCS = document.getElementById('nrContactSelect'); if (nrCS) nrCS.innerHTML = '<option value="">— Select contact —</option>';
        await loadRequisitions();
        expandToSubTab(data.id, 'sightings');
        showToast('Requisition created — add parts below', 'info');
    } catch (e) { showToast('Failed to create requisition', 'error'); }
    _createReqPending = false;
}

function _clearNrValidation() {
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('nrName', 'value', ''); _s('nrSiteSearch', 'value', ''); _s('nrSiteId', 'value', '');
    _s('nrDeadline', 'value', ''); _s('nrDeadline', 'disabled', false); _s('nrAsap', 'checked', false);
    const nrSS = document.getElementById('nrSiteSelected'); if (nrSS) { nrSS.classList.add('u-hidden'); nrSS.style.display = ''; }
    const nrCF = document.getElementById('nrContactField'); if (nrCF) { nrCF.classList.add('u-hidden'); nrCF.style.display = ''; }
    const nrCS = document.getElementById('nrContactSelect'); if (nrCS) nrCS.innerHTML = '<option value="">— Select contact —</option>';
    ['nrNameError', 'nrSiteError'].forEach(id => { const el = document.getElementById(id); if (el) { el.style.display = 'none'; el.textContent = ''; } });
}

function clearNrSite() {
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('nrSiteId', 'value', ''); _s('nrSiteSearch', 'value', '');
    const ss = document.getElementById('nrSiteSearch'); if (ss) ss.style.display = '';
    const sel = document.getElementById('nrSiteSelected'); if (sel) { sel.classList.add('u-hidden'); sel.style.display = ''; }
    const cf = document.getElementById('nrContactField'); if (cf) { cf.classList.add('u-hidden'); cf.style.display = ''; }
}

async function markReqOutcome(id, outcome) {
    confirmAction('Mark Requisition', 'Mark this requisition as ' + outcome.toUpperCase() + '?', async function() {
        try {
            await apiFetch(`/api/requisitions/${id}/outcome`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({outcome})
            });
            showToast(`Requisition marked as ${outcome}`, 'success');
            const q = document.getElementById('mainSearch')?.value?.trim() || '';
            loadRequisitions(q);
        } catch (e) { showToast('Failed to update outcome', 'error'); }
    });
}

async function toggleArchive(id) {
    try {
        await apiFetch(`/api/requisitions/${id}/archive`, { method: 'PUT' });
        if (_reqStatusFilter === 'archive') {
            const resp = await apiFetch('/api/requisitions?status=archive');
            const data = resp.requisitions || resp;
            _reqListData = data;
            data.forEach(r => { if (r.customer_display) _reqCustomerMap[r.id] = r.customer_display; });
            renderReqList();
        } else {
            const q = document.getElementById('mainSearch')?.value?.trim() || '';
            loadRequisitions(q);
        }
    } catch (e) { showToast('Failed to toggle archive', 'error'); }
}

async function archiveFromList(reqId) {
    if (_currentMainView === 'archive') {
        // Restore from archive — no outcome needed
        try {
            const resp = await apiFetch(`/api/requisitions/${reqId}/archive`, { method: 'PUT' });
            const wasRestored = resp.status === 'active';
            _reqListData = _reqListData.filter(r => r.id !== reqId);
            const drow = document.getElementById('d-' + reqId);
            if (drow) drow.remove();
            const arow = document.getElementById('a-' + reqId);
            if (arow) arow.remove();
            const row = document.querySelector(`.req-row[onclick*="toggleDrillDown(${reqId})"]`);
            if (row) row.remove();
            _updateToolbarStats();
            renderReqList();
            const msg = wasRestored ? 'Restored to active' : 'Archived';
            showToast(msg, 'success', { duration: 8000, action: { label: 'Undo', fn: async () => {
                try {
                    await apiFetch(`/api/requisitions/${reqId}/archive`, { method: 'PUT' });
                    showToast('Undone', 'success');
                    loadRequisitions();
                } catch (ue) { showToast('Undo failed — ' + friendlyError(ue), 'error'); }
            }}});
        } catch (e) { showToast('Couldn\'t restore — ' + friendlyError(e, 'please try again'), 'error'); }
        return;
    }
    // Show outcome modal
    const btns = document.getElementById('archiveOutcomeButtons');
    if (!btns) return;
    btns.textContent = '';
    const options = [
        { label: 'Won', outcome: 'won', style: 'background:var(--green);color:#fff' },
        { label: 'Lost', outcome: 'lost', style: 'background:var(--red);color:#fff' },
        { label: 'Just Archive', outcome: '', style: 'background:var(--bg3);color:var(--text)' },
    ];
    for (const opt of options) {
        const b = document.createElement('button');
        b.className = 'btn';
        b.style.cssText = opt.style + ';padding:10px;font-size:13px;font-weight:600;border-radius:6px;width:100%';
        b.textContent = opt.label;
        b.onclick = () => _archiveWithOutcome(reqId, opt.outcome || null);
        btns.appendChild(b);
    }
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.style.cssText = 'font-size:12px;padding:8px;width:100%';
    cancel.textContent = 'Cancel';
    cancel.onclick = () => closeModal('archiveOutcomeModal');
    btns.appendChild(cancel);
    openModal('archiveOutcomeModal');
}

async function _archiveWithOutcome(reqId, outcome) {
    closeModal('archiveOutcomeModal');
    try {
        if (outcome) {
            await apiFetch(`/api/requisitions/${reqId}/outcome`, {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({outcome})
            });
        } else {
            await apiFetch(`/api/requisitions/${reqId}/archive`, { method: 'PUT' });
        }
        _reqListData = _reqListData.filter(r => r.id !== reqId);
        const drow = document.getElementById('d-' + reqId);
        if (drow) drow.remove();
        const arow = document.getElementById('a-' + reqId);
        if (arow) arow.remove();
        const row = document.querySelector(`.req-row[onclick*="toggleDrillDown(${reqId})"]`);
        if (row) row.remove();
        _updateToolbarStats();
        renderReqList();
        const label = outcome ? 'Marked as ' + outcome + ' and archived' : 'Archived';
        showToast(label, 'success', { duration: 8000, action: { label: 'Undo', fn: async () => {
            try {
                await apiFetch(`/api/requisitions/${reqId}/archive`, { method: 'PUT' });
                showToast('Restored', 'success');
                loadRequisitions();
            } catch (ue) { showToast('Undo failed — ' + friendlyError(ue), 'error'); }
        }}});
    } catch (e) { showToast('Couldn\'t archive — ' + friendlyError(e, 'please try again'), 'error'); }
}

async function cloneFromList(reqId) {
    try {
        const resp = await apiFetch(`/api/requisitions/${reqId}/clone`, { method: 'POST' });
        showToast(`Cloned as "${resp.name}"`);
        // Switch to RFQ tab to show the new draft
        const rfqBtn = document.querySelector('#mainPills .fp');
        if (rfqBtn) setMainView('rfq', rfqBtn);
        else loadRequisitions();
    } catch (e) { showToast('Failed to clone', 'error'); }
}

async function requoteFromList(reqId) {
    confirmAction('Re-Quote Requisition', 'Create a new open requisition with the same parts and customer?', async function() {
        try {
            const resp = await apiFetch(`/api/requisitions/${reqId}/clone`, { method: 'POST' });
            const reName = resp.name.replace('(copy)', '(re-quote)');
            if (reName !== resp.name) {
                await apiFetch(`/api/requisitions/${resp.id}`, { method: 'PUT', body: { name: reName } });
            }
            showToast(`Re-quoted as "${reName}" — opening now…`, 'success');
            const srcBtn = document.querySelector('#mainPills .fp:nth-child(2)');
            if (srcBtn) setMainView('purchasing', srcBtn);
            await loadRequisitions();
            const found = _reqListData.find(r => r.id === resp.id);
            if (found) {
                expandToSubTab(resp.id, 'sightings');
            } else {
                showToast(`Created REQ-${resp.id} — "${reName}"`, 'info');
            }
        } catch (e) { showToast('Couldn\'t re-quote — ' + friendlyError(e, 'please try again'), 'error'); }
    });
}

// ── Requirements ────────────────────────────────────────────────────────
let reqData = []; // Cache for editing
let selectedRequirements = new Set(); // Track selected requirements for partial search

async function loadRequirements() {
    if (!currentReqId) return;
    const reqId = currentReqId;
    delete _ddReqCache[reqId];
    try { reqData = await apiFetch(`/api/requisitions/${reqId}/requirements`); }
    catch(e) { logCatchError('loadRequirements', e); showToast('Failed to load requirements', 'error'); return; }
    if (currentReqId !== reqId) return; // RFQ changed while loading
    window._currentRequirements = reqData;  // expose for AI Smart RFQ
    // Auto-select all requirements
    selectedRequirements = new Set(reqData.map(r => r.id));
    const el = document.getElementById('reqTable');
    const filterBar = document.getElementById('reqFilterBar');
    if (!reqData.length) {
        el.innerHTML = '<tr><td colspan="12" class="empty">No parts yet — add one below</td></tr>';
        if (filterBar) filterBar.style.display = 'none';
        return;
    }
    if (filterBar) filterBar.style.display = reqData.length > 3 ? 'flex' : 'none';
    renderRequirementsTable();
    updateSearchAllBar();
}
function renderRequirementsTable() {
    const el = document.getElementById('reqTable');
    const q = (document.getElementById('reqFilter')?.value || '').trim().toUpperCase();
    const pill = reqFilterType;
    const sort = document.getElementById('reqSort')?.value || 'default';

    let filtered = [...reqData];

    // Text filter
    if (q) filtered = filtered.filter(r =>
        (r.primary_mpn || '').toUpperCase().includes(q) ||
        (r.substitutes || []).some(s => s.toUpperCase().includes(q))
    );

    // Pill filter
    if (pill === 'nosrc') filtered = filtered.filter(r => !r.sighting_count);
    if (pill === 'hassubs') filtered = filtered.filter(r => (r.substitutes || []).length > 0);

    // Sort
    if (sort === 'mpn-asc') filtered.sort((a,b) => (a.primary_mpn||'').localeCompare(b.primary_mpn||''));
    else if (sort === 'mpn-desc') filtered.sort((a,b) => (b.primary_mpn||'').localeCompare(a.primary_mpn||''));
    else if (sort === 'qty-desc') filtered.sort((a,b) => (b.target_qty||0) - (a.target_qty||0));
    else if (sort === 'qty-asc') filtered.sort((a,b) => (a.target_qty||0) - (b.target_qty||0));
    else if (sort === 'src-desc') filtered.sort((a,b) => (b.sighting_count||0) - (a.sighting_count||0));
    else if (sort === 'src-asc') filtered.sort((a,b) => (a.sighting_count||0) - (b.sighting_count||0));

    const countEl = document.getElementById('reqFilterCount');
    if (countEl) countEl.textContent = (q || pill !== 'all') ? `${filtered.length} of ${reqData.length}` : `${reqData.length} parts`;
    // AI Normalize button in toolbar
    const normWrap = document.getElementById('aiNormWrap');
    if (normWrap) normWrap.remove();
    if (reqData.length > 0) {
        const filterBar = document.getElementById('reqFilterBar');
        if (filterBar) {
            const wrap = document.createElement('span');
            wrap.id = 'aiNormWrap';
            wrap.innerHTML = '<button class="btn btn-sm" style="font-size:11px;background:var(--bg3);color:var(--teal);border:1px solid var(--teal);margin-left:8px" onclick="aiNormalizeParts(this)">AI Normalize</button>';
            filterBar.appendChild(wrap);
        }
    }
    el.innerHTML = filtered.map(r => {
        const subsText = (r.substitutes || []).length ? r.substitutes.join(', ') : '—';
        const checked = selectedRequirements.has(r.id) ? 'checked' : '';
        return `<tr data-req-id="${r.id}">
            <td style="width:28px;text-align:center"><input type="checkbox" ${checked} onchange="toggleReqSelection(${r.id}, this.checked)" title="Include in search"></td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'primary_mpn')" title="Click to edit">${esc(r.primary_mpn || '—')}</td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'target_qty')" title="Click to edit" style="width:50px">${r.target_qty}</td>
            <td class="req-edit-cell" onclick="editReqCell(this,${r.id},'substitutes')" title="Click to edit" style="font-size:11px;color:var(--text2)">${esc(subsText)}</td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'target_price')" title="Click to edit" style="width:64px;color:${r.target_price ? 'var(--teal)' : 'var(--muted)'}">${r.target_price != null ? '$' + parseFloat(r.target_price).toFixed(2) : '—'}</td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'firmware')" title="Click to edit" style="font-size:11px">${esc(r.firmware || '—')}</td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'date_codes')" title="Click to edit" style="font-size:11px">${esc(r.date_codes || '—')}</td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'hardware_codes')" title="Click to edit" style="font-size:11px">${esc(r.hardware_codes || '—')}</td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'packaging')" title="Click to edit" style="font-size:11px">${esc(r.packaging || '—')}</td>
            <td class="mono req-edit-cell" onclick="editReqCell(this,${r.id},'condition')" title="Click to edit" style="font-size:11px">${esc(r.condition || '—')}</td>
            <td class="mono">${r.sighting_count}</td>
            <td><button class="btn btn-danger btn-sm" onclick="deleteReq(${r.id})" title="Remove">✕</button></td>
        </tr>`;
    }).join('');
    if (Object.keys(searchResults).length) updateRequirementCounts();
}

function toggleReqSelection(reqId, checked) {
    if (checked) selectedRequirements.add(reqId);
    else selectedRequirements.delete(reqId);
    updateSearchAllBar();
}


let reqFilterType = 'all';

function editReqCell(td, reqId, field) {
    if (td.querySelector('input, select')) return; // Already editing
    const r = reqData.find(x => x.id === reqId);
    if (!r) return;

    let currentVal;
    if (field === 'substitutes') currentVal = (r.substitutes || []).join(', ');
    else if (field === 'target_qty') currentVal = String(r.target_qty || 1);
    else if (field === 'target_price') currentVal = r.target_price != null ? String(r.target_price) : '';
    else currentVal = r[field] || '';

    let el;
    if (field === 'condition') {
        el = document.createElement('select');
        el.className = 'req-edit-input';
        el.innerHTML = '<option value="">—</option>' + CONDITION_OPTIONS.map(o => `<option value="${o}"${currentVal === o ? ' selected' : ''}>${o}</option>`).join('');
    } else {
        el = document.createElement('input');
        el.className = 'req-edit-input';
        el.value = currentVal;
        if (field === 'target_qty') { el.type = 'number'; el.min = '1'; el.style.width = '50px'; }
        if (field === 'target_price') { el.type = 'number'; el.step = '0.01'; el.min = '0'; el.style.width = '60px'; el.placeholder = '0.00'; }
    }

    td.textContent = '';
    td.appendChild(el);
    el.focus();
    if (el.select) el.select();

    let _cancelled = false;
    const save = async () => {
        if (_cancelled) return;
        const val = el.value.trim();
        if (val === currentVal) { loadRequirements(); return; }
        const body = {};
        if (field === 'target_price') {
            body[field] = val ? parseFloat(val) : null;
        } else if (field === 'target_qty') {
            body[field] = parseInt(val) || 1;
        } else if (field === 'substitutes') {
            body[field] = val ? val.split(',').map(s => s.trim()).filter(Boolean) : [];
        } else {
            body[field] = val;
        }
        try {
            await apiFetch(`/api/requirements/${reqId}`, { method: 'PUT', body });
        } catch(e) { showToast('Failed to save', 'error'); }
        loadRequirements();
    };

    el.addEventListener('blur', save);
    if (field === 'condition') {
        el.addEventListener('change', () => el.blur());
    }
    el.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
        if (e.key === 'Escape') { _cancelled = true; loadRequirements(); }
    });
}


async function addReq() {
    if (!currentReqId) return;
    const mpnEl = document.getElementById('fMpn');
    const qtyEl = document.getElementById('fQty');
    const subsEl = document.getElementById('fSubs');
    const targetEl = document.getElementById('fTarget');
    const mpn = mpnEl.value.trim();
    if (!mpn) { mpnEl.focus(); return; }
    const targetPrice = targetEl && targetEl.value ? parseFloat(targetEl.value) : null;
    try {
        await apiFetch(`/api/requisitions/${currentReqId}/requirements`, {
            method: 'POST', body: { primary_mpn: mpn, target_qty: qtyEl.value || '1', substitutes: subsEl.value.trim(), target_price: targetPrice }
        });
        mpnEl.value = ''; subsEl.value = ''; qtyEl.value = '1';
        if (targetEl) targetEl.value = '';
        mpnEl.focus();
        loadRequirements();
    } catch(e) { showToast('Failed to add requirement', 'error'); }
}

async function deleteReq(id) {
    confirmAction('Remove Requirement', 'Remove this requirement?', async function() {
        try { await apiFetch(`/api/requirements/${id}`, { method: 'DELETE' }); } catch(e) { showToast('Failed to delete requirement', 'error'); return; }
        // Clear cached search results & selections for this requirement
        delete searchResults[id];
        _rebuildSightingIndex();
        for (const key of [...selectedSightings]) {
            if (key.startsWith(id + ':')) selectedSightings.delete(key);
        }
        if (currentReqId) searchResultsCache[currentReqId] = searchResults;
        loadRequirements();
    }, {confirmClass: 'btn-danger', confirmLabel: 'Remove'});
}


function showFileReady(inputId, readyId, nameId) {
    const inputEl = document.getElementById(inputId);
    const file = inputEl?.files?.[0];
    const readyEl = document.getElementById(readyId);
    const nameEl = document.getElementById(nameId);
    if (file) {
        if (nameEl) nameEl.textContent = file.name;
        if (readyEl) { readyEl.classList.remove('u-hidden'); readyEl.style.display = ''; }
    } else {
        if (readyEl) { readyEl.classList.add('u-hidden'); readyEl.style.display = 'none'; }
    }
}

function clearFileInput(inputId, readyId) {
    const inputEl = document.getElementById(inputId); if (inputEl) inputEl.value = '';
    const readyEl = document.getElementById(readyId); if (readyEl) readyEl.style.display = 'none';
}

async function doUpload() {
    const file = document.getElementById('fileInput')?.files?.[0];
    if (!file || !currentReqId) return;
    const st = document.getElementById('uploadStatus');
    if (st) { st.className = 'ustatus load'; st.textContent = 'Uploading…'; st.style.display = 'block'; }
    const ur = document.getElementById('uploadReady'); if (ur) ur.style.display = 'none';
    const fd = new FormData(); fd.append('file', file);
    try {
        const data = await apiFetch(`/api/requisitions/${currentReqId}/upload`, { method: 'POST', body: fd });
        st.className = 'ustatus ok';
        st.textContent = `Added ${data.created} parts from ${data.total_rows} rows`;
        loadRequirements();
    } catch (e) {
        st.className = 'ustatus err'; st.textContent = 'Upload error: ' + e.message;
    }
    const fi = document.getElementById('fileInput'); if (fi) fi.value = '';
}

// ── Search ──────────────────────────────────────────────────────────────
function updateSearchAllBar() {
    const bar = document.getElementById('searchAllBar');
    if (!bar) return;
    if (!reqData.length) { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    const n = selectedRequirements.size;
    const textEl = document.getElementById('searchAllBarText');
    textEl.textContent = n > 0
        ? `Search ${n} selected part${n !== 1 ? 's' : ''}`
        : 'Select parts to search';
}

async function submitToSourcing(reqId) {
    // Redirect to inline source-all (detail page removed)
    await inlineSourceAll(reqId);
}

async function inlineSourceAll(reqId) {
    // Inline version: searches all parts without navigating to detail page
    // 1. Fetch requirements to get their IDs
    const btn = event ? event.target : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Searching\u2026'; }
    try {
        const reqs = await apiFetch(`/api/requisitions/${reqId}/requirements`);
        _ddReqCache[reqId] = reqs;
        if (!reqs.length) { showToast('No parts to search', 'warn'); return; }
        // 2. Fire search
        const body = { requirement_ids: reqs.map(r => r.id) };
        const results = await apiFetch(`/api/requisitions/${reqId}/search`, { method: 'POST', body });
        // 3. Update caches — convert search results to sightings format for drill-down
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].sightings;
        delete _ddSightingsCache[reqId];
        // 4. Update status in list data
        const reqInfo = _reqListData.find(r => r.id === reqId);
        if (reqInfo) {
            if (reqInfo.status === 'draft') reqInfo.status = 'active';
            reqInfo.last_searched_at = new Date().toISOString();
        }
        // 5. Re-render list to show updated button state
        renderReqList();
        showToast('Search complete — parts are being sourced', 'success');
    } catch(e) {
        showToast('Search error: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '\u25b6 Sourcing'; }
    }
}


async function searchAll() {
    if (!currentReqId) return;
    if (!selectedRequirements.size) { showToast('No parts selected', 'warn'); return; }
    const btn = document.getElementById('searchAllBtn');
    const reqIdAtStart = currentReqId;
    await guardBtn(btn, 'Searching…', async () => {
        try {
            const body = { requirement_ids: [...selectedRequirements] };
            const results = await apiFetch(`/api/requisitions/${reqIdAtStart}/search`, { method: 'POST', body });
            if (currentReqId !== reqIdAtStart) return;  // User navigated away
            window._lastSourceStats = results.source_stats || [];
            delete results.source_stats;
            searchResults = results;
            searchResultsCache[currentReqId] = searchResults;
            _rebuildSightingIndex();
            selectedSightings.clear();
            expandedGroups = new Set(Object.keys(results));
            renderSources();
            updateRequirementCounts();
            switchTab('sources', document.querySelectorAll('#reqTabs .tab')[1]);
            // Update status in cached list (draft→active after submit)
            const reqInfo = _reqListData.find(r => r.id === currentReqId);
            if (reqInfo && reqInfo.status === 'draft') {
                reqInfo.status = 'active';
                notifyStatusChange({status_changed: true, req_status: 'active'});
            }
        } catch (e) {
            showToast('Search error: ' + e.message, 'error');
        }
    });
}

function updateRequirementCounts() {
    const rows = document.querySelectorAll('#reqTable tr');
    for (const reqId of Object.keys(searchResults)) {
        const group = searchResults[reqId];
        // Count unique vendors (fresh + material history, deduplicated)
        const uniqueVendors = new Set(
            (group.sightings || [])
                .filter(s => !s.is_historical)
                .map(s => (s.vendor_name || '').trim().toLowerCase())
                .filter(Boolean)
        );
        const count = uniqueVendors.size;
        // Update the RESULTS column in the matching row
        for (const row of rows) {
            const cells = row.querySelectorAll('td');
            if (cells.length >= 5) {
                const mpn = cells[0].textContent.trim();
                if (mpn === (group.label || '').trim()) {
                    cells[4].textContent = count;
                }
            }
        }
    }
}

// ── Render Search Results ───────────────────────────────────────────────
let srcFilterType = 'all';
let _srcSort = 'default';



function renderSources() {
    const el = document.getElementById('sourceResults');
    if (!el) return; // Not on Sourcing view (e.g. RFQ drill-down Sightings tab) — avoid setting innerHTML on null
    const keys = Object.keys(searchResults);
    if (!keys.length) {
        el.innerHTML = stateEmpty('No results found', 'Try a different part number or check spelling');
        const countEl = document.getElementById('srcFilterCount');
        if (countEl) countEl.textContent = '';
        document.getElementById('collapsedMatchHint')?.classList.add('hidden');
        return;
    }

    // Build target price lookup: MPN (uppercase) → target_price
    const targetPriceMap = {};
    for (const r of reqData) {
        if (r.target_price != null && r.primary_mpn) {
            targetPriceMap[r.primary_mpn.trim().toUpperCase()] = parseFloat(r.target_price);
        }
    }

    const q = (document.getElementById('srcFilter')?.value || '').trim().toUpperCase();
    let totalShown = 0;
    let totalAll = 0;
    let collapsedMatchCount = 0;
    let collapsedMatchGroups = new Set();
    const isFiltering = !!(q || srcFilterType !== 'all');

    let html = '';

    // Connector status banner
    const _ss = window._lastSourceStats || [];
    if (_ss.length) {
        const okList = _ss.filter(s => s.status === 'ok');
        const errList = _ss.filter(s => s.status === 'error');
        const skipList = _ss.filter(s => s.status === 'skipped');
        const disabledList = _ss.filter(s => s.status === 'disabled');
        const summaryParts = [];
        if (okList.length) summaryParts.push(`<span style="color:var(--green)">${okList.length} ok</span>`);
        if (errList.length) summaryParts.push(`<span style="color:var(--red)">${errList.length} failed</span>`);
        if (skipList.length) summaryParts.push(`<span style="color:var(--muted)">${skipList.length} no key</span>`);
        if (disabledList.length) summaryParts.push(`<span style="color:var(--muted)">${disabledList.length} off</span>`);
        let detailHtml = '';
        for (const s of okList) {
            const ms = s.ms >= 1000 ? (s.ms / 1000).toFixed(1) + 's' : s.ms + 'ms';
            detailHtml += `<span class="badge" style="background:#dcfce7;color:#166534">${esc(s.source)} ${s.results} (${ms})</span> `;
        }
        for (const s of errList) {
            const errMsg = s.error ? ': ' + esc(s.error.length > 50 ? s.error.slice(0, 50) + '\u2026' : s.error) : '';
            detailHtml += `<span class="badge" style="background:#fee2e2;color:#991b1b">${esc(s.source)}${errMsg}</span> `;
        }
        for (const s of skipList) {
            detailHtml += `<span class="badge" style="background:var(--bg2);color:var(--muted)">${esc(s.source)} (no key)</span> `;
        }
        for (const s of disabledList) {
            detailHtml += `<span class="badge" style="background:var(--bg2);color:var(--muted)">${esc(s.source)} (off)</span> `;
        }
        html += `<div class="source-status-banner" style="margin-bottom:10px;padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:11px;background:var(--bg1)">
            <div style="display:flex;align-items:center;gap:6px;cursor:pointer" onclick="this.parentElement.classList.toggle('ss-expanded')">
                <span style="font-weight:600">Sources:</span> ${summaryParts.join(' · ')}
                <span style="margin-left:auto;color:var(--muted);font-size:10px" class="ss-toggle-hint">details</span>
            </div>
            <div class="ss-detail" style="display:none;margin-top:6px;flex-wrap:wrap;gap:4px">${detailHtml}</div>
        </div>`;
    }

    for (const reqId of keys) {
        const group = searchResults[reqId];
        const sightings = group.sightings || [];
        const isExpanded = expandedGroups.has(reqId);
        const chevron = isExpanded ? '▼' : '▶';

        // Count unique vendors
        const uniqueVendors = new Set(
            sightings
                .filter(s => !s.is_historical)
                .map(s => (s.vendor_name || '').trim().toLowerCase())
                .filter(v => v && v !== 'no seller listed')
        );
        const vendorCount = uniqueVendors.size;
        const freshCount = sightings.filter(s => !s.is_historical && !s.is_material_history).length;
        const matHistCount = sightings.filter(s => s.is_material_history).length;
        const histCount = sightings.filter(s => s.is_historical).length;
        let countLabel = `${vendorCount} vendors`;
        if (matHistCount > 0) countLabel += ` (${freshCount} current + ${matHistCount} from history)`;
        if (histCount > 0) countLabel += ` + ${histCount} past searches`;

        // Count how many sightings in this group pass filters (for collapsed match tracking)
        let groupMatchCount = 0;
        for (let i = 0; i < sightings.length; i++) {
            const s = sightings[i];
            const vName = (s.vendor_name || '').trim();
            if (!vName || vName.toLowerCase() === 'no seller listed') continue;
            const searchText = ((s.vendor_name||'') + ' ' + (s.mpn_matched||'') + ' ' + (s.manufacturer||'') + ' ' + (s.source_type||'')).toUpperCase();
            if (q && !searchText.includes(q)) continue;
            const isSub_ = s.mpn_matched && group.label && s.mpn_matched.trim().toUpperCase() !== group.label.trim().toUpperCase();
            if (srcFilterType === 'exact' && isSub_) continue;
            if (srcFilterType === 'sub' && !isSub_) continue;
            if (srcFilterType === 'available' && s.is_unavailable) continue;
            if (srcFilterType === 'sold' && !s.is_unavailable) continue;
            groupMatchCount++;
        }

        // Selected count for this group (show in header when collapsed)
        let groupSelectedCount = 0;
        for (let i = 0; i < sightings.length; i++) {
            if (selectedSightings.has(`${reqId}:${i}`)) groupSelectedCount++;
        }

        const selectedBadge = groupSelectedCount > 0
            ? `<span class="badge b-selected">${groupSelectedCount} selected</span>`
            : '';
        const matchCountBadge = !isExpanded && isFiltering && groupMatchCount > 0
            ? `<span class="badge b-matchcount">${groupMatchCount} match${groupMatchCount !== 1 ? 'es' : ''}</span>`
            : '';

        html += `<div class="sight-group ${isExpanded ? 'sg-expanded' : 'sg-collapsed'}">
            <div class="sight-group-title" onclick="toggleGroup('${escAttr(reqId)}')" title="Click to ${isExpanded ? 'collapse' : 'expand'}">
                <div class="sg-title-left">
                    <span class="sg-chevron">${chevron}</span>
                    <span class="sg-mpn" onclick="event.stopPropagation();openMaterialPopupByMpn('${escAttr(group.label)}')" title="View material card">${esc(group.label)}</span>
                    ${selectedBadge}
                    ${matchCountBadge}
                </div>
                <span class="mono">${countLabel}</span>
            </div>`;

        // Sighting rows — always rendered, visibility controlled by CSS
        html += `<div class="sg-body" ${isExpanded ? '' : 'style="display:none"'}>`;

        if (!sightings.length) {
            html += '<p class="empty" style="padding:12px 0">No vendors found for this part</p>';
        }

        // Sort sightings for display (preserve original indices for checkbox keys)
        const sortedIndices = sightings.map((_, idx) => idx);
        if (_srcSort !== 'default') {
            sortedIndices.sort((a, b) => {
                const sa = sightings[a], sb = sightings[b];
                switch (_srcSort) {
                    case 'price-asc': return (sa.unit_price ?? Infinity) - (sb.unit_price ?? Infinity);
                    case 'price-desc': return (sb.unit_price ?? -1) - (sa.unit_price ?? -1);
                    case 'qty-desc': return (sb.qty_available ?? 0) - (sa.qty_available ?? 0);
                    case 'qty-asc': return (sa.qty_available ?? 0) - (sb.qty_available ?? 0);
                    case 'vendor-az': return (sa.vendor_name || '').localeCompare(sb.vendor_name || '');
                    case 'engagement': {
                        const ea = sa.vendor_card?.vendor_score ?? -1;
                        const eb = sb.vendor_card?.vendor_score ?? -1;
                        return eb - ea;
                    }
                    default: return 0;
                }
            });
        }
        for (const i of sortedIndices) {
            const s = sightings[i];

            const vName = (s.vendor_name || '').trim();
            if (!vName || vName.toLowerCase() === 'no seller listed') continue;

            totalAll++;

            // Text filter
            const searchText = ((s.vendor_name||'') + ' ' + (s.mpn_matched||'') + ' ' + (s.manufacturer||'') + ' ' + (s.source_type||'')).toUpperCase();
            if (q && !searchText.includes(q)) continue;

            // Pill filter
            const isSub_ = s.mpn_matched && group.label && s.mpn_matched.trim().toUpperCase() !== group.label.trim().toUpperCase();
            if (srcFilterType === 'exact' && isSub_) continue;
            if (srcFilterType === 'sub' && !isSub_) continue;
            if (srcFilterType === 'available' && s.is_unavailable) continue;
            if (srcFilterType === 'sold' && !s.is_unavailable) continue;

            totalShown++;

            // Track matches in collapsed groups
            if (!isExpanded && isFiltering) {
                collapsedMatchCount++;
                collapsedMatchGroups.add(reqId);
            }

            const key = `${reqId}:${i}`;
            const checked = selectedSightings.has(key) ? 'checked' : '';
            const srcLabel = (s.source_type || '').toUpperCase();
            const cond = (s.condition || '').toUpperCase().trim();
            const condBadge = cond ? `<span class="badge b-cond-${cond === 'NEW' ? 'new' : cond === 'USED' ? 'used' : 'ref'}">${esc(cond)}</span>` : '';

            // Lead Opportunity Score
            const los = s.score != null ? Math.round(s.score) : null;
            const losHtml = los != null ? `<span class="sc-los ${los >= 70 ? 'sc-los-high' : los >= 40 ? 'sc-los-mid' : 'sc-los-low'}" title="Lead Opportunity Score: ${los}/100&#10;Based on recency, quantity, source, completeness, vendor reliability, and price">LOS ${los}</span>` : '';

            // Extended field badges
            const moqBadge = s.moq ? `<span class="badge b-moq" title="Minimum order quantity">MOQ ${s.moq.toLocaleString()}</span>` : '';
            const dcBadge = s.date_code ? `<span class="badge b-datecode" title="Date code">DC ${esc(s.date_code)}</span>` : '';
            const pkgBadge = s.packaging ? `<span class="badge b-packaging" title="Packaging">${esc(s.packaging.toUpperCase())}</span>` : '';
            const ltBadge = s.lead_time_days != null ? `<span class="badge b-leadtime" title="Lead time">${s.lead_time_days === 0 ? 'In Stock' : s.lead_time_days + 'd'}</span>` : (s.lead_time ? `<span class="badge b-leadtime" title="Lead time">${esc(s.lead_time)}</span>` : '');
            const histBadge = s.is_historical ? `<span class="badge b-hist" title="Previously seen ${s.historical_date || ''}">📋 ${s.historical_date || 'Past'}</span>` : '';
            const matHistBadge = s.is_material_history ? `<span class="badge b-mathistory" title="Seen ${s.material_times_seen || 1}× · Last: ${s.material_last_seen || '?'} · First: ${s.material_first_seen || '?'}">🧩 ${s.material_times_seen || 1}× · Last ${s.material_last_seen || '?'}</span>` : '';

            const isSub = s.mpn_matched && group.label && s.mpn_matched.trim().toUpperCase() !== group.label.trim().toUpperCase();
            const matchBadge = isSub
                ? '<span class="badge b-sub">SUB</span>'
                : '<span class="badge b-exact">EXACT</span>';

            const unavail = s.is_unavailable;
            const unavailClass = unavail ? 'sc-unavailable' : '';
            const unavailBadge = unavail ? '<span class="badge b-unavail">NOT AVAIL</span>' : '';
            const unavailBtn = s.id
                ? `<button class="btn-unavail" onclick="event.stopPropagation();markUnavailable(${s.id},${!unavail})" title="${unavail ? 'Mark available' : 'Mark as not available'}">${unavail ? '↩ Restore' : '✕ N/A'}</button>`
                : '';

            const vn = escAttr(s.vendor_name);
            const mpn = escAttr(s.mpn_matched || '');
            const ph = escAttr(s.vendor_phone || '');

            const vc = s.vendor_card || {};
            let ratingHtml = '';
            if (vc.card_id) {
                let scoreRing = '';
                if (s.is_authorized) {
                    scoreRing = `<span style="display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;border:2px solid var(--green);background:var(--green-light);font-size:8px;font-weight:700;color:var(--green);margin-right:3px;cursor:default" title="Authorized Distributor">\u2713</span>`;
                } else if (vc.is_new_vendor || vc.vendor_score == null) {
                    scoreRing = `<span style="display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;border:2px solid var(--muted);background:var(--card2);font-size:7px;font-weight:700;color:var(--muted);margin-right:3px;cursor:default" title="New Vendor — no order history">NEW</span>`;
                } else {
                    const vs = Math.round(vc.vendor_score);
                    const vsColor = vs >= 66 ? 'var(--green)' : vs >= 33 ? 'var(--amber)' : 'var(--red)';
                    const vsBg = vs >= 66 ? 'var(--green-light)' : vs >= 33 ? 'var(--amber-light)' : 'var(--red-light)';
                    scoreRing = `<span style="display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;border:2px solid ${vsColor};background:${vsBg};font-size:8px;font-weight:700;color:${vsColor};margin-right:3px;cursor:default" title="Vendor Score: ${vs}/100">${vs}</span>`;
                }
                const starStr = vc.avg_rating != null ? `<span class="stars" style="font-size:11px">\u2605</span><span class="stars-num" style="font-size:10px">${vc.avg_rating}</span><span class="stars-count" style="font-size:9px;color:var(--muted)">(${vc.review_count})</span>` : '';
                const cardPill = `<span class="badge" style="background:var(--bg2);cursor:pointer;font-size:9px;padding:1px 6px;margin-left:3px" onclick="event.stopPropagation();openVendorPopup(${vc.card_id})" title="Open vendor card">View</span>`;
                ratingHtml = `<span class="sc-rating">${scoreRing}${starStr}${cardPill}</span>`;
            } else {
                ratingHtml = '<span class="sc-rating sc-rating-new" title="New vendor">\u2606</span>';
            }

            // Build listing link: use click_url if available, else construct from source + MPN
            const _srcListingUrl = (() => {
                if (s.click_url) return s.click_url;
                if (s.octopart_url) return s.octopart_url;
                const pn = encodeURIComponent(s.mpn_matched || '');
                if (!pn) return '';
                const st = (s.source_type || '').toLowerCase();
                if (st === 'digikey') return `https://www.digikey.com/en/products/result?keywords=${pn}`;
                if (st === 'mouser') return `https://www.mouser.com/Search/Refine?Keyword=${pn}`;
                if (st === 'nexar' || st === 'octopart') return `https://octopart.com/search?q=${pn}`;
                if (st === 'oemsecrets') return `https://www.oemsecrets.com/compare/${pn}`;
                if (st === 'element14') return `https://www.newark.com/search?st=${pn}`;
                if (st === 'brokerbin') return `https://www.brokerbin.com/search?q=${pn}`;
                if (st === 'sourcengine') return `https://www.sourcengine.com/search/${pn}`;
                if (st === 'netcomponents') return `https://www.netcomponents.com/partsearch/${pn}`;
                if (st === 'ebay') return `https://www.ebay.com/sch/i.html?_nkw=${pn}`;
                return `https://octopart.com/search?q=${pn}`;
            })();
            const listingLink = _srcListingUrl ? `<a href="${escAttr(_srcListingUrl)}" target="_blank" class="btn-link" title="Search on ${esc(s.source_type || 'web')}">🔗 Listing</a>` : '';
            const vendorLink = s.vendor_url ? `<a href="${escAttr(s.vendor_url)}" target="_blank" class="btn-link">🏢 Site</a>` : '';
            const phoneLinkHtml = s.vendor_phone ? `<a class="btn-call phone-link" href="tel:${ph}" onclick="logCallInitiated(this)" data-phone="${ph}" data-ctx="${escAttr(JSON.stringify({vendor_card_id: vc.card_id || null, requirement_id: s.requirement_id || null, origin: 'search_results'}))}">📞 ${esc(s.vendor_phone)}</a>` : '';
            const emailIndicator = vc.has_emails ? `<span class="badge b-email" title="${vc.email_count} email(s) on file">✉ ${vc.email_count}</span>` : '';

            // Build price HTML
            const priceHtml = (() => {
                if (s.unit_price == null) return '<span class="sc-key-val" style="color:var(--muted)">—</span>';
                const tp = targetPriceMap[(group.label || '').trim().toUpperCase()];
                const priceStr = '$' + s.unit_price.toFixed(2);
                if (tp == null) return `<span class="sc-key-val">${priceStr}</span>`;
                const pct = ((s.unit_price - tp) / tp * 100).toFixed(0);
                if (s.unit_price <= tp) return `<span class="sc-key-val" style="color:var(--green)">${priceStr}</span><span class="badge" style="background:#dcfce7;color:#166534;font-size:8px;padding:1px 4px">${pct > 0 ? '+' : ''}${pct}%</span>`;
                if (s.unit_price <= tp * 1.15) return `<span class="sc-key-val" style="color:var(--amber)">${priceStr}</span><span class="badge" style="background:#fef3c7;color:#92400e;font-size:8px;padding:1px 4px">+${pct}%</span>`;
                return `<span class="sc-key-val" style="color:var(--red)">${priceStr}</span><span class="badge" style="background:#fee2e2;color:#991b1b;font-size:8px;padding:1px 4px">+${pct}%</span>`;
            })();
            const qtyHtml = s.qty_available != null
                ? `<span class="sc-key-val">${s.qty_available.toLocaleString()}</span>`
                : '<span class="sc-key-val" style="color:var(--muted)">—</span>';

            // Row 2 badges: collect all, show max 5 + overflow
            const excessBadge = (s.source_type || '').toLowerCase() === 'excess_list' ? '<span class="badge" style="background:#fef3c7;color:#92400e" title="Excess list from customer">EXCESS</span>' : '';
            const allBadges = [matchBadge, unavailBadge, excessBadge, s.is_authorized ? '<span class="badge b-auth">Auth</span>' : '', `<span class="badge b-src">${srcLabel}</span>`, condBadge, moqBadge, dcBadge, pkgBadge, ltBadge, emailIndicator, histBadge, matHistBadge].filter(b => b);
            const visibleBadges = allBadges.slice(0, 5).join('');
            const overflowBadge = allBadges.length > 5 ? `<span class="sc-more-badge" title="${allBadges.slice(5).map(b => b.replace(/<[^>]+>/g, '')).join(' · ')}">+${allBadges.length - 5}</span>` : '';

            html += `<div class="card sc ${s.is_historical ? 'sc-hist' : ''} ${s.is_material_history ? 'sc-mathistory' : ''} ${isSub ? 'sc-sub' : ''} ${unavailClass}">
                ${isBuyer() ? `<input type="checkbox" ${checked} onchange="toggleSighting('${key}')">` : ''}
                <div class="sc-body">
                    <div class="sc-top">
                        ${vc.card_id ? `<span class="sc-vendor cust-link" title="${escAttr(s.vendor_name)}" onclick="event.stopPropagation();openVendorPopup(${vc.card_id})">${esc(s.vendor_name)}</span>` : `<span class="sc-vendor" title="${escAttr(s.vendor_name)}">${esc(s.vendor_name)}</span>`}
                        ${ratingHtml}
                        ${losHtml}
                        <div class="sc-key-vals">
                            <span class="sc-detail-label">QTY</span>${qtyHtml}
                            <span class="sc-detail-label">PRICE</span>${priceHtml}
                        </div>
                    </div>
                    <div class="sc-badges">${visibleBadges}${overflowBadge}${s.mpn_matched && s.mpn_matched.trim().toUpperCase() !== (group.label || '').trim().toUpperCase() ? ` <span style="font-size:10px;color:var(--muted)">${esc(s.mpn_matched)}</span>` : ''}${s.manufacturer ? ` <span style="font-size:10px;color:var(--muted)">${esc(s.manufacturer)}</span>` : ''}</div>
                </div>
                <div class="sc-actions-right">
                    ${phoneLinkHtml}${listingLink}${vendorLink}${unavailBtn}
                </div>
            </div>`;
        }
        html += '</div></div>';
    }
    el.innerHTML = html;
    const countEl = document.getElementById('srcFilterCount');
    if (countEl) countEl.textContent = (q || srcFilterType !== 'all') ? `${totalShown} of ${totalAll}` : `${totalAll} results`;

    // Collapsed-match hint
    const hintEl = document.getElementById('collapsedMatchHint');
    if (hintEl) {
        if (isFiltering && collapsedMatchCount > 0) {
            hintEl.innerHTML = `${collapsedMatchCount} match${collapsedMatchCount !== 1 ? 'es' : ''} in ${collapsedMatchGroups.size} collapsed group${collapsedMatchGroups.size !== 1 ? 's' : ''} · <a href="#" onclick="event.preventDefault();expandMatchingGroups()">Expand matching</a>`;
            hintEl.classList.remove('hidden');
        } else {
            hintEl.classList.add('hidden');
        }
    }

    updateBatchCount();
}

// ── Group Collapse / Expand ─────────────────────────────────────────────
function toggleGroup(reqId) {
    if (expandedGroups.has(reqId)) expandedGroups.delete(reqId);
    else expandedGroups.add(reqId);
    renderSources();
}



function expandMatchingGroups() {
    const q = (document.getElementById('srcFilter')?.value || '').trim().toUpperCase();
    const isFiltering = !!(q || srcFilterType !== 'all');
    if (!isFiltering) return;

    for (const reqId of Object.keys(searchResults)) {
        const group = searchResults[reqId];
        const sightings = group.sightings || [];
        for (const s of sightings) {
            const vName = (s.vendor_name || '').trim();
            if (!vName || vName.toLowerCase() === 'no seller listed') continue;
            const searchText = ((s.vendor_name||'') + ' ' + (s.mpn_matched||'') + ' ' + (s.manufacturer||'') + ' ' + (s.source_type||'')).toUpperCase();
            if (q && !searchText.includes(q)) continue;
            const isSub_ = s.mpn_matched && group.label && s.mpn_matched.trim().toUpperCase() !== group.label.trim().toUpperCase();
            if (srcFilterType === 'exact' && isSub_) continue;
            if (srcFilterType === 'sub' && !isSub_) continue;
            if (srcFilterType === 'available' && s.is_unavailable) continue;
            if (srcFilterType === 'sold' && !s.is_unavailable) continue;
            // This group has a match — expand it
            expandedGroups.add(reqId);
            break;
        }
    }
    renderSources();
}

// ── Selection & Batch RFQ ───────────────────────────────────────────────
function toggleSighting(key) {
    if (selectedSightings.has(key)) selectedSightings.delete(key);
    else selectedSightings.add(key);
    updateBatchCount();
}



async function markUnavailable(sightingId, unavail, reqId) {
    try {
        await apiFetch(`/api/sightings/${sightingId}/unavailable`, {
            method: 'PUT', body: { unavailable: unavail }
        });
        // Update local state — old sourcing panel index
        const ref = _sightingIndex[sightingId];
        if (ref) {
            ref.sighting.is_unavailable = unavail;
        }
        // Update drill-down sightings cache
        if (reqId && _ddSightingsCache[reqId]) {
            for (const group of Object.values(_ddSightingsCache[reqId])) {
                const s = (group.sightings || []).find(s => s.id === sightingId);
                if (s) { s.is_unavailable = unavail; break; }
            }
            _renderSourcingDrillDown(reqId);
        }
        renderSources();
    } catch(e) { showToast('Error: ' + e.message, 'error'); }
}

function updateBatchCount() {
    const btn = document.getElementById('batchRfqBtn');
    const groups = getSelectedByVendor();
    const count = groups.length;
    btn.textContent = `Send Batch RFQ (${count} vendor${count !== 1 ? 's' : ''})`;
    btn.disabled = count === 0;
}

function getSelectedByVendor() {
    const groups = {};
    for (const key of selectedSightings) {
        const [reqId, idx] = key.split(':');
        const group = searchResults[reqId];
        if (!group) continue;
        const s = group.sightings[parseInt(idx)];
        if (!s) continue;
        if (s.is_historical) continue; // Skip old search results
        const vKey = (s.vendor_name || '').trim().toLowerCase();
        if (!vKey || vKey === 'no seller listed') continue;
        if (!groups[vKey]) groups[vKey] = { vendor_name: s.vendor_name, parts: [] };
        const part = s.mpn_matched || group.label;
        if (!groups[vKey].parts.includes(part)) groups[vKey].parts.push(part);
    }
    return Object.values(groups);
}

// ── RFQ Drawer (push-style side panel) ──────────────────────────────────
// Opens/closes the RFQ side panel and pushes main content left.
function openRfqDrawer() {
    const drawer = document.getElementById('rfqDrawer');
    if (drawer) drawer.classList.add('open');
    document.body.classList.add('rfq-drawer-open');
}

function closeRfqDrawer() {
    const drawer = document.getElementById('rfqDrawer');
    if (drawer) {
        drawer.classList.remove('open');
        delete drawer.dataset.loading;
    }
    document.body.classList.remove('rfq-drawer-open');
}

// ── RFQ Flow ────────────────────────────────────────────────────────────
let rfqAllParts = []; // All MPNs on this requisition
let rfqSubsMap = {}; // { primary_mpn: [sub1, sub2, ...] }

async function openBatchRfqModal(prebuiltGroups) {
    const groups = prebuiltGroups || getSelectedByVendor();
    if (!groups.length) { showToast('Select sightings first to send RFQs', 'warn'); return; }

    const modal = document.getElementById('rfqDrawer');
    const rfqPrep = document.getElementById('rfqPrepare'); if (rfqPrep) rfqPrep.style.display = '';
    const rfqRdy = document.getElementById('rfqReady'); if (rfqRdy) rfqRdy.style.display = 'none';
    const rfqPrv = document.getElementById('rfqPreview'); if (rfqPrv) { rfqPrv.classList.add('hidden'); rfqPrv.style.display = 'none'; }
    const rfqRes = document.getElementById('rfqResults'); if (rfqRes) { rfqRes.classList.add('hidden'); rfqRes.style.display = 'none'; }
    _rfqPreviewPayload = [];
    _rfqLastFailedGroups = [];
    rfqCondition = 'any';
    document.querySelectorAll('.rfq-cond-btn').forEach((b,i) => {
        b.classList.toggle('active', i === 0);
    });
    openRfqDrawer();

    const prepareAbort = new AbortController();
    const prepareTimeout = setTimeout(() => prepareAbort.abort(), 30000);
    // Wire cancel button to abort the prepare call
    const prepCancelBtn = document.querySelector('#rfqPrepare .btn-danger, #rfqPrepare [data-dismiss]');
    const _origCancel = prepCancelBtn?.onclick;
    if (prepCancelBtn) prepCancelBtn.onclick = () => { prepareAbort.abort(); closeRfqDrawer(); };
    try {
        const data = await apiFetch(`/api/requisitions/${currentReqId}/rfq-prepare`, {
            method: 'POST', body: { vendors: groups.map(g => ({ vendor_name: g.vendor_name })) },
            signal: prepareAbort.signal
        });
        rfqAllParts = data.all_parts || [];
        rfqSubsMap = data.subs_map || {};

        rfqVendorData = data.vendors.map((v, i) => {
            const listingParts = (groups[i] && groups[i].parts) || [];
            const otherParts = rfqAllParts.filter(p => !listingParts.map(lp => lp.toUpperCase()).includes(p.toUpperCase()));
            const alreadyAsked = (v.already_asked || []).map(p => p.toUpperCase());

            // Filter out exhausted parts
            const newListingParts = listingParts.filter(p => !alreadyAsked.includes(p.toUpperCase()));
            const repeatListingParts = listingParts.filter(p => alreadyAsked.includes(p.toUpperCase()));
            const newOtherParts = otherParts.filter(p => !alreadyAsked.includes(p.toUpperCase()));
            const repeatOtherParts = otherParts.filter(p => alreadyAsked.includes(p.toUpperCase()));

            return {
                ...v,
                listing_parts: listingParts,
                other_parts: otherParts,
                new_listing: newListingParts,
                repeat_listing: repeatListingParts,
                new_other: newOtherParts,
                repeat_other: repeatOtherParts,
                already_asked: alreadyAsked,
                include_repeats: false, // toggle per vendor
                included: true, // checkbox in RFQ modal
                selected_email: v.emails.length ? v.emails[0] : '',
                lookup_status: v.needs_lookup ? 'pending' : 'ready',
            };
        });
        // Filter to only vendors the user actually selected (R2-2)
        const selectedVendorNames = new Set(groups.map(g => (g.vendor_name || '').trim().toLowerCase()));
        rfqVendorData = rfqVendorData.filter(v =>
            selectedVendorNames.has((v.vendor_name || '').trim().toLowerCase())
        );
    } catch (e) {
        clearTimeout(prepareTimeout);
        if (prepCancelBtn) prepCancelBtn.onclick = _origCancel;
        const msg = e.name === 'AbortError' ? 'RFQ preparation timed out or was cancelled' : e.message;
        showToast('Failed to prepare RFQ: ' + msg, 'error');
        closeRfqDrawer();
        return;
    }
    clearTimeout(prepareTimeout);
    if (prepCancelBtn) prepCancelBtn.onclick = _origCancel;

    // Run lookups for vendors without emails (3-tier: cache → scrape → AI)
    const needsLookup = rfqVendorData.filter(v => v.lookup_status === 'pending');
    if (needsLookup.length) {
        // Prevent backdrop click from closing modal during lookup
        modal.dataset.loading = '1';
        const abortCtrl = new AbortController();
        // Show cancel button
        const rfqCancelWrap = document.getElementById('rfqPrepareCancel');
        if (rfqCancelWrap) {
            rfqCancelWrap.innerHTML = '<button class="btn btn-danger btn-sm" id="rfqCancelLookup">Cancel Lookup</button>';
            rfqCancelWrap.classList.remove('u-hidden');
            document.getElementById('rfqCancelLookup').onclick = () => abortCtrl.abort();
        }
        // Show "Skip remaining" button after 5 seconds (R2-3)
        const skipTimer = setTimeout(() => {
            if (rfqCancelWrap && !abortCtrl.signal.aborted) {
                const skipBtn = document.createElement('button');
                skipBtn.className = 'btn btn-warning btn-sm';
                skipBtn.style.marginLeft = '8px';
                skipBtn.textContent = 'Skip remaining';
                skipBtn.onclick = () => abortCtrl.abort();
                rfqCancelWrap.appendChild(skipBtn);
            }
        }, 5000);
        try {
            const rfqStatus = document.getElementById('rfqPrepareStatus');
            if (rfqStatus) rfqStatus.textContent = `Finding contacts for ${needsLookup.length} vendor(s)…`;
            needsLookup.forEach(v => { v.lookup_status = 'loading'; });
            _renderRfqPrepareProgress();
            let done = 0;
            await Promise.all(needsLookup.map(async (v) => {
                if (abortCtrl.signal.aborted) { v.lookup_status = 'no_email'; v.lookup_fail_reason = 'Cancelled'; return; }
                try {
                    const timeoutMs = 15000;
                    const fetchPromise = apiFetch('/api/vendor-contact', {
                        method: 'POST', body: { vendor_name: v.vendor_name }, signal: abortCtrl.signal
                    });
                    const timeoutPromise = new Promise((_, reject) =>
                        setTimeout(() => reject(new Error('Lookup timed out')), timeoutMs)
                    );
                    const data = await Promise.race([fetchPromise, timeoutPromise]);
                    v.emails = data.emails || [];
                    v.phones = data.phones || [];
                    v.card_id = data.card_id;
                    v.selected_email = v.emails.length ? v.emails[0] : '';
                    v.lookup_status = v.emails.length ? 'ready' : 'no_email';
                    v.contact_source = data.source || null;
                    v.contact_tier = data.tier || 0;
                    if (!v.emails.length) {
                        v.lookup_fail_reason = data.fail_reason || (data.card_id ? 'Scrape returned no emails' : 'No vendor card found');
                    }
                } catch (e) {
                    if (e.name === 'AbortError') { v.lookup_status = 'no_email'; v.lookup_fail_reason = 'Cancelled'; return; }
                    console.warn(`Vendor lookup failed for ${v.vendor_name}:`, e);
                    v.lookup_status = 'no_email';
                    v.lookup_fail_reason = e.message === 'Lookup timed out' ? 'Lookup timed out (15s)' : 'Lookup error: ' + (e.message || 'unknown');
                }
                done++;
                const st = document.getElementById('rfqPrepareStatus'); if (st) st.textContent = `Finding contacts… ${done}/${needsLookup.length} done`;
                _renderRfqPrepareProgress();
            }));
        } finally {
            clearTimeout(skipTimer);
            delete modal.dataset.loading;
            if (rfqCancelWrap) rfqCancelWrap.classList.add('u-hidden');
        }
    }

    const prep2 = document.getElementById('rfqPrepare'); if (prep2) prep2.style.display = 'none';
    const rdy2 = document.getElementById('rfqReady'); if (rdy2) rdy2.style.display = '';
    try { renderRfqVendors(); } catch(e) { console.error('renderRfqVendors failed:', e); showToast('Couldn\'t load vendor list — please try again', 'error'); }
    try { renderRfqMessage(); } catch(e) { console.error('renderRfqMessage failed:', e); }
}

function _renderRfqPrepareProgress() {
    const el = document.getElementById('rfqPrepareVendors');
    if (!el) return;
    el.innerHTML = rfqVendorData.filter(v => v.lookup_status !== 'ready' || v.needs_lookup).map(v => {
        const icon = v.lookup_status === 'loading' ? '<span class="rfq-spin">⏳</span>'
            : v.lookup_status === 'ready' ? '✅'
            : v.lookup_status === 'no_email' ? '❌'
            : '⏳';
        const reason = v.lookup_status === 'no_email' ? `<span style="color:#9ca3af;margin-left:4px">${esc(v.lookup_fail_reason || 'No contact found')}</span>` : '';
        return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">${icon} <strong>${esc(v.display_name || v.vendor_name)}</strong>${reason}</div>`;
    }).join('');
}

function renderRfqVendors() {
    const el = document.getElementById('rfqVendorList');
    if (!el) { console.error('renderRfqVendors: rfqVendorList not found'); return; }
    if (!rfqVendorData || !rfqVendorData.length) { el.innerHTML = '<p style="padding:12px;color:var(--muted)">No vendors to display</p>'; return; }
    el.innerHTML = rfqVendorData.map((v, i) => {
        let emailHtml;
        if (v.lookup_status === 'loading') {
            emailHtml = '<span class="email-loading">⏳ Looking up…</span>';
        } else if (v.lookup_status === 'no_email' || (!v.emails.length && v.lookup_status !== 'pending')) {
            const failReason = v.lookup_fail_reason || 'No email found';
            const retryBtn = v.lookup_fail_reason ? `<button class="btn btn-ghost btn-sm" onclick="rfqRetryLookup(${i})" title="Retry lookup" style="font-size:10px;padding:2px 6px">🔄 Retry</button>` : '';
            emailHtml = `<div class="rfq-email-row">
                <span class="email-none" title="${escAttr(failReason)}">${esc(failReason)}</span>
                ${retryBtn}
                <input type="email" class="rfq-email-input" placeholder="Enter email…" onchange="rfqManualEmail(${i},this.value)">
                <button class="btn btn-danger btn-sm" onclick="rfqRemoveVendor(${i})" title="Remove">✕</button>
            </div>`;
        } else if (v.emails.length) {
            if (v._editing_email) {
                emailHtml = `<div class="rfq-email-row">
                    <input type="email" class="rfq-email-input" placeholder="Enter email…" autofocus
                        onkeydown="if(event.key==='Enter')rfqConfirmCustomEmail(${i},this);if(event.key==='Escape'){delete rfqVendorData[${i}]._editing_email;renderRfqVendors()}"
                        onblur="rfqConfirmCustomEmail(${i},this)">
                    <button class="btn btn-danger btn-sm" onclick="rfqRemoveVendor(${i})" title="Remove">✕</button>
                </div>`;
            } else {
                const opts = v.emails.map(e =>
                    `<option value="${escAttr(e)}" ${e === v.selected_email ? 'selected' : ''}>${esc(e)}</option>`
                ).join('');
                emailHtml = `<div class="rfq-email-row">
                    <select class="email-select" onchange="rfqSelectEmail(${i},this.value)">
                        ${opts}
                        <option value="__custom__">✏️ Enter custom…</option>
                    </select>
                    <button class="btn btn-danger btn-sm" onclick="rfqRemoveVendor(${i})" title="Remove">✕</button>
                </div>`;
            }
        } else {
            emailHtml = '<span class="email-loading">⏳ Pending…</span>';
        }

        // Source indicator with tooltips
        const srcLabels = { cached: '💾 Cached', past_rfq: '📬 Past RFQ', website_scrape: '🌐 Website', ai_lookup: '🤖 AI', apollo: '📇 Apollo', hunter: '📧 Hunter', rocketreach: '🚀 RocketReach', explorium: '🔬 Explorium', ai: '🤖 AI', enrichment: '🔍 Auto' };
        const srcTitles = { cached: 'Contact from local database cache', past_rfq: 'Email reused from a previous RFQ', website_scrape: 'Email scraped from vendor website', ai_lookup: 'Contact found via AI search', apollo: 'Enriched via Apollo.io', hunter: 'Found via Hunter.io email finder', rocketreach: 'Found via RocketReach', explorium: 'Enriched via Explorium', ai: 'Contact found via AI search', enrichment: 'Auto-enriched from multiple sources' };
        const srcKey = (v.contact_source || '').split('+')[0];
        const srcBadge = v.contact_source ? `<span class="rfq-src-badge" title="${escAttr(srcTitles[srcKey] || 'Contact source: ' + v.contact_source)}">${srcLabels[srcKey] || v.contact_source}</span>` : '';

        // Parts breakdown
        let partsHtml = '';
        if (v.new_listing.length) {
            partsHtml += `<span class="rfq-parts-tag rfq-parts-listing" title="Vendor is actively listing these">📦 ${v.new_listing.join(', ')}</span>`;
        }
        if (v.new_other.length) {
            partsHtml += `<span class="rfq-parts-tag rfq-parts-other" title="Also requesting — vendor not currently listing">🔍 ${v.new_other.join(', ')}</span>`;
        }

        // Exhaustion badges
        const totalRepeats = v.repeat_listing.length + v.repeat_other.length;
        let exhaustHtml = '';
        if (totalRepeats > 0 && (v.new_listing.length + v.new_other.length) === 0) {
            exhaustHtml = `<span class="rfq-exhaust-full">⚠️ Already contacted for all parts</span>`;
            if (!v.include_repeats) {
                exhaustHtml += `<button class="rfq-exhaust-btn" onclick="rfqIncludeRepeats(${i})">Send anyway</button>`;
            } else {
                exhaustHtml += `<span class="rfq-exhaust-override">✓ Will re-send</span>`;
            }
        } else if (totalRepeats > 0) {
            const repeatNames = [...v.repeat_listing, ...v.repeat_other].join(', ');
            exhaustHtml = `<span class="rfq-exhaust-partial" title="Previously asked: ${repeatNames}">🔄 ${totalRepeats} part${totalRepeats > 1 ? 's' : ''} already asked — ${v.new_listing.length + v.new_other.length} new</span>`;
        }

        // Past contact / cross-req history subtitle
        let pastHtml = '';
        if (v.past_contacts && v.past_contacts.length) {
            const lastDate = v.past_contacts[0].date;
            if (lastDate) {
                const daysAgo = Math.floor((Date.now() - new Date(lastDate).getTime()) / 86400000);
                const allPastParts = [...new Set(v.past_contacts.flatMap(pc => pc.parts || []))].slice(0, 5);
                const partsStr = allPastParts.join(', ');
                const reqCount = new Set(v.past_contacts.map(pc => pc.req_id)).size;
                const reqNote = reqCount > 1 ? ` across ${reqCount} reqs` : ' on another req';
                pastHtml = `<span class="rfq-past-contact" style="font-size:10px;color:var(--text2);display:block;margin-top:2px">Also contacted ${daysAgo}d ago${reqNote}${partsStr ? ' for ' + esc(partsStr) : ''}</span>`;
            }
        }

        return `<div class="rfq-vendor-row ${totalRepeats > 0 && (v.new_listing.length + v.new_other.length) === 0 && !v.include_repeats ? 'rfq-vendor-exhausted' : ''} ${!v.included ? 'rfq-vendor-excluded' : ''}">
            <input type="checkbox" ${v.included ? 'checked' : ''} onchange="rfqToggleVendor(${i})" class="rfq-vendor-cb" title="Include in RFQ">
            <div class="rfq-vendor-info">
                <strong>${esc(v.display_name || v.vendor_name)}</strong>
                ${pastHtml}
                <div class="rfq-parts-breakdown">${partsHtml}</div>
                ${exhaustHtml}
                ${srcBadge}
            </div>
            ${emailHtml}
        </div>`;
    }).join('');

    // Count sendable (included + has email + has new parts or override)
    const sendable = rfqVendorData.filter(v => v.included && v.selected_email && _vendorHasPartsToSend(v));
    const ready = sendable.length;
    const excluded = rfqVendorData.filter(v => !v.included).length;
    const exhausted = rfqVendorData.filter(v => v.included && !_vendorHasPartsToSend(v)).length;
    let summary = `${ready} of ${rfqVendorData.length} vendors ready to send`;
    if (excluded > 0) summary += ` · ${excluded} unchecked`;
    if (exhausted > 0) summary += ` · ${exhausted} skipped (already contacted)`;
    const rfqSum = document.getElementById('rfqSummary'); if (rfqSum) rfqSum.textContent = summary;
}

function _vendorHasPartsToSend(v) {
    if (v.new_listing.length + v.new_other.length > 0) return true;
    if (v.include_repeats && (v.repeat_listing.length + v.repeat_other.length > 0)) return true;
    return false;
}

function rfqIncludeRepeats(idx) {
    rfqVendorData[idx].include_repeats = true;
    renderRfqVendors();
}

function rfqToggleVendor(idx) {
    rfqVendorData[idx].included = !rfqVendorData[idx].included;
    renderRfqVendors();
}

function rfqSelectAllVendors() {
    rfqVendorData.forEach(v => v.included = true);
    renderRfqVendors();
}

function rfqDeselectAllVendors() {
    rfqVendorData.forEach(v => v.included = false);
    renderRfqVendors();
}

let rfqCondition = 'any';

function setRfqCondition(cond, btn) {
    rfqCondition = cond;
    document.querySelectorAll('.rfq-cond-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderRfqMessage();
}

// ── RFQ Email Templates ──────────────────────────────────────────────
const _rfqBuiltinTemplates = [
    {
        id: '__standard__',
        name: 'Standard RFQ',
        subject: 'RFQ: {PARTS}',
        body: `Hi,

We are sourcing the following parts — please send your best offer if available:

{PARTS_LIST}
{CONDITION}
Please include with your quote:
  - Qty available / Lead time
  - Unit price (USD)
  - Condition (New / Used / Refurb)
  - Photos if available
  - Warranty & payment terms

Thanks,
{SENDER}
Trio Supply Chain Solutions`
    },
    {
        id: '__urgent__',
        name: 'Urgent RFQ',
        subject: 'URGENT RFQ: {PARTS}',
        body: `Hi,

We have an URGENT requirement for the following parts — same-day quotes appreciated:

{PARTS_LIST}
{CONDITION}
Please reply ASAP with:
  - Qty available / Lead time
  - Unit price (USD)
  - Condition & date codes
  - Fastest shipping option

This is time-sensitive — thank you for your quick response.

{SENDER}
Trio Supply Chain Solutions`
    },
    {
        id: '__broker__',
        name: 'Broker Outreach',
        subject: 'Sourcing Inquiry: {PARTS}',
        body: `Hello,

We are a supply chain solutions company sourcing the following components for a client:

{PARTS_LIST}
{CONDITION}
If you have stock or access to any of these, please share:
  - Qty on hand / Lead time
  - Best unit price (USD)
  - Condition, date codes, packaging
  - MOQ if applicable

We're open to alternatives or cross-references if exact matches aren't available.

Best regards,
{SENDER}
Trio Supply Chain Solutions`
    }
];

function rfqLoadTemplates() {
    const sel = document.getElementById('rfqTemplateSelect');
    if (!sel) return;
    const customs = JSON.parse(safeGet('rfq_templates', '[]'));
    const all = [..._rfqBuiltinTemplates, ...customs];
    sel.innerHTML = `<option value="">— Select template —</option>` +
        all.map(t => `<option value="${escAttr(t.id)}">${esc(t.name)}${t.id.startsWith('__') ? '' : ' (custom)'}</option>`).join('');
    const delBtn = document.getElementById('rfqDeleteTplBtn');
    if (delBtn) delBtn.style.display = 'none';
}

function rfqApplyTemplate(templateId) {
    if (!templateId) return;
    const customs = JSON.parse(safeGet('rfq_templates', '[]'));
    const all = [..._rfqBuiltinTemplates, ...customs];
    const tpl = all.find(t => t.id === templateId);
    if (!tpl) return;

    const allParts = [...new Set(rfqAllParts)];
    const partsStr = allParts.slice(0, 5).join(', ') + (allParts.length > 5 ? '…' : '');
    const fullName = (window.userName || 'Trio Supply Chain Solutions').trim();
    const firstName = fullName.split(' ')[0];
    let condLine = '';
    if (rfqCondition === 'new') condLine = 'Condition: NEW ONLY\n\n';
    else if (rfqCondition === 'used') condLine = 'Condition: USED / REFURBISHED ACCEPTABLE\n\n';
    const partsListStr = allParts.map(p => {
        const subs = (rfqSubsMap[p] || []).filter(s => s.toUpperCase() !== p.toUpperCase());
        return subs.length ? `  ${p}  (also acceptable: ${subs.join(', ')})` : `  ${p}`;
    }).join('\n');

    const subject = tpl.subject.replace('{PARTS}', partsStr);
    const body = tpl.body
        .replace('{PARTS_LIST}', partsListStr + '\n')
        .replace('{CONDITION}', condLine)
        .replace('{SENDER}', firstName)
        .replace('{PARTS}', partsStr);

    const rfqSubj = document.getElementById('rfqSubject');
    const rfqBod = document.getElementById('rfqBody');
    if (rfqSubj) rfqSubj.value = subject;
    if (rfqBod) rfqBod.value = body;
    _saveRfqDraft();

    // Show/hide delete button for custom templates
    const delBtn = document.getElementById('rfqDeleteTplBtn');
    if (delBtn) delBtn.style.display = templateId.startsWith('__') ? 'none' : '';
}

function rfqSaveTemplate() {
    promptInput('Save Template', 'Template name:', function(name) {
        if (!name || !name.trim()) return;
        const subject = document.getElementById('rfqSubject')?.value || '';
        const body = document.getElementById('rfqBody')?.value || '';
        const id = 'custom_' + Date.now();
        const customs = JSON.parse(safeGet('rfq_templates', '[]'));
        customs.push({ id, name: name.trim(), subject, body });
        safeSet('rfq_templates', JSON.stringify(customs));
        rfqLoadTemplates();
        document.getElementById('rfqTemplateSelect').value = id;
        showToast('Template saved', 'success');
    }, {required: true, placeholder: 'Enter template name...'});
}

function rfqDeleteTemplate() {
    const sel = document.getElementById('rfqTemplateSelect');
    const id = sel?.value;
    if (!id || id.startsWith('__')) return;
    const customs = JSON.parse(safeGet('rfq_templates', '[]'));
    safeSet('rfq_templates', JSON.stringify(customs.filter(t => t.id !== id)));
    rfqLoadTemplates();
    showToast('Template deleted', 'success');
}

function buildVendorBody(v) {
    // Determine which parts to include
    let listingParts = [...v.new_listing];
    let otherParts = [...v.new_other];
    if (v.include_repeats) {
        listingParts = [...listingParts, ...v.repeat_listing];
        otherParts = [...otherParts, ...v.repeat_other];
    }
    const allSendParts = [...listingParts, ...otherParts];
    if (!allSendParts.length) return null;

    // Sender first name
    const fullName = (window.userName || 'Trio Supply Chain Solutions').trim();
    const firstName = fullName.split(' ')[0];

    // Condition line
    let condLine = '';
    if (rfqCondition === 'new') condLine = 'Condition: NEW ONLY\n\n';
    else if (rfqCondition === 'used') condLine = 'Condition: USED / REFURBISHED ACCEPTABLE\n\n';

    let body = 'Hi,\n\n';

    // Build qty lookup from cached requirements
    const _reqQtys = {};
    const _cachedReqs = _ddReqCache[currentReqId] || [];
    for (const rq of _cachedReqs) {
        if (rq.primary_mpn) _reqQtys[rq.primary_mpn.toUpperCase()] = rq.target_qty || 0;
    }

    body += 'We are sourcing the following parts — please send your best offer if available:\n\n';
    body += allSendParts.map(p => {
        const qty = _reqQtys[p.toUpperCase()];
        const qtyStr = qty ? `  Qty: ${Number(qty).toLocaleString()}` : '';
        const subs = (rfqSubsMap[p] || []).filter(s => s.toUpperCase() !== p.toUpperCase());
        if (subs.length) {
            return `  ${p}${qtyStr}  (also acceptable: ${subs.join(', ')})`;
        }
        return `  ${p}${qtyStr}`;
    }).join('\n');
    body += '\n';

    if (condLine) body += '\n' + condLine;

    // Add bid due date if available
    const _reqInfo = _reqListData.find(r => r.id === currentReqId);
    if (_reqInfo && _reqInfo.deadline) {
        const dl = _reqInfo.deadline === 'ASAP' ? 'ASAP' : fmtDate(_reqInfo.deadline);
        body += `\nBid due: ${dl}\n`;
    }

    body += `
Please include with your quote:
  - Qty available / Lead time
  - Unit price (USD)
  - Condition (New / Used / Refurb)
  - Photos if available
  - Warranty & payment terms

Thanks,
${firstName}
Trio Supply Chain Solutions`;

    return body;
}

function renderRfqMessage() {
    rfqLoadTemplates();
    // Restore saved draft if available
    const draftKey = `rfq_draft_${currentReqId}`;
    let saved = safeGet(draftKey);
    const rfqSubj = document.getElementById('rfqSubject');
    const rfqBod = document.getElementById('rfqBody');

    if (saved) {
        try {
            const draft = JSON.parse(saved);
            if (draft.subject && draft.subject.trim() && draft.body && draft.body.trim()) {
                if (rfqSubj) rfqSubj.value = draft.subject;
                if (rfqBod) rfqBod.value = draft.body;
            } else {
                localStorage.removeItem(draftKey);
                saved = null;
            }
        } catch {
            localStorage.removeItem(draftKey);
            saved = null;
        }
    }
    if (!saved) {
        // Subject uses all unique parts across all vendors being sent, with qty for small lists
        const allParts = [...new Set(rfqAllParts)];
        const condTag = rfqCondition !== 'any' ? ` [${rfqCondition.toUpperCase()}]` : '';
        const _cachedR = _ddReqCache[currentReqId] || [];
        const _qtyMap = {};
        for (const rq of _cachedR) { if (rq.primary_mpn) _qtyMap[rq.primary_mpn.toUpperCase()] = rq.target_qty || 0; }
        const partLabels = allParts.slice(0, 5).map(p => {
            const q = _qtyMap[p.toUpperCase()];
            return q ? `${p} x${Number(q).toLocaleString()}` : p;
        });
        if (rfqSubj) rfqSubj.value = `RFQ: ${partLabels.join(', ')}${allParts.length > 5 ? '…' : ''}${condTag} — ${currentReqName}`;

        // Preview body shows a sample for the first vendor with parts to send
        const sample = rfqVendorData.find(v => _vendorHasPartsToSend(v));
        if (sample) {
            if (rfqBod) rfqBod.value = buildVendorBody(sample) || '';
        } else {
            if (rfqBod) rfqBod.value = '(No vendors with new parts to send)';
        }
    }

    // Auto-save on edit
    if (rfqSubj) rfqSubj.oninput = () => _saveRfqDraft();
    if (rfqBod) rfqBod.oninput = () => _saveRfqDraft();

}

function _saveRfqDraft() {
    if (!currentReqId) return;
    const subject = document.getElementById('rfqSubject')?.value || '';
    const body = document.getElementById('rfqBody')?.value || '';
    safeSet(`rfq_draft_${currentReqId}`, JSON.stringify({ subject, body }));
}


function rfqSelectEmail(idx, value) {
    if (value === '__custom__') {
        // Switch to inline input mode instead of prompt()
        rfqVendorData[idx]._editing_email = true;
        renderRfqVendors();
    } else {
        rfqVendorData[idx].selected_email = value;
    }
}

function rfqConfirmCustomEmail(idx, inputEl) {
    const email = (inputEl.value || '').trim().toLowerCase();
    if (email && email.includes('@')) {
        rfqVendorData[idx].selected_email = email;
        if (!rfqVendorData[idx].emails.includes(email)) {
            rfqVendorData[idx].emails.unshift(email);
        }
        apiFetch('/api/vendor-card/add-email', {
            method: 'POST', body: { vendor_name: rfqVendorData[idx].vendor_name, email }
        }).catch(() => showToast('Failed to save email', 'error'));
    }
    delete rfqVendorData[idx]._editing_email;
    renderRfqVendors();
}

function rfqManualEmail(idx, value) {
    const email = value.trim().toLowerCase();
    if (email && email.includes('@')) {
        rfqVendorData[idx].selected_email = email;
        rfqVendorData[idx].emails.unshift(email);
        rfqVendorData[idx].lookup_status = 'ready';
        apiFetch('/api/vendor-card/add-email', {
            method: 'POST', body: { vendor_name: rfqVendorData[idx].vendor_name, email }
        }).catch(() => showToast('Failed to save email', 'error'));
        renderRfqVendors();
    }
}

function rfqRemoveVendor(idx) {
    rfqVendorData.splice(idx, 1);
    if (!rfqVendorData.length) { closeRfqDrawer(); return; }
    renderRfqVendors();
    renderRfqMessage();
}

async function rfqRetryLookup(idx) {
    const v = rfqVendorData[idx];
    if (!v) return;
    v.lookup_status = 'loading';
    v.lookup_fail_reason = null;
    renderRfqVendors();
    try {
        const data = await apiFetch('/api/vendor-contact', {
            method: 'POST', body: { vendor_name: v.vendor_name }
        });
        v.emails = data.emails || [];
        v.phones = data.phones || [];
        v.card_id = data.card_id;
        v.selected_email = v.emails.length ? v.emails[0] : '';
        v.lookup_status = v.emails.length ? 'ready' : 'no_email';
        v.contact_source = data.source || null;
        if (!v.emails.length) {
            v.lookup_fail_reason = data.fail_reason || 'No contact found after retry';
        }
    } catch (e) {
        v.lookup_status = 'no_email';
        v.lookup_fail_reason = 'Retry failed: ' + (e.message || 'unknown');
    }
    renderRfqVendors();
}

let _rfqPreviewPayload = [];
let _rfqLastFailedGroups = [];

function _buildRfqPayload() {
    const subject = document.getElementById('rfqSubject')?.value || '';
    const sendable = rfqVendorData.filter(g => g.included && g.selected_email && _vendorHasPartsToSend(g));
    if (!sendable.length) return [];
    return sendable.map(g => {
        // Per-vendor body using buildVendorBody for each vendor's specific parts
        const body = buildVendorBody(g) || document.getElementById('rfqBody')?.value || '';
        let sentParts = [...g.new_listing, ...g.new_other];
        if (g.include_repeats) sentParts = [...sentParts, ...g.repeat_listing, ...g.repeat_other];
        return {
            vendor_name: g.vendor_name, vendor_email: g.selected_email,
            parts: sentParts, subject, body
        };
    });
}

function rfqShowPreview() {
    _rfqPreviewPayload = _buildRfqPayload();
    if (!_rfqPreviewPayload.length) { showToast('No vendors with email and new parts to send', 'error'); return; }

    const previewEl = document.getElementById('rfqPreviewCards');
    previewEl.innerHTML = _rfqPreviewPayload.map((p, i) => `
        <div class="rfq-preview-card" style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;background:var(--bg2)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <strong style="font-size:13px">${esc(p.vendor_name)}</strong>
                <span style="font-size:11px;color:var(--text2)">${esc(p.vendor_email)}</span>
            </div>
            <div style="font-size:11px;color:var(--text2);margin-bottom:4px"><strong>Subject:</strong> ${esc(p.subject)}</div>
            <div style="font-size:11px;color:var(--text2);margin-bottom:4px"><strong>Parts:</strong> ${esc(p.parts.join(', '))}</div>
            <details>
                <summary style="font-size:11px;color:var(--teal);cursor:pointer">Show email body</summary>
                <pre style="font-size:11px;white-space:pre-wrap;margin-top:4px;padding:8px;background:var(--bg1);border-radius:4px;max-height:200px;overflow-y:auto">${esc(p.body)}</pre>
            </details>
        </div>
    `).join('');

    const sumEl = document.getElementById('rfqPreviewSummary');
    if (sumEl) sumEl.textContent = `${_rfqPreviewPayload.length} email(s) ready to send`;

    document.getElementById('rfqReady').style.display = 'none';
    const preview = document.getElementById('rfqPreview');
    preview.classList.remove('hidden');
    preview.style.display = '';
}

function rfqBackToCompose() {
    const preview = document.getElementById('rfqPreview');
    preview.classList.add('hidden');
    preview.style.display = 'none';
    document.getElementById('rfqReady').style.display = '';
}

async function rfqConfirmSend() {
    const btn = document.getElementById('rfqSendBtn');
    await guardBtn(btn, 'Sending…', async () => {
        if (!_rfqPreviewPayload.length) { showToast('No emails to send', 'error'); return; }
        try {
            const data = await apiFetch(`/api/requisitions/${currentReqId}/rfq`, {
                method: 'POST', body: { groups: _rfqPreviewPayload }
            });
            const results = data.results || [];
            const sent = results.filter(r => r.status === 'sent').length;
            const failed = results.filter(r => r.status !== 'sent');

            if (failed.length > 0) {
                // Show results panel instead of closing
                _rfqShowResults(results);
            } else {
                showToast(`Sent ${sent} of ${results.length} RFQs`, 'success');
                closeRfqDrawer();
            }
            safeRemove(`rfq_draft_${currentReqId}`);
            selectedSightings.clear();
            if (_ddSelectedSightings[currentReqId]) delete _ddSelectedSightings[currentReqId];
            if (_ddSightingsCache[currentReqId]) delete _ddSightingsCache[currentReqId];
            renderSources();
            loadActivity();

            // Auto-poll inbox after successful send to catch quick replies
            if (sent > 0) {
                _scheduleAutoPoll(currentReqId);
            }
        } catch (e) {
            if (e.isRateLimit) {
                const secs = e.retryAfter || 60;
                _showRateLimitCountdown(secs);
            } else {
                showToast('Send error: ' + e.message, 'error');
            }
        }
    });
}

function _rfqShowResults(results) {
    const listEl = document.getElementById('rfqResultsList');
    _rfqLastFailedGroups = [];
    listEl.innerHTML = results.map(r => {
        const ok = r.status === 'sent';
        if (!ok) {
            const matchGroup = _rfqPreviewPayload.find(p => p.vendor_name === r.vendor_name && p.vendor_email === (r.vendor_email || r.email));
            if (matchGroup) _rfqLastFailedGroups.push(matchGroup);
        }
        return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;font-size:12px;border-bottom:1px solid var(--border)">
            <span>${ok ? '✅' : '❌'}</span>
            <strong>${esc(r.vendor_name)}</strong>
            <span style="color:var(--text2)">${esc(r.vendor_email || r.email || '')}</span>
            ${!ok ? `<span style="color:var(--red);margin-left:auto">${esc(r.error || 'Send failed')}</span>` : '<span style="color:var(--green);margin-left:auto">Sent</span>'}
        </div>`;
    }).join('');

    const retryBtn = document.getElementById('rfqRetryBtn');
    if (retryBtn) { if (_rfqLastFailedGroups.length) retryBtn.classList.remove('u-hidden'); else retryBtn.classList.add('u-hidden'); }

    const sent = results.filter(r => r.status === 'sent').length;
    const failedCount = results.length - sent;
    showToast(`Sent ${sent} of ${results.length} RFQs (${failedCount} failed)`, failedCount > 0 ? 'warn' : 'success');

    document.getElementById('rfqPreview').style.display = 'none';
    document.getElementById('rfqReady').style.display = 'none';
    const resultsDiv = document.getElementById('rfqResults');
    resultsDiv.classList.remove('hidden');
    resultsDiv.style.display = '';
}

async function rfqRetryFailed() {
    if (!_rfqLastFailedGroups.length) return;
    const btn = document.getElementById('rfqRetryBtn');
    await guardBtn(btn, 'Retrying…', async () => {
        try {
            const data = await apiFetch(`/api/requisitions/${currentReqId}/rfq`, {
                method: 'POST', body: { groups: _rfqLastFailedGroups }
            });
            _rfqShowResults(data.results || []);
            loadActivity();
        } catch (e) {
            if (e.isRateLimit) {
                _showRateLimitCountdown(e.retryAfter || 60);
            } else {
                showToast('Retry error: ' + e.message, 'error');
            }
        }
    });
}

// ── Rate-Limit Countdown + Auto Inbox Poll ─────────────────────────────
// Shows a countdown toast when the user hits the RFQ rate limit (5/min).
// Called by: rfqConfirmSend on 429 response
// Depends on: showToast

let _rateLimitTimer = null;

function _showRateLimitCountdown(totalSecs) {
    if (_rateLimitTimer) clearInterval(_rateLimitTimer);
    let remaining = totalSecs;
    const toastId = 'rate-limit-toast';
    // Remove any existing rate-limit toast
    document.getElementById(toastId)?.remove();
    const toast = document.createElement('div');
    toast.id = toastId;
    toast.className = 'toast toast-warn';
    toast.style.cssText = 'position:fixed;bottom:80px;right:20px;z-index:10000;padding:12px 18px;border-radius:8px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.15);display:flex;align-items:center;gap:10px';
    const update = () => {
        toast.innerHTML = `<span style="font-size:18px">\u23F1</span> Rate limit reached \u2014 retry in <strong>${remaining}s</strong>`;
    };
    update();
    document.body.appendChild(toast);
    _rateLimitTimer = setInterval(() => {
        remaining--;
        if (remaining <= 0) {
            clearInterval(_rateLimitTimer);
            _rateLimitTimer = null;
            toast.remove();
            showToast('Rate limit cleared \u2014 you can send again', 'success');
        } else {
            update();
        }
    }, 1000);
}

// Auto-poll inbox 30s and 90s after sending RFQs to catch quick replies
// Called by: rfqConfirmSend after successful send
// Depends on: apiFetch, showToast
let _autoPollTimers = [];

function _scheduleAutoPoll(reqId) {
    // Clear any prior auto-poll timers
    _autoPollTimers.forEach(t => clearTimeout(t));
    _autoPollTimers = [];

    const doPoll = async (label) => {
        try {
            const data = await apiFetch(`/api/requisitions/${reqId}/poll`, { method: 'POST' });
            const responses = data.responses || [];
            if (responses.length > 0) {
                showToast(`Inbox check: ${responses.length} new repl${responses.length === 1 ? 'y' : 'ies'} found`, 'success');
                // Refresh the drill-down if it's open for this req
                if (_ddTabCache[reqId]) delete _ddTabCache[reqId].offers;
                if (_ddTabCache[reqId]) delete _ddTabCache[reqId].activity;
            } else {
                showToast(`${label}: no new replies yet`, 'info');
            }
        } catch {
            // Silent — don't bother user if auto-poll fails
        }
    };

    _autoPollTimers.push(setTimeout(() => doPoll('30s inbox check'), 30000));
    _autoPollTimers.push(setTimeout(() => doPoll('90s inbox check'), 90000));
}

// ── Click-to-Call Logging ───────────────────────────────────────────────
async function logCall(event, vendorName, vendorPhone, mpn) {
    try {
        await apiFetch('/api/contacts/phone', {
            method: 'POST', body: { requisition_id: currentReqId, vendor_name: vendorName,
                                   vendor_phone: vendorPhone, parts: mpn ? [mpn] : [] }
        });
        loadActivity();
    } catch (e) { logCatchError('logCall', e); showToast('Failed to log call', 'error'); }
}

// ── Vendor Card Popup ──────────────────────────────────────────────────
export async function openVendorPopup(cardId) {
    _vendorEmailsLoaded = null;  // Reset so emails reload for new vendor
    let card;
    try { card = await apiFetch(`/api/vendors/${cardId}`); }
    catch (e) { logCatchError('openVendorPopup', e); showToast('Failed to load vendor', 'error'); return; }

    let html = `<div class="vp-header">
        <h2 onclick="editVendorField(${card.id},'display_name',this)" style="cursor:pointer" title="Click to edit">${esc(card.display_name)}</h2>
        <div class="vp-rating">${stars(card.avg_rating, card.review_count)}</div>
    </div>`;

    // Blacklist toggle + admin delete
    const blOn = card.is_blacklisted;
    html += `<div class="vp-section" style="padding-bottom:8px;margin-bottom:10px;display:flex;gap:8px;align-items:center">
        <button class="btn-blacklist ${blOn ? 'vp-bl-on' : 'vp-bl-off'}" onclick="vpToggleBlacklist(${card.id}, ${!blOn})">
            ${blOn ? '🚫 Blacklisted' : 'Blacklist'}
        </button>
        ${blOn ? '<span style="font-size:10px;color:var(--red);margin-left:8px">This vendor is hidden from all search results</span>' : ''}
        ${window.__isAdmin ? `<button class="btn btn-danger btn-sm" onclick="deleteVendor(${card.id},'${escAttr(card.display_name)}')" style="margin-left:auto;font-size:10px">Delete Vendor</button>` : ''}
    </div>`;

    // Info
    html += '<div class="vp-section">';
    if (card.website) html += `<div class="vp-field"><span class="vp-label">Website</span> <span onclick="editVendorField(${card.id},'website',this)" style="cursor:pointer;color:var(--teal);text-decoration:underline" title="Click to edit">${esc(card.website)}</span></div>`;
    else html += `<div class="vp-field"><span class="vp-label">Website</span> <span onclick="editVendorField(${card.id},'website',this)" style="cursor:pointer;color:var(--muted);font-size:11px" title="Click to add">+ Add website</span></div>`;
    if (card.linkedin_url) html += `<div class="vp-field"><span class="vp-label">LinkedIn</span> <a href="${escAttr(card.linkedin_url)}" target="_blank" style="color:var(--teal)">Company Page ↗</a></div>`;
    html += `<div class="vp-field"><span class="vp-label">Seen in</span> ${card.sighting_count} search results</div>`;
    // Enrichment tags
    if (card.industry || card.employee_size || card.hq_city) {
        html += '<div class="enrich-bar" style="margin-top:6px">';
        if (card.industry) html += `<span class="enrich-tag">${esc(card.industry)}</span>`;
        if (card.employee_size) html += `<span class="enrich-tag">👥 ${esc(card.employee_size)}</span>`;
        if (card.hq_city) html += `<span class="enrich-tag">📍 ${esc(card.hq_city)}${card.hq_state ? ', ' + esc(card.hq_state) : ''}</span>`;
        if (card.hq_country && card.hq_country !== 'US') html += `<span class="enrich-tag">${esc(card.hq_country)}</span>`;
        html += '</div>';
    }
    // Material tags (AI-generated brands + commodities)
    const hasTags = (card.brand_tags && card.brand_tags.length) || (card.commodity_tags && card.commodity_tags.length);
    if (hasTags) {
        html += '<div style="margin-top:6px">';
        if (card.brand_tags && card.brand_tags.length) {
            html += '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px">';
            html += card.brand_tags.map(b => `<span style="display:inline-block;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;background:rgba(59,130,246,.12);color:var(--blue)">${esc(b)}</span>`).join('');
            html += '</div>';
        }
        if (card.commodity_tags && card.commodity_tags.length) {
            html += '<div style="display:flex;flex-wrap:wrap;gap:4px">';
            html += card.commodity_tags.map(c => `<span style="display:inline-block;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;background:rgba(245,158,11,.12);color:var(--amber)">${esc(c)}</span>`).join('');
            html += '</div>';
        }
        html += '</div>';
    }
    // Action buttons
    const vendorDomain = card.domain || (card.website ? card.website.replace(/https?:\/\/(www\.)?/, '').split('/')[0] : '');
    html += `<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        <button class="btn-enrich" onclick="unifiedEnrichVendor(${card.id})">Enrich</button>
    </div>`;

    // Vendor Score (order advancement based)
    if (card.vendor_score != null) {
        const vs = Math.round(card.vendor_score);
        const vsClass = vs >= 66 ? 'eng-high' : vs >= 33 ? 'eng-med' : 'eng-low';
        const advText = card.advancement_score != null ? `Advancement: ${Math.round(card.advancement_score)}` : '';
        html += `<div class="metrics-panel u-items-center">
            <div class="engagement-ring ${vsClass}">${vs}</div>
            <div style="flex:1;font-size:11px">
                <div style="font-weight:700;margin-bottom:2px">Vendor Score</div>
                <div style="color:var(--text2);display:flex;gap:10px;flex-wrap:wrap">
                    ${advText ? `<span>${advText}</span>` : ''}
                    ${card.total_outreach != null ? `<span>Outreach: ${card.total_outreach}</span>` : ''}
                    ${card.total_responses != null ? `<span>Replies: ${card.total_responses}</span>` : ''}
                </div>
            </div>
        </div>`;
    } else if (card.is_new_vendor) {
        html += `<div class="metrics-panel u-items-center">
            <div class="engagement-ring eng-low" style="border-color:var(--muted);color:var(--muted)">--</div>
            <div style="flex:1;font-size:11px">
                <div style="font-weight:700;margin-bottom:2px">Vendor Score</div>
                <div style="color:var(--muted)">New Vendor \u2014 No Order History</div>
            </div>
        </div>`;
    }
    html += '</div>';

    // Email Metrics (loaded async)
    html += `<div id="vpEmailMetrics"></div>`;

    // Intel Card container (loaded async)
    html += `<div id="vpIntelCard"></div>`;

    // Contacts (structured — loaded async)
    html += `<div class="vp-section">
        <div class="vp-label" style="display:flex;justify-content:space-between;align-items:center">
            Contacts
            <span style="display:flex;gap:4px">
                ${vendorDomain ? `<button class="btn btn-ghost btn-sm" onclick="openSuggestedContacts('vendor',${card.id},'${escAttr(vendorDomain)}','${escAttr(card.display_name)}')">Find Contacts</button>` : ''}
                <button class="btn btn-ghost btn-sm" onclick="openAddVendorContact(${card.id})">+ Add</button>
            </span>
        </div>
        <div id="vpContactsList"><p class="vp-muted" style="font-size:11px">Loading contacts...</p></div>
        <div id="vpContactNudges"></div>
    </div>`;

    // Recent Activity (loaded async)
    html += `<div class="vp-section">
        <div class="vp-label" style="display:flex;justify-content:space-between;align-items:center">
            Recent Activity
            <span id="vpActHealth-${card.id}" style="margin-right:auto;margin-left:8px"></span>
            <span style="display:flex;gap:4px">
                <button class="btn btn-ghost btn-sm" onclick="openVendorLogNoteModal(${card.id},'${escAttr(card.display_name)}')">+ Note</button>
            </span>
        </div>
        <div id="vpActivityList-${card.id}"><p class="vp-muted" style="font-size:11px">Loading...</p></div>
    </div>`;

    // Material Profile (brands/manufacturers)
    const brands = card.brands || [];
    const uniqueParts = card.unique_parts || 0;
    if (brands.length || uniqueParts) {
        html += '<div class="vp-section"><div class="vp-label">Material Profile</div>';
        if (uniqueParts) html += `<div class="vp-field" style="margin-bottom:6px"><span style="font-size:11px;color:var(--muted)">Seen with ${uniqueParts} unique part number${uniqueParts !== 1 ? 's' : ''}</span></div>`;
        if (brands.length) {
            html += '<div style="display:flex;flex-wrap:wrap;gap:4px">';
            html += brands.map(b => `<span class="badge b-src" style="font-size:10px;padding:2px 8px" title="${b.count} sighting${b.count !== 1 ? 's' : ''}">${esc(b.name)} <span style="opacity:.6">×${b.count}</span></span>`).join('');
            html += '</div>';
        } else {
            html += '<div class="vp-item vp-muted">No manufacturer data yet</div>';
        }
        html += '</div>';
    }

    // Confirmed Quotes (buyer-entered offers)
    html += `<div class="vp-section">
        <div class="vp-label" style="display:flex;justify-content:space-between;align-items:center">
            Confirmed Quotes
            <button class="btn btn-ghost btn-sm" onclick="toggleConfirmedQuotes(${card.id})">View Quotes</button>
        </div>
        <div id="vpConfirmedQuotes" style="display:none">
            <div id="vpConfirmedQuotesList"><p class="vp-muted" style="font-size:11px">Loading...</p></div>
        </div>
    </div>`;

    // Parts Sightings (collapsible, searchable summary)
    html += `<div class="vp-section">
        <div class="vp-label" style="display:flex;justify-content:space-between;align-items:center">
            Parts Sightings
            <span style="display:flex;align-items:center;gap:6px">
                ${card.unique_parts ? `<span style="font-size:10px;padding:2px 8px;border-radius:10px;background:var(--surface);border:1px solid var(--border);color:var(--text2);font-weight:600">${card.unique_parts} parts</span>` : ''}
                <button class="btn btn-ghost btn-sm" onclick="togglePartsSightings(${card.id})">View Parts</button>
            </span>
        </div>
        <div id="vpPartsSightings" style="display:none">
            <div style="margin-bottom:8px">
                <input id="vpPartsSightingsSearch" placeholder="Search by MPN..."
                    style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:12px"
                    oninput="debouncePartsSightingsSearch(${card.id})">
            </div>
            <div id="vpPartsSightingsList"><p class="vp-muted" style="font-size:11px">Loading...</p></div>
            <div id="vpPartsSightingsMore" style="display:none;text-align:center;margin-top:8px">
                <button class="btn btn-ghost btn-sm" onclick="loadMorePartsSightings(${card.id})">Load More</button>
            </div>
        </div>
    </div>`;

    // Offer History (collapsible, searchable, paginated)
    html += `<div class="vp-section">
        <div class="vp-label" style="display:flex;justify-content:space-between;align-items:center">
            Offer History
            <button class="btn btn-ghost btn-sm" onclick="toggleOfferHistory(${card.id})">View History</button>
        </div>
        <div id="vpOfferHistory" style="display:none">
            <div style="margin-bottom:8px">
                <input id="vpOfferHistorySearch" placeholder="Search by MPN..."
                    style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:12px"
                    oninput="debounceOfferHistorySearch(${card.id})">
            </div>
            <div id="vpOfferHistoryList"><p class="vp-muted" style="font-size:11px">Loading...</p></div>
            <div id="vpOfferHistoryMore" style="display:none;text-align:center;margin-top:8px">
                <button class="btn btn-ghost btn-sm" onclick="loadMoreOfferHistory(${card.id})">Load More</button>
            </div>
        </div>
    </div>`;

    // Reviews
    html += '<div class="vp-section"><div class="vp-label">Reviews</div>';
    if (card.reviews.length) {
        html += card.reviews.map(r => `<div class="vp-review">
            <div class="vp-review-header">
                <span class="stars">${'★'.repeat(r.rating)}${'☆'.repeat(5 - r.rating)}</span>
                <span class="vp-review-author">${esc(r.user_name)} · ${fmtDate(r.created_at)}</span>
            </div>
            ${r.comment ? `<div class="vp-review-comment">${esc(r.comment)}</div>` : ''}
        </div>`).join('');
    } else {
        html += '<div class="vp-item vp-muted">No reviews yet</div>';
    }
    html += '</div>';

    // Add review form
    html += `<div class="vp-section">
        <div class="vp-label">Add Review</div>
        <div class="vp-review-form">
            <div class="vp-star-picker" id="vpStarPicker">
                ${[1,2,3,4,5].map(n => `<span class="vp-star" onclick="vpSetRating(${n})" data-n="${n}">☆</span>`).join('')}
            </div>
            <input id="vpComment" class="vp-input" placeholder="Short comment (optional)…" maxlength="500">
            <button class="btn btn-primary btn-sm" onclick="vpSubmitReview(${card.id})">Submit</button>
        </div>
    </div>`;

    // Vendor Emails section
    html += `<div class="vp-section">
        <div class="vp-label" style="cursor:pointer" onclick="toggleVendorEmails(${card.id})">
            Emails <span style="font-size:10px;color:var(--muted)">▼</span>
        </div>
        <div id="vpEmails" style="display:none">
            <p class="vp-muted" style="font-size:11px">Loading...</p>
        </div>
    </div>`;

    const vpcEl = document.getElementById('vendorPopupContent');
    if (vpcEl) vpcEl.innerHTML = html;
    openModal('vendorPopup');

    // Vendor Intelligence insights card
    if (vpcEl) {
        _renderEntityInsightsCard('vendors', card.id, vpcEl, { title: 'Vendor Intelligence' });
    }

    // Load contacts, activities, metrics, and intel asynchronously
    loadVendorContacts(card.id);
    loadVendorActivities(card.id);
    loadVendorActivityStatus(card.id);
    loadVendorEmailMetrics(card.id);
    const intelEl = document.getElementById('vpIntelCard');
    if (intelEl && card.display_name) {
        loadCompanyIntel(card.display_name, vendorDomain, intelEl);
    }
    // Last-called indicator
    apiFetch('/api/activity/vendors/' + card.id + '/last-call').then(function(data) {
        if (!data || !data.last_call) return;
        var lc = data.last_call;
        var calledAt = new Date(lc.called_at);
        var daysAgo = Math.floor((Date.now() - calledAt.getTime()) / 86400000);
        var timeStr = daysAgo === 0 ? 'today' : daysAgo === 1 ? 'yesterday' : daysAgo + ' days ago';
        var msg = data.is_current_user
            ? 'You called ' + timeStr
            : 'Last called ' + timeStr + ' by ' + (lc.user_name || 'unknown');
        var header = document.querySelector('#vendorPopupContent .vp-header');
        if (header) {
            var el = document.createElement('div');
            el.style.cssText = 'font-size:11px;color:var(--muted);margin-top:2px;font-style:italic';
            el.textContent = msg;
            header.appendChild(el);
        }
    }).catch(function(e) { console.warn('last-call lookup:', e); });
}

async function loadVendorEmailMetrics(cardId) {
    const el = document.getElementById('vpEmailMetrics');
    if (!el) return;
    try {
        const m = await apiFetch(`/api/vendors/${cardId}/email-metrics`);
        const avgResp = m.avg_response_hours != null ? (m.avg_response_hours < 24 ? Math.round(m.avg_response_hours) + 'h' : Math.round(m.avg_response_hours / 24) + 'd') : '—';
        el.innerHTML = `<div class="metrics-panel">
            <div style="text-align:center"><div style="font-weight:800;font-size:14px;color:var(--blue)">${m.total_rfqs_sent || 0}</div><div style="color:var(--muted)">RFQs Sent</div></div>
            <div style="text-align:center"><div style="font-weight:800;font-size:14px;color:var(--green)">${m.total_replies || 0}</div><div style="color:var(--muted)">Replies</div></div>
            <div style="text-align:center"><div style="font-weight:800;font-size:14px;color:var(--amber)">${m.total_quotes || 0}</div><div style="color:var(--muted)">Quotes</div></div>
            <div style="text-align:center"><div style="font-weight:800;font-size:14px">${m.response_rate != null ? Math.round(m.response_rate) + '%' : '—'}</div><div style="color:var(--muted)">Response Rate</div></div>
            <div style="text-align:center"><div style="font-weight:800;font-size:14px">${avgResp}</div><div style="color:var(--muted)">Avg Response</div></div>
            <div style="text-align:center"><div style="font-weight:800;font-size:14px;color:var(--purple)">${m.active_rfqs || 0}</div><div style="color:var(--muted)">Active RFQs</div></div>
        </div>`;
    } catch(e) { logCatchError('vendorMetrics', e); el.innerHTML = ''; }
}

// ── Vendor Offer History ─────────────────────────────────────────────────
let _offerHistoryOffset = 0;
let _offerHistoryQuery = '';
const debounceOfferHistorySearch = debounce((cardId) => { _offerHistoryOffset = 0; _offerHistoryQuery = document.getElementById('vpOfferHistorySearch')?.value?.trim() || ''; loadOfferHistory(cardId); }, 300);

function toggleOfferHistory(cardId) {
    const el = document.getElementById('vpOfferHistory');
    if (!el) return;
    const show = el.style.display === 'none';
    el.style.display = show ? '' : 'none';
    if (show) { _offerHistoryOffset = 0; _offerHistoryQuery = ''; loadOfferHistory(cardId); }
}

async function loadOfferHistory(cardId) {
    const el = document.getElementById('vpOfferHistoryList');
    if (!el) return;
    if (_offerHistoryOffset === 0) el.innerHTML = '<p class="vp-muted" style="font-size:11px">Loading...</p>';
    try {
        let url = `/api/vendors/${cardId}/offer-history?offset=${_offerHistoryOffset}&limit=20`;
        if (_offerHistoryQuery) url += '&q=' + encodeURIComponent(_offerHistoryQuery);
        const data = await apiFetch(url);
        const items = data.items || [];
        let html = items.map(o => `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--card2);font-size:11px">
            <span><b class="cust-link" onclick="openMaterialPopup(${o.material_card_id})">${esc(o.mpn)}</b> ${o.manufacturer ? '<span style="color:var(--muted)">'+esc(o.manufacturer)+'</span>' : ''}</span>
            <span style="display:flex;gap:8px;color:var(--text2)">
                ${o.price != null ? '<span>$'+Number(o.price).toFixed(2)+'</span>' : ''}
                ${o.qty ? '<span>×'+o.qty.toLocaleString()+'</span>' : ''}
                <span style="color:var(--muted)">${o.times_seen || 1}× seen</span>
                ${o.last_seen ? '<span style="color:var(--muted)">'+fmtDate(o.last_seen)+'</span>' : ''}
            </span>
        </div>`).join('');
        if (_offerHistoryOffset === 0) el.innerHTML = html || '<p class="vp-muted" style="font-size:11px">No offer history</p>';
        else el.innerHTML += html;
        const more = document.getElementById('vpOfferHistoryMore');
        if (more) more.style.display = items.length >= 20 ? '' : 'none';
        _offerHistoryOffset += items.length;
    } catch { if (_offerHistoryOffset === 0) el.innerHTML = '<p class="vp-muted" style="font-size:11px">Failed to load</p>'; }
}

function loadMoreOfferHistory(cardId) { loadOfferHistory(cardId); }

// ── Vendor Inline Edit / Delete ──────────────────────────────────────────
function editVendorField(cardId, field, el) {
    if (el.querySelector('input')) return;
    const currentVal = el.textContent.trim();
    const input = document.createElement('input');
    input.className = 'req-edit-input';
    input.value = currentVal === '+ Add website' ? '' : currentVal;
    input.style.cssText = 'font-size:inherit;padding:2px 6px;border:1px solid var(--border);border-radius:4px;width:100%;background:var(--white)';
    el.textContent = '';
    el.appendChild(input);
    input.focus();
    input.select();
    const save = async () => {
        const val = input.value.trim();
        if (val === currentVal || (!val && currentVal === '+ Add website')) { openVendorPopup(cardId); return; }
        try {
            await apiFetch(`/api/vendors/${cardId}`, { method: 'PUT', body: { [field]: val } });
            showToast('Vendor updated', 'success');
            openVendorPopup(cardId);
        } catch (e) { showToast('Failed to update vendor', 'error'); openVendorPopup(cardId); }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); openVendorPopup(cardId); }
    });
}

async function deleteVendor(cardId, name) {
    confirmAction('Delete Vendor', 'Delete vendor "' + name + '"? This cannot be undone.', async function() {
        try {
            await apiFetch(`/api/vendors/${cardId}`, { method: 'DELETE' });
            showToast('Vendor deleted', 'success');
            document.getElementById('vendorPopup')?.classList.remove('open');
            if (typeof loadVendorList === 'function') loadVendorList();
        } catch (e) { showToast('Failed to delete vendor: ' + e.message, 'error'); }
    }, {confirmClass: 'btn-danger', confirmLabel: 'Delete'});
}

let vpRating = 0;
function vpSetRating(n) {
    vpRating = n;
    document.querySelectorAll('#vpStarPicker .vp-star').forEach(el => {
        el.textContent = parseInt(el.dataset.n) <= n ? '★' : '☆';
    });
}

async function vpSubmitReview(cardId) {
    if (vpRating === 0) { showToast('Please select a rating', 'error'); return; }
    const comment = document.getElementById('vpComment')?.value?.trim() || '';
    try {
        await apiFetch(`/api/vendors/${cardId}/reviews`, { method: 'POST', body: { rating: vpRating, comment } });
        vpRating = 0; openVendorPopup(cardId);
    } catch (e) { showToast('Failed to submit review', 'error'); }
}

async function vpToggleBlacklist(cardId, blacklisted) {
    const action = blacklisted ? 'blacklist' : 'remove from blacklist';
    confirmAction('Vendor Blacklist', 'Are you sure you want to ' + action + ' this vendor?', async function() {
        try {
            await apiFetch(`/api/vendors/${cardId}/blacklist`, { method: 'POST', body: { blacklisted } });
            openVendorPopup(cardId);
            if (currentReqId && Object.keys(searchResults).length) renderSources();
        } catch (e) { showToast('Failed to update blacklist', 'error'); }
    });
}

// ── Vendor Contacts CRUD ──────────────────────────────────────────────

export async function loadVendorContacts(cardId) {
    const el = document.getElementById('vpContactsList');
    if (!el) return;
    try {
        const contacts = await apiFetch(`/api/vendors/${cardId}/contacts`);
        if (!contacts.length) {
            el.innerHTML = '<p class="vp-muted" style="font-size:11px">No contacts on file</p>';
            return;
        }
        el.innerHTML = contacts.map(c => {
            const srcBadge = `<span class="badge b-src" style="font-size:9px;padding:1px 6px">${esc(c.source || 'manual')}</span>`;
            const confClass = c.confidence >= 80 ? 'badge-green' : c.confidence >= 50 ? 'badge-yellow' : 'badge-gray';
            const confBadge = `<span class="badge ${confClass}" style="font-size:9px;padding:1px 6px">${c.confidence}%</span>`;
            const verBadge = c.is_verified ? '<span class="badge badge-green" style="font-size:9px;padding:1px 6px">Verified</span>' : '';
            // Relationship score badge
            const scoreBadge = c.relationship_score != null
                ? `<span class="contact-score-badge ${c.relationship_score >= 70 ? 'score-green' : c.relationship_score >= 40 ? 'score-yellow' : 'score-gray'}">${Math.round(c.relationship_score)}</span>`
                : '';
            // Activity trend indicator
            const trendIcons = { warming: '↗', stable: '→', cooling: '↘', dormant: '◯' };
            const trendClasses = { warming: 'trend-warming', stable: 'trend-stable', cooling: 'trend-cooling', dormant: 'trend-dormant' };
            const trendBadge = c.activity_trend
                ? `<span class="contact-trend ${trendClasses[c.activity_trend] || 'trend-stable'}" title="${esc(c.activity_trend)}">${trendIcons[c.activity_trend] || '→'}</span>`
                : '';
            // Phone links — both office and mobile as click-to-call with logging
            var vcPhoneLink = c.phone ? phoneLink(c.phone, {vendor_card_id: cardId, origin: 'vendor_popup'}) : '';
            var vcMobileLink = c.phone_mobile ? phoneLink(c.phone_mobile, {vendor_card_id: cardId, origin: 'vendor_popup'}) : '';
            var vcPhoneSep = vcPhoneLink && vcMobileLink ? ' &middot; ' : '';
            return `<div class="si-contact" style="padding:6px 0;border-bottom:1px solid var(--border)">
                <div class="si-contact-info" style="flex:1;min-width:0">
                    <div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center">
                        <span class="si-contact-name">${esc(c.full_name || c.email)}</span>
                        ${scoreBadge} ${trendBadge} ${srcBadge} ${confBadge} ${verBadge}
                    </div>
                    ${c.title ? '<div style="font-size:11px;color:var(--text2)">' + esc(c.title) + '</div>' : ''}
                    <div class="si-contact-meta">
                        ${c.email ? '<a href="mailto:' + escAttr(c.email) + '" onclick="autoLogEmail(\'' + escAttr(c.email) + '\',\'' + escAttr(c.full_name || '') + '\')">' + esc(c.email) + '</a>' : ''}
                        ${c.email && (vcPhoneLink || vcMobileLink) ? ' &middot; ' : ''}
                        ${vcPhoneLink}${vcPhoneSep}${vcMobileLink}
                    </div>
                    ${c.label ? '<div style="font-size:10px;color:var(--muted)">' + esc(c.label) + '</div>' : ''}
                </div>
                <div class="si-contact-actions" style="display:flex;gap:4px;align-items:center;flex-shrink:0">
                    <button class="btn btn-ghost btn-sm" onclick="openContactTimeline(${cardId},${c.id},'${escAttr(c.full_name || c.email)}')" title="Timeline">⏱</button>
                    <button class="btn btn-ghost btn-sm" onclick="openEditVendorContact(${cardId},${c.id})">Edit</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteVendorContact(${cardId},${c.id},'${escAttr(c.full_name || c.email)}')">✕</button>
                </div>
            </div>`;
        }).join('');
        // Load nudges below contacts
        loadContactNudges(cardId);
    } catch(e) { console.error('loadVendorContacts:', e); el.innerHTML = '<p class="vp-muted" style="font-size:11px">Error loading contacts</p>'; }
}

async function openContactTimeline(cardId, contactId, contactName) {
    const html = `<h2>Timeline: ${esc(contactName)}</h2>
        <div id="contactTimelineContent" style="max-height:400px;overflow-y:auto"><p class="vp-muted">Loading...</p></div>
        <div class="mactions"><button class="btn btn-ghost" onclick="closeModal('contactTimelineModal')">Close</button></div>`;
    let modal = document.getElementById('contactTimelineModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'contactTimelineModal';
        modal.className = 'modal-bg';
        modal.onclick = function(e) { if (e.target === modal) closeModal('contactTimelineModal'); };
        modal.innerHTML = '<div class="modal">' + html + '</div>';
        document.body.appendChild(modal);
    } else {
        modal.querySelector('.modal').innerHTML = html;
    }
    openModal('contactTimelineModal');
    try {
        const events = await apiFetch(`/api/vendors/${cardId}/contacts/${contactId}/timeline`);
        const el = document.getElementById('contactTimelineContent');
        if (!events.length) { el.innerHTML = '<p class="vp-muted" style="font-size:11px">No activity recorded</p>'; return; }
        el.innerHTML = '<div class="contact-timeline">' + events.map(e => {
            const icon = { email_received: '📧', email_sent: '📤', rfq_sent: '📋', quote_received: '💰', po_issued: '✅', call_initiated: '📞', call_outbound: '📞', note: '📝' }[e.activity_type] || '📌';
            const dt = e.occurred_at ? new Date(e.occurred_at).toLocaleString() : '';
            return `<div class="timeline-item">
                <span class="timeline-icon">${icon}</span>
                <div class="timeline-content">
                    <div class="timeline-type">${esc(e.activity_type.replace(/_/g, ' '))}${e.auto_logged ? ' <span class="badge badge-gray" style="font-size:8px">auto</span>' : ''}</div>
                    ${e.subject ? '<div class="timeline-subject">' + esc(e.subject) + '</div>' : ''}
                    ${e.notes ? '<div class="timeline-notes">' + esc(e.notes) + '</div>' : ''}
                    <div class="timeline-date">${dt}${e.channel ? ' via ' + esc(e.channel) : ''}</div>
                </div>
            </div>`;
        }).join('') + '</div>';
    } catch(e) { const ctc = document.getElementById('contactTimelineContent'); if (ctc) ctc.innerHTML = '<p class="vp-muted">Error loading timeline</p>'; }
}

async function loadContactNudges(cardId) {
    const container = document.getElementById('vpContactNudges');
    if (!container) return;
    try {
        const nudges = await apiFetch(`/api/vendors/${cardId}/contact-nudges`);
        if (!nudges.length) { container.innerHTML = ''; return; }
        container.innerHTML = nudges.map(n => {
            const typeColor = n.nudge_type === 'dormant' ? 'var(--red)' : 'var(--amber)';
            return `<div class="nudge-banner" style="border-left:3px solid ${typeColor}">
                <div style="display:flex;align-items:center;gap:6px">
                    <span style="font-size:14px">${n.nudge_type === 'dormant' ? '💤' : '📉'}</span>
                    <div>
                        <div style="font-size:11px;font-weight:600;color:var(--text)">${esc(n.contact_name)}</div>
                        <div style="font-size:10px;color:var(--text2)">${esc(n.message)}</div>
                    </div>
                </div>
                <div style="font-size:9px;color:var(--muted);margin-top:2px">${n.days_since_contact}d ago${n.relationship_score != null ? ' · Score: ' + Math.round(n.relationship_score) : ''}</div>
            </div>`;
        }).join('');
    } catch(e) { console.error('loadContactNudges:', e); }
}

function openAddVendorContact(cardId) {
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('vcCardId', 'value', cardId); _s('vcContactId', 'value', '');
    _s('vendorContactModalTitle', 'textContent', 'Add Vendor Contact');
    ['vcFullName','vcTitle','vcEmail','vcPhone','vcLabel'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    _s('vcLabel', 'value', 'Sales');
    openModal('vendorContactModal', 'vcEmail');
}

async function openEditVendorContact(cardId, contactId) {
    try {
        const contacts = await apiFetch('/api/vendors/' + cardId + '/contacts');
        const c = contacts.find(x => x.id === contactId);
        if (!c) { showToast('Contact not found', 'error'); return; }
        const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
        _s('vcCardId', 'value', cardId); _s('vcContactId', 'value', contactId);
        _s('vendorContactModalTitle', 'textContent', 'Edit Vendor Contact');
        _s('vcFullName', 'value', c.full_name || ''); _s('vcTitle', 'value', c.title || '');
        _s('vcEmail', 'value', c.email || ''); _s('vcPhone', 'value', c.phone || '');
        _s('vcLabel', 'value', c.label || '');
        openModal('vendorContactModal', 'vcFullName');
    } catch(e) { logCatchError('openEditVendorContact', e); showToast('Error loading contact', 'error'); }
}

async function saveVendorContact() {
    const _v = id => document.getElementById(id)?.value || '';
    const cardId = _v('vcCardId');
    const contactId = _v('vcContactId');
    const body = {
        full_name: _v('vcFullName').trim() || null,
        title: _v('vcTitle').trim() || null,
        email: _v('vcEmail').trim(),
        phone: _v('vcPhone').trim() || null,
        label: _v('vcLabel').trim() || 'Sales',
    };
    if (!body.email) { showToast('Email is required', 'error'); return; }
    if (window.isValidEmail && !window.isValidEmail(body.email)) { showToast('Invalid email format', 'error'); return; }
    if (body.phone && window.isValidPhone && !window.isValidPhone(body.phone)) { showToast('Invalid phone number', 'error'); return; }
    var btn = document.querySelector('#vendorContactModal .btn-primary');
    await guardBtn(btn, 'Saving…', async () => {
        const url = contactId
            ? `/api/vendors/${cardId}/contacts/${contactId}`
            : `/api/vendors/${cardId}/contacts`;
        await apiFetch(url, { method: contactId ? 'PUT' : 'POST', body });
        closeModal('vendorContactModal');
        showToast(contactId ? 'Contact updated' : 'Contact added', 'success');
        loadVendorContacts(parseInt(cardId));
    });
}

async function deleteVendorContact(cardId, contactId, name) {
    confirmAction('Remove Contact', 'Remove contact "' + name + '"?', async function() {
        try {
            await apiFetch(`/api/vendors/${cardId}/contacts/${contactId}`, { method: 'DELETE' });
            showToast('Contact removed', 'info');
            loadVendorContacts(cardId);
        } catch(e) { showToast('Failed to delete contact', 'error'); }
    }, {confirmClass: 'btn-danger', confirmLabel: 'Remove'});
}

// ── Vendor Activity ──────────────────────────────────────────────────

async function loadVendorActivities(cardId) {
    const el = document.getElementById('vpActivityList-' + cardId);
    if (!el) return;
    try {
        const activities = await apiFetch('/api/vendors/' + cardId + '/activities');
        if (!activities.length) {
            el.innerHTML = '<p class="vp-muted" style="font-size:11px">No activity recorded yet</p>';
            return;
        }
        el.innerHTML = activities.slice(0, 10).map(a => {
            const icons = { email_sent: '&#x1f4e4;', email_received: '&#x1f4e5;', call_outbound: '&#x1f4de;', call_inbound: '&#x1f4f2;', note: '&#x1f4dd;' };
            const icon = icons[a.activity_type] || '&#x1f4cb;';
            const label = (a.activity_type || '').replace(/_/g, ' ');
            const dur = a.duration_seconds ? ' (' + Math.round(a.duration_seconds / 60) + 'm)' : '';
            return `<div class="act-row">
                <span class="act-row-icon">${icon}</span>
                <div class="act-row-body">
                    <span class="act-row-label">${esc(label)}</span>${dur}
                    ${a.contact_name ? ' &mdash; ' + esc(a.contact_name) : ''}
                    ${a.contact_email ? ' <span style="color:var(--muted)">' + esc(a.contact_email) + '</span>' : ''}
                    ${a.subject ? '<div class="act-row-subject">' + esc(a.subject) + '</div>' : ''}
                    ${a.notes ? '<div class="act-row-subject">' + esc(a.notes) + '</div>' : ''}
                </div>
                <span class="act-row-meta">${esc(a.user_name || '')}</span>
                <span class="act-row-meta">${fmtRelative(a.created_at)}</span>
            </div>`;
        }).join('');
    } catch(e) { console.error('loadVendorActivities:', e); el.innerHTML = '<p class="vp-muted" style="font-size:11px">Error loading activities</p>'; }
}

async function loadVendorActivityStatus(cardId) {
    const el = document.getElementById('vpActHealth-' + cardId);
    if (!el) return;
    try {
        const d = await apiFetch('/api/vendors/' + cardId + '/activity-status');
        const colors = { green: 'var(--green)', yellow: 'var(--amber)', red: 'var(--red)', no_activity: 'var(--muted)' };
        const labels = { green: 'Active', yellow: 'At risk', red: 'Stale', no_activity: 'No activity' };
        const daysText = d.days_since_activity != null ? ' (' + d.days_since_activity + 'd)' : '';
        el.innerHTML = `<span class="badge activity-badge" style="background:color-mix(in srgb,${colors[d.status]} 15%,transparent);color:${colors[d.status]}">${labels[d.status]}${daysText}</span>`;
    } catch(e) { logCatchError('vendorActivityStatus', e); }
}


export function autoLogEmail(email, contactName) {
    apiFetch('/api/activities/email', {
        method: 'POST', body: { email: email, contact_name: contactName || null }
    }).catch(function(e) { console.error('autoLogEmail:', e); });
}

function autoLogVendorCall(cardId, phone) {
    apiFetch('/api/vendors/' + cardId + '/activities/call', {
        method: 'POST', body: { phone: phone, direction: 'outbound' }
    }).then(function() {
        loadVendorActivities(parseInt(cardId));
        loadVendorActivityStatus(parseInt(cardId));
    }).catch(function(e) { console.error('autoLogVendorCall:', e); });
}

function placeVendorCall(cardId, vendorName, reqId, phone) {
    if (!phone) {
        showToast('No phone number on file for ' + vendorName, 'error');
        return;
    }
    // Initiate click-to-call via tel: link
    var a = document.createElement('a');
    a.href = 'tel:' + phone.replace(/[^\d+\-() ]/g, '');
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // Silently log the call timestamp
    var body = { phone: phone, direction: 'outbound' };
    if (reqId) body.requisition_id = reqId;
    apiFetch('/api/vendors/' + cardId + '/activities/call', {
        method: 'POST', body: body
    }).then(function() {
        showToast('Call logged', 'success');
        if (reqId) loadActivity();
        else loadVendorActivities(parseInt(cardId));
        loadVendorActivityStatus(parseInt(cardId));
    }).catch(function(e) { console.error('placeVendorCall:', e); });
}

async function saveVendorLogCall() {
    const _v = id => document.getElementById(id)?.value || '';
    const cardId = _v('vlcCardId');
    const durSec = (parseInt(_v('vlcDurMin')) || 0) * 60 + (parseInt(_v('vlcDurSec')) || 0);
    const data = {
        phone: _v('vlcPhone').trim() || null,
        contact_name: _v('vlcContactName').trim() || null,
        direction: _v('vlcDirection'),
        duration_seconds: durSec || null,
        notes: _v('vlcNotes').trim() || null,
    };
    if (window._vlcReqId) data.requisition_id = window._vlcReqId;
    var btn = document.querySelector('#vendorLogCallModal .btn-primary');
    await guardBtn(btn, 'Logging…', async () => {
        await apiFetch('/api/vendors/' + cardId + '/activities/call', { method: 'POST', body: data });
        closeModal('vendorLogCallModal');
        showToast('Call logged', 'success');
        if (currentReqId && _ddTabCache[currentReqId]) delete _ddTabCache[currentReqId].activity;
        if (window._vlcReqId) { loadActivity(); }
        else { loadVendorActivities(parseInt(cardId)); }
        loadVendorActivityStatus(parseInt(cardId));
        window._vlcReqId = null;
    });
}

function openVendorLogNoteModal(cardId, vendorName, reqId) {
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('vlnCardId', 'value', cardId); _s('vlnVendorName', 'textContent', vendorName);
    ['vlnContactName','vlnNotes'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    window._vlnReqId = reqId || null;
    openModal('vendorLogNoteModal', 'vlnNotes');
}

async function saveVendorLogNote() {
    const _v = id => document.getElementById(id)?.value || '';
    const cardId = _v('vlnCardId');
    const notes = _v('vlnNotes').trim();
    if (!notes) { showToast('Note text is required', 'error'); return; }
    const data = {
        contact_name: _v('vlnContactName').trim() || null,
        notes: notes,
    };
    if (window._vlnReqId) data.requisition_id = window._vlnReqId;
    var btn = document.querySelector('#vendorLogNoteModal .btn-primary');
    await guardBtn(btn, 'Saving…', async () => {
        await apiFetch('/api/vendors/' + cardId + '/activities/note', { method: 'POST', body: data });
        closeModal('vendorLogNoteModal');
        showToast('Note added', 'success');
        if (currentReqId && _ddTabCache[currentReqId]) delete _ddTabCache[currentReqId].activity;
        if (window._vlnReqId) { loadActivity(); }
        else { loadVendorActivities(parseInt(cardId)); }
        loadVendorActivityStatus(parseInt(cardId));
        window._vlnReqId = null;
    });
}

// ── Confirmed Quotes ─────────────────────────────────────────────────

function toggleConfirmedQuotes(cardId) {
    const el = document.getElementById('vpConfirmedQuotes');
    if (el.style.display === 'none') {
        el.style.display = '';
        loadConfirmedQuotes(cardId);
    } else {
        el.style.display = 'none';
    }
}

async function loadConfirmedQuotes(cardId) {
    const listEl = document.getElementById('vpConfirmedQuotesList');
    listEl.innerHTML = '<p class="vp-muted" style="font-size:11px">Loading...</p>';
    try {
        const data = await apiFetch(`/api/vendors/${cardId}/confirmed-offers?limit=50`);
        if (!data.items.length) {
            listEl.innerHTML = '<p class="vp-muted" style="font-size:11px;font-style:italic">No confirmed quotes yet</p>';
            return;
        }
        listEl.innerHTML = data.items.map(o => {
            const priceStr = o.unit_price != null ? `${o.currency || '$'}${o.unit_price.toFixed(2)}` : '--';
            const qtyStr = o.qty_available != null ? o.qty_available.toLocaleString() : '--';
            const statusCls = o.status === 'active' ? 'color:var(--green)' : 'color:var(--text2)';
            return `<div class="mp-vh-row">
                <span class="mp-vh-vendor" style="font-weight:600;font-family:'JetBrains Mono',monospace;font-size:11px">${esc(o.mpn)}</span>
                <span class="mp-vh-detail">${esc(o.manufacturer)}</span>
                <span class="mp-vh-detail">Qty: ${qtyStr}</span>
                <span class="mp-vh-detail" style="font-weight:600">${priceStr}</span>
                ${o.lead_time ? `<span class="mp-vh-detail">${esc(o.lead_time)}</span>` : ''}
                ${o.condition ? `<span class="badge b-src" style="font-size:9px;padding:1px 6px">${esc(o.condition)}</span>` : ''}
                <span style="font-size:10px;${statusCls}">${esc(o.status)}</span>
                <span class="mp-vh-detail" style="margin-left:auto">${esc(o.entered_by)} · ${o.created_at ? fmtDate(o.created_at) : '--'}</span>
            </div>`;
        }).join('');
    } catch(e) {
        listEl.innerHTML = '<p class="vp-muted">Error loading quotes</p>';
    }
}

// ── Parts Sightings ─────────────────────────────────────────────────

let _partsSightingsOffset = 0;
const _debouncedPartsSightingsSearch = debounce((cardId) => {
    const q = (document.getElementById('vpPartsSightingsSearch') || {}).value || '';
    loadPartsSightings(cardId, q);
}, 300);
function debouncePartsSightingsSearch(cardId) { _debouncedPartsSightingsSearch(cardId); }

function togglePartsSightings(cardId) {
    const el = document.getElementById('vpPartsSightings');
    if (el.style.display === 'none') {
        el.style.display = '';
        _partsSightingsOffset = 0;
        loadPartsSightings(cardId, '');
    } else {
        el.style.display = 'none';
    }
}

async function loadPartsSightings(cardId, query) {
    _partsSightingsOffset = 0;
    const q = (query || '').trim();
    const listEl = document.getElementById('vpPartsSightingsList');
    const moreEl = document.getElementById('vpPartsSightingsMore');
    listEl.innerHTML = '<p class="vp-muted" style="font-size:11px">Loading...</p>';

    try {
        const data = await apiFetch(`/api/vendors/${cardId}/parts-summary?q=${encodeURIComponent(q)}&limit=50`);
        if (!data.items.length) {
            listEl.innerHTML = '<p class="vp-muted" style="font-size:11px;font-style:italic">No part sightings found</p>';
            moreEl.style.display = 'none';
            return;
        }
        listEl.innerHTML = renderPartsSightingItems(data.items);
        _partsSightingsOffset = data.items.length;
        moreEl.style.display = data.items.length < data.total ? '' : 'none';
    } catch(e) {
        listEl.innerHTML = '<p class="vp-muted">Error loading sightings</p>';
    }
}

async function loadMorePartsSightings(cardId) {
    const q = (document.getElementById('vpPartsSightingsSearch') || {}).value || '';
    const listEl = document.getElementById('vpPartsSightingsList');
    const moreEl = document.getElementById('vpPartsSightingsMore');

    try {
        const data = await apiFetch(`/api/vendors/${cardId}/parts-summary?q=${encodeURIComponent(q)}&limit=50&offset=${_partsSightingsOffset}`);
        listEl.innerHTML += renderPartsSightingItems(data.items);
        _partsSightingsOffset += data.items.length;
        moreEl.style.display = _partsSightingsOffset < data.total ? '' : 'none';
    } catch(e) { logCatchError('partsSightings', e); }
}

function renderPartsSightingItems(items) {
    return items.map(i => {
        const priceStr = i.last_price != null ? '$' + i.last_price.toFixed(2) : '--';
        const qtyStr = i.last_qty != null ? i.last_qty.toLocaleString() : '--';
        const dateRange = i.first_seen && i.last_seen && i.first_seen !== i.last_seen
            ? `${fmtDate(i.first_seen)} — ${fmtDate(i.last_seen)}`
            : (i.last_seen ? fmtDate(i.last_seen) : '--');
        return `<div class="mp-vh-row">
            <span class="mp-vh-vendor" style="font-weight:600;font-family:'JetBrains Mono',monospace;font-size:11px">${esc(i.mpn)}</span>
            <span class="mp-vh-detail">${esc(i.manufacturer)}</span>
            <span class="mp-vh-detail">Qty: ${qtyStr}</span>
            <span class="mp-vh-detail">Price: ${priceStr}</span>
            <span class="mp-vh-times" title="Times seen">${i.sighting_count}x</span>
            <span class="mp-vh-detail">${dateRange}</span>
        </div>`;
    }).join('');
}

// ── Unified Vendor Enrichment ───────────────────────────────────────

async function unifiedEnrichVendor(cardId) {
    showToast('Enriching vendor — this may take a moment…', 'info');
    try {
        const res = await apiFetch(`/api/enrichment/vendor/${cardId}`, {
            method: 'POST',
            body: { force: true },
        });
        if (res.status === 'completed') {
            const n = (res.enriched_fields || []).length;
            showToast(`Enrichment complete — ${n} field${n !== 1 ? 's' : ''} updated`, 'success');
        } else {
            showToast('Enrichment: ' + (res.status || 'done'));
        }
        openVendorPopup(cardId);
    } catch (e) {
        showToast('Enrichment failed — ' + friendlyError(e, 'please try again'), 'error');
    }
}

// ── Vendors Tab ────────────────────────────────────────────────────────

let _vendorAbort = null;
async function loadVendorList() {
    if (_vendorAbort) { try { _vendorAbort.abort(); } catch(e){} }
    _vendorAbort = new AbortController();
    const q = (document.getElementById('vendorSearch') || {}).value || '';
    var vl = document.getElementById('vendorList');
    if (vl && !_vendorListData.length) vl.innerHTML = window.skeletonRows(5);
    let resp;
    try { resp = await apiFetch(`/api/vendors?q=${encodeURIComponent(q)}`, {signal: _vendorAbort.signal}); }
    catch (e) { if (e.name === 'AbortError') return; logCatchError('loadVendorList', e); showToast('Failed to load vendors', 'error'); return; }
    _vendorListData = resp.vendors || resp;
    filterVendorList();
}

function vendorTier(c) {
    const score = typeof c === 'object' ? c.vendor_score : c;
    const isNew = typeof c === 'object' ? c.is_new_vendor : false;
    if (isNew || score == null) return 'new';
    if (score >= 66) return 'proven';
    if (score >= 33) return 'developing';
    return 'caution';
}

function setVendorTier(tier, btn) {
    _vendorTierFilter = tier;
    document.querySelectorAll('#vendorTierPills .chip').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    filterVendorList();
}

let _selectedVendorId = null;
let _vendorSortCol = null;
let _vendorSortDir = 'asc';

function filterVendorList() {
    const q = (document.getElementById('vendorSearch') || {}).value || '';
    let filtered = [..._vendorListData];
    if (q) {
        const lq = q.toLowerCase();
        filtered = filtered.filter(c => (c.display_name || '').toLowerCase().includes(lq));
    }
    if (_vendorTierFilter !== 'all') {
        filtered = filtered.filter(c => vendorTier(c) === _vendorTierFilter);
    }

    // Sort
    if (_vendorSortCol) {
        filtered.sort((a, b) => {
            let va, vb;
            switch (_vendorSortCol) {
                case 'name': va = (a.display_name || ''); vb = (b.display_name || ''); break;
                case 'score': va = (a.vendor_score ?? -1); vb = (b.vendor_score ?? -1); break;
                case 'response': va = (a.response_rate ?? 0); vb = (b.response_rate ?? 0); break;
                case 'pos': va = (a.total_pos ?? 0); vb = (b.total_pos ?? 0); break;
                case 'parts': va = (a.sighting_count ?? 0); vb = (b.sighting_count ?? 0); break;
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _vendorSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _vendorSortDir === 'asc' ? va - vb : vb - va;
        });
    } else {
        filtered.sort((a, b) => (b.vendor_score ?? -1) - (a.vendor_score ?? -1));
    }

    const countEl = document.getElementById('vendorFilterCount');
    if (countEl) countEl.textContent = filtered.length + ' vendors';

    const el = document.getElementById('vendorList');
    if (!filtered.length) {
        el.innerHTML = _vendorListData.length ? stateEmpty('No vendors match filters', 'Try adjusting your search or filter criteria') : stateEmpty('No vendors yet', 'They\'ll appear automatically after your first search');
        return;
    }

    // Build table
    const thSort = (col, label, extra = '') => {
        const active = _vendorSortCol === col;
        const arrow = active ? (_vendorSortDir === 'asc' ? ' ▲' : ' ▼') : '';
        return `<th class="${active ? 'sorted' : ''}" onclick="sortVendorList('${col}')" ${extra}>${label}<span class="sort-arrow">${arrow}</span></th>`;
    };

    let html = `<table class="crm-table">
        <thead><tr>
            ${thSort('name', 'Vendor')}
            <th>Tier</th>
            ${thSort('score', 'Score')}
            ${thSort('response', 'Response Rate')}
            ${thSort('pos', 'POs Sent')}
            ${thSort('parts', 'Parts Seen')}
            <th>Last Activity</th>
        </tr></thead><tbody>`;

    for (const c of filtered) {
        const score = c.vendor_score != null ? Math.round(c.vendor_score) : null;
        const tier = vendorTier(c);
        const responseRate = c.response_rate != null ? Math.round(c.response_rate) + '%' : '—';
        const lastAgo = c.last_sighting_at ? getRelativeTime(c.last_sighting_at) : '—';
        const activeRow = _selectedVendorId === c.id ? ' active-row' : '';

        html += `<tr class="${activeRow}" onclick="openVendorDrawer(${c.id})" data-vendor-id="${c.id}">
            <td>
                <span style="font-weight:600;color:var(--text)">${esc(c.display_name)}</span>
                ${c.is_blacklisted ? ' <span style="color:var(--red);font-size:10px;font-weight:600">BLOCKED</span>' : ''}
            </td>
            <td><span class="tier-badge tier-badge-${tier}">${tier}</span></td>
            <td>${score != null ? '<span style="font-weight:600;font-family:\'JetBrains Mono\',monospace">' + score + '</span>' : '<span style="color:var(--muted);font-size:10px">New</span>'}</td>
            <td>${responseRate}</td>
            <td>${c.total_pos || 0}</td>
            <td>${c.sighting_count || 0}</td>
            <td class="muted-cell">${lastAgo}</td>
        </tr>`;
    }
    html += '</tbody></table>';

    // Mobile: render cards instead of table
    if (window.__isMobile) {
        let mHtml = '';
        for (const c of filtered) {
            mHtml += _renderVendorCardMobile(c);
        }
        el.innerHTML = mHtml || '<p class="m-empty">No vendors match filters</p>';
    } else {
        el.innerHTML = html;
    }
}

function _renderVendorCardMobile(c) {
    const score = c.vendor_score != null ? Math.round(c.vendor_score) : 0;
    const tier = vendorTier(c);
    const responseRate = c.response_rate != null ? Math.round(c.response_rate) + '%' : '—';
    const parts = c.sighting_count || 0;
    const tierColors = {proven:'m-chip-green',developing:'m-chip-blue',caution:'m-chip-amber','new':'m-chip'};

    return `<div class="m-card" onclick="openVendorDrawer(${c.id})">
        <div class="m-card-header">
            <span class="m-card-title">${esc(c.display_name)}${c.is_blacklisted ? ' <span style="color:var(--red);font-size:10px">BLOCKED</span>' : ''}</span>
            <span class="m-chip ${tierColors[tier] || 'm-chip'}">${tier}</span>
        </div>
        <div class="m-card-body">
            <span style="font-size:12px">Score: <b>${score}</b></span>
            <span style="font-size:12px">Response: <b>${responseRate}</b></span>
            <span style="font-size:12px"><b>${parts}</b> parts</span>
        </div>
        <div class="m-card-footer">
            <span class="m-card-meta">${c.last_sighting_at ? getRelativeTime(c.last_sighting_at) : '—'}</span>
            <span class="m-card-chevron">›</span>
        </div>
    </div>`;
}

function sortVendorList(col) {
    if (_vendorSortCol === col) {
        if (_vendorSortDir === 'asc') _vendorSortDir = 'desc';
        else { _vendorSortCol = null; _vendorSortDir = 'asc'; }
    } else {
        _vendorSortCol = col;
        _vendorSortDir = col === 'name' ? 'asc' : 'desc';
    }
    filterVendorList();
}

function selectVendor(vendorId) { openVendorDrawer(vendorId); }

function openVendorDrawer(vendorId) {
    _selectedVendorId = vendorId;
    document.querySelectorAll('#vendorList tbody tr').forEach(r => {
        r.classList.toggle('active-row', Number(r.dataset.vendorId) === vendorId);
    });
    const backdrop = document.getElementById('vendorDrawerBackdrop');
    const drawer = document.getElementById('vendorDrawer');
    if (backdrop) backdrop.classList.add('open');
    if (drawer) drawer.classList.add('open');
    const v = _vendorListData.find(x => x.id === vendorId);
    if (window._setTopDrillLabel) window._setTopDrillLabel(v?.display_name || 'Vendor');
    _renderVendorDrawerOverview(vendorId);
    document.querySelectorAll('#vendorDrawerTabs .drawer-tab').forEach((t, i) => t.classList.toggle('active', i === 0));
}

function closeVendorDrawer() {
    _selectedVendorId = null;
    const backdrop = document.getElementById('vendorDrawerBackdrop');
    const drawer = document.getElementById('vendorDrawer');
    if (backdrop) backdrop.classList.remove('open');
    if (drawer) drawer.classList.remove('open');
    document.querySelectorAll('#vendorList tbody tr').forEach(r => r.classList.remove('active-row'));
    if (window._setTopViewLabel) window._setTopViewLabel('Vendors');
}

function switchVendorDrawerTab(tab, btn) {
    document.querySelectorAll('#vendorDrawerTabs .drawer-tab').forEach(t => t.classList.remove('active'));
    if (btn) btn.classList.add('active');
    if (!_selectedVendorId) return;
    if (tab === 'overview') _renderVendorDrawerOverview(_selectedVendorId);
    else if (tab === 'contacts') _renderVendorDrawerContacts(_selectedVendorId);
    else if (tab === 'scorecard') _renderVendorDrawerScorecard(_selectedVendorId);
    else if (tab === 'parts') _renderVendorDrawerParts(_selectedVendorId);
    else if (tab === 'comms') _renderVendorDrawerComms(_selectedVendorId);
}

function _renderVendorDrawerOverview(vendorId) {
    const body = document.getElementById('vendorDrawerBody');
    const title = document.getElementById('vendorDrawerTitle');
    if (!body) return;
    const v = _vendorListData.find(x => x.id === vendorId);
    if (!v) { body.innerHTML = '<p class="crm-empty">Vendor not found</p>'; return; }

    if (title) title.textContent = v.display_name;
    const mVTitle = document.getElementById('vendorDrawerMobileTitle');
    if (mVTitle) mVTitle.textContent = v.display_name;
    const score = v.vendor_score != null ? Math.round(v.vendor_score) : null;
    const tier = vendorTier(v);

    let html = `<div class="drawer-section">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
            <div style="display:flex;align-items:center;gap:8px">
                <span class="tier-badge tier-badge-${tier}">${tier}</span>
                ${score != null ? '<span style="font-size:14px;font-weight:700;font-family:\'JetBrains Mono\',monospace">' + score + '</span><span style="font-size:12px;color:var(--muted)">score</span>' : '<span style="font-size:12px;color:var(--muted)">New vendor — no score yet</span>'}
            </div>
            <div style="display:flex;gap:6px">
                <button class="btn btn-ghost btn-sm" onclick="openVendorPopup(${v.id})">Full Details</button>
            </div>
        </div>
        <div class="drawer-field"><span class="drawer-field-label">Response Rate</span><span class="drawer-field-value">${v.response_rate != null ? Math.round(v.response_rate) + '%' : '—'}</span></div>
        <div class="drawer-field"><span class="drawer-field-label">Parts Tracked</span><span class="drawer-field-value">${v.sighting_count || 0}</span></div>
        <div class="drawer-field"><span class="drawer-field-label">Last Activity</span><span class="drawer-field-value">${v.last_sighting_at ? getRelativeTime(v.last_sighting_at) : 'Never'}</span></div>
        ${v.email ? '<div class="drawer-field"><span class="drawer-field-label">Email</span><span class="drawer-field-value"><a href="mailto:'+escAttr(v.email)+'">'+esc(v.email)+'</a></span></div>' : ''}
        ${v.phone ? '<div class="drawer-field"><span class="drawer-field-label">Phone</span><span class="drawer-field-value">'+phoneLink(v.phone, {vendor_card_id: v.id, origin: 'vendor_drawer'})+'</span></div>' : ''}
        ${v.website ? '<div class="drawer-field"><span class="drawer-field-label">Website</span><span class="drawer-field-value"><a href="'+escAttr(v.website)+'" target="_blank">'+esc(v.website)+'</a></span></div>' : ''}
    </div>`;

    body.innerHTML = html;

    // Async: fetch strategic vendor status badge
    apiFetch('/api/strategic-vendors/status/' + vendorId).then(function(st) {
        if (!st) return;
        var section = body.querySelector('.drawer-section');
        if (!section) return;
        var badge = document.createElement('div');
        badge.style.cssText = 'margin-bottom:12px;padding:8px 12px;border-radius:6px;font-size:12px;display:flex;align-items:center;justify-content:space-between';
        if (st.status === 'open_pool') {
            badge.style.background = 'var(--card)';
            badge.style.border = '1px solid var(--line)';
            badge.innerHTML = '<span style="color:var(--muted)">Open Pool</span>' +
                '<button class="btn btn-primary btn-sm" onclick="claimStrategicVendor(' + vendorId + ',\'' + esc(v.display_name).replace(/'/g, "\\'") + '\')">Claim as Strategic</button>';
        } else if (st.owner_user_id === (window.userId || 0)) {
            badge.style.background = 'rgba(59,130,246,0.08)';
            badge.style.border = '1px solid rgba(59,130,246,0.2)';
            badge.innerHTML = '<span><strong>Strategic (You)</strong> — ' + st.days_remaining + ' days left</span>' +
                '<button class="btn btn-ghost btn-sm" onclick="dropStrategicVendor(' + vendorId + ',\'' + esc(v.display_name).replace(/'/g, "\\'") + '\')">Drop</button>';
        } else {
            badge.style.background = 'rgba(234,179,8,0.08)';
            badge.style.border = '1px solid rgba(234,179,8,0.2)';
            badge.innerHTML = '<span>Strategic (' + esc(st.owner_name || 'Another buyer') + ')</span>';
        }
        section.insertBefore(badge, section.firstChild);
    }).catch(function() {});

    // Async: fetch last-call indicator
    apiFetch('/api/activity/vendors/' + vendorId + '/last-call').then(function(data) {
        if (!data || !data.last_call) return;
        var lc = data.last_call;
        var calledAt = new Date(lc.called_at);
        var daysAgo = Math.floor((Date.now() - calledAt.getTime()) / 86400000);
        var timeStr = daysAgo === 0 ? 'today' : daysAgo === 1 ? 'yesterday' : daysAgo + ' days ago';
        var msg = data.is_current_user
            ? 'You called ' + timeStr
            : 'Last called ' + timeStr + ' by ' + (lc.user_name || 'unknown');
        var el = document.createElement('div');
        el.className = 'last-called-indicator';
        el.style.cssText = 'font-size:11px;color:var(--muted);margin:4px 20px 0;font-style:italic';
        el.textContent = msg;
        var section = body.querySelector('.drawer-section');
        if (section) section.appendChild(el);
    }).catch(function(e) { console.warn('vendor activities:', e); });
}

async function _renderVendorDrawerScorecard(vendorId) {
    const body = document.getElementById('vendorDrawerBody');
    if (!body) return;
    body.innerHTML = '<div class="drawer-section"><p class="empty">Loading scorecard...</p></div>';

    try {
        const v = await apiFetch('/api/vendors/' + vendorId).catch(() => _vendorListData.find(x => x.id === vendorId));
        if (!v) { body.innerHTML = '<p class="crm-empty">Vendor not found</p>'; return; }

        // Compute factor scores (0-100)
        const respVelocity = v.response_velocity_hours != null
            ? Math.max(0, Math.min(100, Math.round(100 - (v.response_velocity_hours / 72) * 100)))
            : null;
        const hasOutreach = (v.total_outreach || 0) > 0;
        const ghostScore = v.ghost_rate != null
            ? Math.round((1 - v.ghost_rate) * 100)
            : (hasOutreach ? 0 : null);
        const pricingScore = v.overall_win_rate != null
            ? Math.round(v.overall_win_rate * 100)
            : null;
        const volumeScore = v.sighting_count
            ? Math.min(100, Math.round((v.sighting_count / 500) * 100))
            : null;
        const cancelRate = v.cancellation_rate || 0;
        const rmaRate = v.rma_rate || 0;
        const hasDeliveryData = v.cancellation_rate != null || v.rma_rate != null || (v.offer_count || 0) >= 5;
        const deliveryScore = hasDeliveryData ? Math.round((1 - Math.min(1, cancelRate + rmaRate)) * 100) : null;

        const factors = [
            { label: 'Response Velocity', score: hasOutreach ? respVelocity : 0, detail: hasOutreach ? (v.response_velocity_hours != null ? Math.round(v.response_velocity_hours) + 'h avg' : 'No data') : 'No RFQ history' },
            { label: 'Ghost Rate', score: hasOutreach ? ghostScore : 0, detail: hasOutreach ? (v.ghost_rate != null ? Math.round(v.ghost_rate * 100) + '% ghost' : 'No data') : 'No RFQ history' },
            { label: 'Pricing Competitiveness', score: pricingScore, detail: v.overall_win_rate != null ? Math.round(v.overall_win_rate * 100) + '% win rate' : 'No data' },
            { label: 'Volume Consistency', score: volumeScore, detail: (v.sighting_count || 0) + ' sightings' },
            { label: 'Delivery Reliability', score: hasOutreach ? deliveryScore : 0, detail: hasOutreach ? (hasDeliveryData ? 'Cancel ' + Math.round(cancelRate * 100) + '% / RMA ' + Math.round(rmaRate * 100) + '%' : 'No data') : 'No RFQ history' },
        ];

        let html = '<div class="drawer-section">';
        html += '<div class="drawer-section-title" style="margin-bottom:12px">Engagement Scorecard</div>';

        for (const f of factors) {
            const score = f.score != null ? f.score : 0;
            const barColor = score >= 70 ? 'var(--green)' : score >= 40 ? 'var(--amber)' : 'var(--red)';
            const hasData = f.score != null;
            html += `<div style="margin-bottom:14px">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                    <span style="font-size:12px;font-weight:600">${f.label}</span>
                    <span style="font-size:11px;color:var(--muted)">${f.detail}</span>
                </div>
                <div class="factor-bar">
                    <div class="factor-bar-fill" style="width:${hasData ? score : 0}%;background:${hasData ? barColor : 'var(--muted)'}"></div>
                </div>
                ${hasData ? '<div style="text-align:right;font-size:10px;font-weight:700;color:' + barColor + '">' + score + '/100</div>' : '<div style="text-align:right;font-size:10px;color:var(--muted)">' + (f.detail === 'No RFQ history' ? '0/100' : 'No data') + '</div>'}
            </div>`;
        }

        // Overall score
        const overall = v.vendor_score != null ? Math.round(v.vendor_score) : null;
        html += `<div style="border-top:1px solid var(--border);padding-top:12px;margin-top:8px;display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:13px;font-weight:700">Overall Score</span>
            ${overall != null ? '<span style="font-size:20px;font-weight:700;font-family:\'JetBrains Mono\',monospace;color:' + (overall >= 70 ? 'var(--green)' : overall >= 40 ? 'var(--amber)' : 'var(--red)') + '">' + overall + '</span>' : '<span style="font-size:14px;color:var(--muted)">New</span>'}
        </div>`;
        html += '</div>';
        body.innerHTML = html;
    } catch (err) {
        body.innerHTML = '<p class="crm-empty">Failed to load scorecard</p>';
    }
}

async function _renderVendorDrawerContacts(vendorId) {
    const body = document.getElementById('vendorDrawerBody');
    if (!body) return;
    body.innerHTML = '<div class="drawer-section"><p class="empty">Loading contacts...</p></div>';
    try {
        const contacts = await apiFetch('/api/vendors/' + vendorId + '/contacts');
        if (!contacts.length) {
            body.innerHTML = `<div class="drawer-section"><p class="crm-empty">No contacts — <a href="#" onclick="event.preventDefault();openAddVendorContact(${vendorId})">add one</a></p></div>`;
            return;
        }
        let html = '<div style="padding:12px 20px">';
        for (const c of contacts) {
            const isPrimary = c.is_primary || c.contact_type === 'primary';
            const initials = (c.full_name || c.label || '?').split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2);
            html += `<div class="site-contact-row">
                <div class="site-contact-avatar">${initials}</div>
                <div class="site-contact-info">
                    <div class="site-contact-name">${esc(c.full_name || c.label || '—')}${isPrimary ? ' <span style="font-size:9px;color:var(--blue);font-weight:700">PRIMARY</span>' : ''}</div>
                    ${c.title ? '<div class="site-contact-title">' + esc(c.title) + '</div>' : ''}
                </div>
                <div class="site-contact-actions">
                    ${c.email ? '<a href="mailto:'+escAttr(c.email)+'" title="'+escAttr(c.email)+'">✉</a>' : ''}
                    ${c.phone ? '<a href="tel:'+escAttr(toE164(c.phone) || c.phone)+'" class="phone-link" onclick="logCallInitiated(this)" data-phone="'+escAttr(c.phone)+'" data-ctx="'+escAttr(JSON.stringify({vendor_card_id: vendorId, origin: 'vendor_drawer_contacts'}))+'" title="'+escAttr(c.phone)+'">📞</a>' : ''}
                </div>
            </div>`;
        }
        html += '</div>';
        body.innerHTML = html;  // All values escaped via esc()/escAttr()
    } catch (e) { body.innerHTML = '<div class="drawer-section"><p class="crm-empty">Error loading contacts</p></div>'; }
}

async function _renderVendorDrawerComms(vendorId) {
    const body = document.getElementById('vendorDrawerBody');
    if (!body) return;
    body.innerHTML = '<div class="drawer-section"><p class="empty">Loading communications...</p></div>';
    try {
        const emails = await apiFetch('/api/vendors/' + vendorId + '/emails?limit=20');
        if (!emails.length) {
            body.innerHTML = '<div class="drawer-section"><p class="crm-empty">No communications recorded</p></div>';
            return;
        }
        let html = '<div style="padding:12px 20px"><div class="activity-feed">';
        for (const e of emails) {
            const typeClass = e.direction === 'inbound' ? 'activity-icon-email' : 'activity-icon-system';
            html += `<div class="activity-item">
                <div class="activity-icon ${typeClass}">${activityIcon(e.direction === 'inbound' ? 'email_received' : 'email_sent')}</div>
                <div class="activity-content">
                    <div class="activity-title">${esc(e.subject || '(no subject)')}</div>
                    <div class="activity-detail">${e.direction === 'inbound' ? 'Received' : 'Sent'}</div>
                </div>
                <span class="activity-time">${getRelativeTime(e.received_at || e.sent_at)}</span>
            </div>`;
        }
        html += '</div></div>';
        body.innerHTML = html;
    } catch (err) { body.innerHTML = '<div class="drawer-section"><p class="crm-empty">Error loading communications</p></div>'; }
}

async function _renderVendorDrawerParts(vendorId) {
    const body = document.getElementById('vendorDrawerBody');
    if (!body) return;
    body.innerHTML = '<div class="drawer-section"><p class="empty">Loading part history...</p></div>';
    try {
        const data = await apiFetch('/api/vendors/' + vendorId + '/parts-summary?limit=20');
        const parts = data.items || [];
        if (!parts.length) {
            body.innerHTML = '<div class="drawer-section"><p class="crm-empty">No part history</p></div>';
            return;
        }
        let html = `<div style="padding:12px 20px"><table class="crm-table"><thead><tr>
            <th>Part #</th><th>Last Seen</th><th style="text-align:right">Price</th><th style="text-align:right">Sightings</th>
        </tr></thead><tbody>`;
        for (const s of parts) {
            html += `<tr>
                <td class="mono">${esc(s.mpn || '—')}</td>
                <td style="color:var(--muted)">${s.last_seen ? fmtDate(s.last_seen) : '—'}</td>
                <td style="text-align:right">${s.last_price != null ? '$' + Number(s.last_price).toFixed(2) : '—'}</td>
                <td style="text-align:right;color:var(--muted)">${s.sighting_count != null ? Number(s.sighting_count).toLocaleString() : '—'}</td>
            </tr>`;
        }
        html += '</tbody></table></div>';
        body.innerHTML = html;
    } catch (err) {
        body.innerHTML = `<div class="drawer-section"><p class="crm-empty">Error loading parts</p>
            <button class="btn-sm" onclick="_renderVendorDrawerParts(${vendorId})" style="margin-top:8px">Retry</button></div>`;
    }
}


// ── Materials Tab ──────────────────────────────────────────────────────
let _materialListData = [];
let _matSortCol = null;
let _matSortDir = 'asc';

function _matSortArrow(col) {
    if (_matSortCol !== col) return '\u21c5';
    return _matSortDir === 'asc' ? '\u25b2' : '\u25bc';
}

function sortMatList(col) {
    if (_matSortCol === col) {
        if (_matSortDir === 'asc') _matSortDir = 'desc';
        else { _matSortCol = null; _matSortDir = 'asc'; }
    } else {
        _matSortCol = col;
        _matSortDir = 'asc';
    }
    renderMaterialList();
}

let _materialAbort = null;
async function loadMaterialList() {
    if (_materialAbort) { try { _materialAbort.abort(); } catch(e){} }
    _materialAbort = new AbortController();
    const q = (document.getElementById('materialSearch') || {}).value || '';
    var ml = document.getElementById('materialList');
    if (ml && !_materialListData.length) ml.innerHTML = '<div class="spinner-row"><div class="spinner"></div>Loading materials…</div>';
    let resp;
    try { resp = await apiFetch(`/api/materials?q=${encodeURIComponent(q)}`, {signal: _materialAbort.signal}); }
    catch (e) { if (e.name === 'AbortError') return; logCatchError('loadMaterialList', e); showToast('Failed to load materials', 'error'); return; }
    _materialListData = resp.materials || resp;
    renderMaterialList();
}

function renderMaterialList() {
    let data = [..._materialListData];
    const q = (document.getElementById('materialSearch') || {}).value || '';
    const el = document.getElementById('materialList');
    if (!data.length) {
        el.innerHTML = q ? stateEmpty('No materials match your search', 'Try a different part number') : stateEmpty('No material cards yet', 'They\'ll build automatically as you search');
        return;
    }

    if (_matSortCol) {
        data.sort((a, b) => {
            let va, vb;
            switch (_matSortCol) {
                case 'mpn': va = (a.display_mpn || ''); vb = (b.display_mpn || ''); break;
                case 'mfr': va = (a.manufacturer || ''); vb = (b.manufacturer || ''); break;
                case 'vendors': va = a.vendor_count || 0; vb = b.vendor_count || 0; break;
                case 'price': va = a.best_price ?? 999999; vb = b.best_price ?? 999999; break;
                case 'offers': va = a.offer_count || 0; vb = b.offer_count || 0; break;
                case 'searches': va = a.search_count || 0; vb = b.search_count || 0; break;
                case 'last': va = a.last_searched_at || ''; vb = b.last_searched_at || ''; break;
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _matSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _matSortDir === 'asc' ? va - vb : vb - va;
        });
    } else {
        data.sort((a, b) => new Date(b.last_searched_at || 0) - new Date(a.last_searched_at || 0));
    }

    const thC = (col) => _matSortCol === col ? ' class="sorted"' : '';
    const sa = (col) => `<span class="sort-arrow">${_matSortArrow(col)}</span>`;

    let html = `<div style="padding:0 16px"><table class="tbl"><thead><tr>
        <th onclick="sortMatList('mpn')"${thC('mpn')}>MPN ${sa('mpn')}</th>
        <th onclick="sortMatList('mfr')"${thC('mfr')}>Manufacturer ${sa('mfr')}</th>
        <th onclick="sortMatList('vendors')"${thC('vendors')}>Vendors ${sa('vendors')}</th>
        <th onclick="sortMatList('price')"${thC('price')}>Best Price ${sa('price')}</th>
        <th onclick="sortMatList('offers')"${thC('offers')}>Offers ${sa('offers')}</th>
        <th onclick="sortMatList('searches')"${thC('searches')}>Searches ${sa('searches')}</th>
        <th onclick="sortMatList('last')"${thC('last')}>Last Searched ${sa('last')}</th>
    </tr></thead><tbody>`;

    for (const c of data) {
        const bestPrice = c.best_price != null ? `$${Number(c.best_price).toFixed(2)}` : '\u2014';
        const matDays = daysSince(c.last_searched_at);
        const matColor = recencyColor(matDays, [30, 90]);
        html += `<tr onclick="openMaterialPopup(${c.id})">
            <td><b class="cust-link">${esc(c.display_mpn)}</b></td>
            <td>${esc(c.manufacturer || '\u2014')}</td>
            <td class="mono">${c.vendor_count || 0}</td>
            <td class="mono" style="color:${c.best_price != null ? 'var(--green)' : 'var(--muted)'};font-weight:600">${bestPrice}</td>
            <td class="mono">${c.offer_count || 0}</td>
            <td class="mono">${c.search_count || 0}</td>
            <td style="font-size:11px;color:var(--muted)">${healthDot(matColor, matDays < 900 ? matDays + 'd ago' : '')} ${c.last_searched_at ? fmtDate(c.last_searched_at) : '\u2014'}</td>
        </tr>`;
    }

    html += '</tbody></table></div>';
    el.innerHTML = html;
}

async function openMaterialPopup(cardId) {
    let card, pricingHistory;
    try { card = await apiFetch(`/api/materials/${cardId}`); }
    catch (e) { logCatchError('openMaterialPopup', e); showToast('Failed to load material', 'error'); return; }

    // Fetch customer quote history for this MPN
    const mpn = card.display_mpn || card.normalized_mpn;
    try { pricingHistory = await apiFetch(`/api/pricing-history/${encodeURIComponent(mpn)}`); }
    catch { pricingHistory = { history: [] }; }

    // Compute hub stats
    const offers = card.offers || [];
    const sightings = card.sightings || [];
    const allPrices = [...offers, ...sightings].map(r => r.unit_price).filter(p => p != null && p > 0);
    const uniqueVendors = new Set([...offers, ...sightings].map(r => (r.vendor_name || '').toLowerCase()).filter(Boolean));
    const priceMin = allPrices.length ? Math.min(...allPrices) : null;
    const priceMax = allPrices.length ? Math.max(...allPrices) : null;

    // Object header using shared framework
    let html = renderObjHeader({
        title: card.display_mpn,
        subtitle: card.manufacturer || '',
        meta: `${card.search_count} searches · Last searched ${card.last_searched_at ? fmtDate(card.last_searched_at) : 'never'}`,
        onEdit: `editMaterialField(${card.id},'display_mpn',this)`,
        actions: window.__isAdmin ? `<button class="btn btn-ghost btn-sm" onclick="deleteMaterial(${card.id},'${escAttr(card.display_mpn)}')" style="font-size:9px;color:var(--muted)" title="Admin: permanently delete this material card">Delete</button>` : '',
    });

    // Status strip with key metrics
    html += renderStatusStrip([
        { label: 'Offers', value: offers.length, color: offers.length > 0 ? 'var(--green)' : 'var(--muted)' },
        { label: 'Sightings', value: sightings.length },
        { label: 'Vendors', value: card.vendor_count || uniqueVendors.size },
        { label: 'Price Range', value: priceMin != null ? '$' + priceMin.toFixed(2) + (priceMax !== priceMin ? '\u2013$' + priceMax.toFixed(2) : '') : '\u2014' },
    ]);

    // AI intelligence card — supply health assessment
    const _supplyHealth = offers.length >= 3 ? 'Strong' : offers.length >= 1 ? 'Moderate' : sightings.length > 0 ? 'Weak' : 'Unknown';
    const _supplyConf = offers.length >= 3 ? 0.9 : offers.length >= 1 ? 0.7 : sightings.length > 0 ? 0.4 : 0.1;
    const _supplyInsight = offers.length >= 3
        ? `${offers.length} active offers from ${uniqueVendors.size} vendor${uniqueVendors.size !== 1 ? 's' : ''}. Competitive pricing available.`
        : offers.length >= 1
        ? `${offers.length} offer${offers.length !== 1 ? 's' : ''} available. Consider sending more RFQs for better coverage.`
        : sightings.length > 0
        ? `${sightings.length} sighting${sightings.length !== 1 ? 's' : ''} but no confirmed offers. RFQ outreach recommended.`
        : 'No supply data. Search or add sightings to build intelligence.';
    html += renderAiCard({
        title: 'Supply Intelligence',
        confidence: _supplyConf,
        body: `<b>${_supplyHealth}</b> \u2014 ${_supplyInsight}`,
    });

    html += `<div class="mp-section"><div class="mp-label">Description</div><div onclick="editMaterialField(${card.id},'description',this)" style="font-size:12px;cursor:pointer" title="Click to edit">${card.description ? esc(card.description) : '<span style="color:var(--muted)">+ Add description</span>'}</div></div>`;

    // ── Tags section ──
    const tags = card.tags || [];
    if (tags.length) {
        const brandTags = tags.filter(t => t.type === 'brand');
        const commodityTags = tags.filter(t => t.type === 'commodity');
        html += '<div class="mp-section"><div class="mp-label">Tags</div><div style="display:flex;flex-wrap:wrap;gap:6px">';
        for (const t of brandTags) {
            const confPct = Math.round(t.confidence * 100);
            const confColor = confPct >= 90 ? 'var(--green)' : confPct >= 70 ? 'var(--amber)' : 'var(--muted)';
            html += `<span class="badge b-auth" style="font-size:11px" title="${esc(t.source)} (${confPct}% confidence)">${esc(t.name)} <span style="color:${confColor};font-size:9px">${confPct}%</span></span>`;
        }
        for (const t of commodityTags) {
            const confPct = Math.round(t.confidence * 100);
            const confColor = confPct >= 90 ? 'var(--green)' : confPct >= 70 ? 'var(--amber)' : 'var(--muted)';
            html += `<span class="badge b-src" style="font-size:11px" title="${esc(t.source)} (${confPct}% confidence)">${esc(t.name)} <span style="color:${confColor};font-size:9px">${confPct}%</span></span>`;
        }
        html += '</div></div>';
    }

    // ── Offers section ──
    html += `<div class="mp-section"><div class="mp-label">Offers (${offers.length})</div>`;
    if (offers.length) {
        html += '<div class="mp-table-wrap"><table class="mp-tbl"><thead><tr><th>Vendor</th><th>Qty</th><th>Price</th><th>Lead Time</th><th>Condition</th><th>Status</th><th>Date</th></tr></thead><tbody>';
        for (const o of offers) {
            const statusCls = o.status === 'active' ? 'b-auth' : 'b-src';
            html += `<tr>
                <td class="mp-tbl-vendor" title="${escAttr(o.vendor_name)}">${esc(o.vendor_name)}</td>
                <td>${o.qty_available != null ? o.qty_available.toLocaleString() : '—'}</td>
                <td>${o.unit_price != null ? '$' + Number(o.unit_price).toFixed(2) : '—'}</td>
                <td>${esc(o.lead_time || '—')}</td>
                <td>${esc(o.condition || '—')}</td>
                <td><span class="badge ${statusCls}">${esc(o.status || 'active')}</span></td>
                <td class="mp-tbl-date">${o.created_at ? fmtDate(o.created_at) : '—'}</td>
            </tr>`;
        }
        html += '</tbody></table></div>';
    } else {
        html += '<div class="mp-empty">No offers recorded yet</div>';
    }
    html += '</div>';

    // ── Sightings section ──
    html += `<div class="mp-section"><div class="mp-label">Sightings (${sightings.length})</div>`;
    if (sightings.length) {
        html += '<div class="mp-table-wrap"><table class="mp-tbl"><thead><tr><th>Vendor</th><th>Qty</th><th>Price</th><th>Source</th><th>Auth</th><th>Condition</th><th>Date</th></tr></thead><tbody>';
        for (const s of sightings) {
            html += `<tr>
                <td class="mp-tbl-vendor" title="${escAttr(s.vendor_name)}">${esc(s.vendor_name)}</td>
                <td>${s.qty_available != null ? s.qty_available.toLocaleString() : '—'}</td>
                <td>${s.unit_price != null ? '$' + Number(s.unit_price).toFixed(2) : '—'}</td>
                <td>${s.source_type ? `<span class="badge b-src">${esc(s.source_type.toUpperCase())}</span>` : '—'}</td>
                <td>${s.is_authorized ? '<span class="badge b-auth">Auth</span>' : '—'}</td>
                <td>${esc(s.condition || '—')}</td>
                <td class="mp-tbl-date">${s.created_at ? fmtDate(s.created_at) : '—'}</td>
            </tr>`;
        }
        html += '</tbody></table></div>';
    } else {
        html += '<div class="mp-empty">No sightings recorded yet</div>';
    }
    html += '</div>';

    // ── Customer Quote History section ──
    const quoteHist = pricingHistory.history || [];
    html += `<div class="mp-section"><div class="mp-label">Customer Quote History (${quoteHist.length})</div>`;
    if (pricingHistory.avg_price != null) {
        html += `<div style="font-size:11px;color:var(--muted);margin-bottom:6px">Avg sell: $${Number(pricingHistory.avg_price).toFixed(2)}${pricingHistory.avg_margin != null ? ` · Avg margin: ${pricingHistory.avg_margin}%` : ''}${pricingHistory.price_range ? ` · Range: $${Number(pricingHistory.price_range[0]).toFixed(2)}–$${Number(pricingHistory.price_range[1]).toFixed(2)}` : ''}</div>`;
    }
    if (quoteHist.length) {
        html += '<div class="mp-table-wrap"><table class="mp-tbl"><thead><tr><th>Date</th><th>Customer</th><th>Quote #</th><th>Qty</th><th>Cost</th><th>Sell</th><th>Margin</th><th>Result</th></tr></thead><tbody>';
        for (const qh of quoteHist) {
            const resultCls = qh.result === 'won' ? 'b-auth' : qh.result === 'lost' ? 'b-src' : '';
            html += `<tr>
                <td class="mp-tbl-date">${qh.date ? fmtDate(qh.date) : '—'}</td>
                <td>${esc(qh.customer || '—')}</td>
                <td>${esc(qh.quote_number || '—')}</td>
                <td>${qh.qty != null ? Number(qh.qty).toLocaleString() : '—'}</td>
                <td>${qh.cost_price != null ? '$' + Number(qh.cost_price).toFixed(2) : '—'}</td>
                <td>${qh.sell_price != null ? '$' + Number(qh.sell_price).toFixed(2) : '—'}</td>
                <td>${qh.margin_pct != null ? qh.margin_pct + '%' : '—'}</td>
                <td>${qh.result ? `<span class="badge ${resultCls}">${esc(qh.result.toUpperCase())}</span>` : '—'}</td>
            </tr>`;
        }
        html += '</tbody></table></div>';
    } else {
        html += '<div class="mp-empty">No customer quotes found for this part</div>';
    }
    html += '</div>';

    const mpc = document.getElementById('materialPopupContent'); if (mpc) mpc.innerHTML = html;
    openModal('materialPopup');

    // Sourcing history badge after MPN heading
    var mpnVal = card.display_mpn || card.normalized_mpn;
    if (mpnVal && mpc) {
        var mpnHeader = mpc.querySelector('.mp-header');
        if (mpnHeader) {
            var histBadge = document.createElement('div');
            histBadge.style.cssText = 'margin:4px 0;font-size:11px;color:var(--muted)';
            histBadge.textContent = 'Loading sourcing history\u2026';
            mpnHeader.appendChild(histBadge);

            apiFetch('/api/materials/insights?mpn=' + encodeURIComponent(mpnVal)).then(function(data) {
                if (data.insights && data.insights.length) {
                    histBadge.textContent = data.insights[0].content;
                    histBadge.style.color = 'var(--primary)';
                } else {
                    histBadge.textContent = 'No sourcing history';
                }
            }).catch(function() {
                histBadge.textContent = '';
            });
        }
    }
}

async function openVendorPopupByName(vendorName) {
    let resp;
    try { resp = await apiFetch(`/api/vendors?q=${encodeURIComponent(vendorName)}`); }
    catch (e) { logCatchError('openVendorPopupByName', e); showToast('Vendor not found', 'error'); return; }
    const data = resp.vendors || resp;
    if (data.length) {
        const exact = data.find(c => c.display_name.toLowerCase() === vendorName.toLowerCase());
        openVendorPopup(exact ? exact.id : data[0].id);
    }
}

// ── Material Inline Edit / Delete ────────────────────────────────────────
function editMaterialField(cardId, field, el) {
    if (el.querySelector('input,textarea')) return;
    const currentVal = el.textContent.trim();
    const isDesc = field === 'description';
    const inp = document.createElement(isDesc ? 'textarea' : 'input');
    inp.className = 'req-edit-input';
    inp.value = (currentVal === '+ Add manufacturer' || currentVal === '+ Add description') ? '' : currentVal;
    inp.style.cssText = 'font-size:inherit;padding:2px 6px;border:1px solid var(--border);border-radius:4px;width:100%;background:var(--white)';
    if (isDesc) { inp.rows = 2; inp.style.resize = 'vertical'; }
    el.textContent = '';
    el.appendChild(inp);
    inp.focus();
    inp.select();
    const save = async () => {
        const val = inp.value.trim();
        if (val === currentVal) { openMaterialPopup(cardId); return; }
        try {
            await apiFetch(`/api/materials/${cardId}`, { method: 'PUT', body: { [field]: val } });
            showToast('Material updated', 'success');
            openMaterialPopup(cardId);
        } catch (e) { showToast('Failed to update material', 'error'); openMaterialPopup(cardId); }
    };
    inp.addEventListener('blur', save);
    inp.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !isDesc) { e.preventDefault(); inp.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); openMaterialPopup(cardId); }
    });
}

async function deleteMaterial(cardId, mpn) {
    confirmAction('Delete Material', 'Delete material "' + mpn + '"? This cannot be undone.', async function() {
        try {
            await apiFetch(`/api/materials/${cardId}`, { method: 'DELETE' });
            showToast('Material deleted', 'success');
            document.getElementById('materialPopup')?.classList.remove('open');
            if (typeof loadMaterialList === 'function') loadMaterialList();
        } catch (e) { showToast('Failed to delete material: ' + e.message, 'error'); }
    }, {confirmClass: 'btn-danger', confirmLabel: 'Delete'});
}

async function openMaterialPopupByMpn(mpn) {
    try {
        const card = await apiFetch(`/api/materials/by-mpn/${encodeURIComponent(mpn)}`);
        openMaterialPopup(card.id);
    } catch { /* No material card yet */ }
}

// ── Activity ────────────────────────────────────────────────────────────
let activityData = { vendors: [], summary: { sent: 0, replied: 0, opened: 0, awaiting: 0 } };
let actFilterType = 'all';
let actStatFilter = null; // null = all, 'replied', 'opened', 'awaiting'



async function loadActivity() {
    if (!currentReqId) return;
    const reqId = currentReqId;
    try {
        activityData = await apiFetch(`/api/requisitions/${reqId}/activity`);
    } catch {
        // Fallback to old endpoint
        let contacts;
        try { contacts = await apiFetch(`/api/requisitions/${reqId}/contacts`); }
        catch { return; }
        // Convert to vendor-grouped format
        const vmap = {};
        for (const c of contacts) {
            const vk = (c.vendor_name||'').trim().toLowerCase();
            if (!vmap[vk]) vmap[vk] = { vendor_name: c.vendor_name, status: 'awaiting', contact_count: 0, contact_types: [], all_parts: [], contacts: [], responses: [], last_contacted_at: c.created_at, last_contacted_by: c.user_name, last_contact_email: c.vendor_contact };
            vmap[vk].contacts.push(c);
            vmap[vk].contact_count++;
            if (!vmap[vk].contact_types.includes(c.contact_type)) vmap[vk].contact_types.push(c.contact_type);
            for (const p of (c.parts_included || [])) { if (!vmap[vk].all_parts.includes(p)) vmap[vk].all_parts.push(p); }
        }
        activityData = { vendors: Object.values(vmap), summary: { sent: Object.keys(vmap).length, replied: 0, awaiting: Object.keys(vmap).length } };
    }
    if (currentReqId !== reqId) return; // RFQ changed while loading
    renderActivityCards();
}

function renderActivityCards() {
    const el = document.getElementById('activityLog');
    const summaryEl = document.getElementById('actSummary');
    const filterBarEl = document.getElementById('actFilterBar');
    const vendors = activityData.vendors || [];
    const summary = activityData.summary || {};

    if (!vendors.length) {
        el.innerHTML = '<p class="empty">No contacts yet — send an RFQ or make a call</p>';
        summaryEl.style.display = 'none';
        filterBarEl.style.display = 'none';
        return;
    }

    // Show compact summary — only non-zero stats
    const _s = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    _s('actStatSent', summary.sent || 0); _s('actStatReplied', summary.replied || 0);
    _s('actStatOpened', summary.opened || 0); _s('actStatAwaiting', summary.awaiting || 0);
    summaryEl.style.display = 'flex';
    // Hide zero-count stat cards
    summaryEl.querySelectorAll('.act-stat').forEach(function(card) {
        var num = parseInt(card.querySelector('.act-stat-num') ? card.querySelector('.act-stat-num').textContent : '0');
        var statType = card.dataset.actStat;
        card.style.display = (statType === 'all' || num > 0) ? '' : 'none';
    });

    filterBarEl.style.display = 'flex';

    const q = (document.getElementById('actFilter')?.value || '').trim().toUpperCase();
    const sortVal = document.getElementById('actSort')?.value || 'date-desc';

    // Ghost vendor filter — auto-replies, own users, noise entries
    var _NOISE_NAMES = ['microsoft outlook', 'outlook', 'postmaster', 'mailer-daemon', 'noreply', 'no-reply', 'do not reply'];
    var _ownName = (window.userName || '').trim().toLowerCase();

    let filtered = [...vendors].filter(v => {
        const vn = (v.vendor_name || '').trim().toLowerCase();
        if (!vn || vn === 'no seller listed') return false;
        if (_NOISE_NAMES.indexOf(vn) !== -1) return false;
        if (_ownName && vn === _ownName) return false;
        if (!(v.contacts||[]).length && !(v.responses||[]).length && !(v.activities||[]).length) return false;
        return true;
    });

    // Stat filter (from clicking summary cards)
    if (actStatFilter === 'replied') filtered = filtered.filter(v => v.status === 'replied');
    else if (actStatFilter === 'opened') filtered = filtered.filter(v => v.status === 'opened');
    else if (actStatFilter === 'awaiting') filtered = filtered.filter(v => v.status === 'awaiting');
    else if (actStatFilter === 'unavailable') filtered = filtered.filter(v => v.status === 'unavailable');

    // Pill filter (contact type)
    if (actFilterType === 'email') filtered = filtered.filter(v => (v.contact_types||[]).includes('email'));
    if (actFilterType === 'phone') filtered = filtered.filter(v => (v.contact_types||[]).includes('phone'));

    // Text filter
    if (q) filtered = filtered.filter(v =>
        ((v.vendor_name||'') + ' ' + (v.last_contact_email||'') + ' ' + (v.last_contacted_by||'')).toUpperCase().includes(q)
    );

    // Sort
    if (sortVal === 'date-desc') filtered.sort((a,b) => (b.last_contacted_at||'').localeCompare(a.last_contacted_at||''));
    else if (sortVal === 'date-asc') filtered.sort((a,b) => (a.last_contacted_at||'').localeCompare(b.last_contacted_at||''));
    else if (sortVal === 'vendor-asc') filtered.sort((a,b) => (a.vendor_name||'').localeCompare(b.vendor_name||''));
    else if (sortVal === 'vendor-desc') filtered.sort((a,b) => (b.vendor_name||'').localeCompare(a.vendor_name||''));
    else if (sortVal === 'status') filtered.sort((a,b) => (a.status||'').localeCompare(b.status||''));

    const countEl = document.getElementById('actFilterCount');
    if (countEl) countEl.textContent = (q || actStatFilter || actFilterType !== 'all') ? `${filtered.length} of ${vendors.length}` : `${filtered.length} vendors`;

    if (!filtered.length) {
        el.innerHTML = '<p class="empty">No matching activity</p>';
        return;
    }

    el.innerHTML = filtered.map(v => {
        const statusBadge = v.status === 'replied'
            ? '<span class="act-badge-replied">Replied</span>'
            : v.status === 'opened'
            ? '<span class="act-badge-opened">Opened</span>'
            : v.status === 'unavailable'
            ? '<span class="act-badge-unavail">Not Available</span>'
            : v.status === 'quoted'
            ? '<span class="act-badge-replied" style="background:#dcfce7;color:#166534">Quoted</span>'
            : v.status === 'declined'
            ? '<span class="act-badge-unavail">Declined</span>'
            : '<span class="act-badge-awaiting">Awaiting</span>';

        // Follow-up button for stale contacts (buyer only)
        let followUpBtn = '';
        if (isBuyer() && v.status === 'awaiting' && v.contacts && v.contacts.length) {
            const lastContact = v.contacts[v.contacts.length - 1];
            const sentDate = new Date(lastContact.created_at);
            const daysSince = Math.floor((Date.now() - sentDate) / 86400000);
            if (daysSince >= 3) {
                followUpBtn = `<button class="btn btn-warning btn-sm" onclick="sendFollowUp(${lastContact.id}, '${escAttr(v.vendor_name)}')">📬 Follow Up (${daysSince}d)</button>`;
            }
        }

        // Quote section from parsed responses
        let quoteHtml = '';
        if (v.responses && v.responses.length) {
            const lines = [];
            for (const r of v.responses) {
                const pd = r.parsed_data || {};
                const pParts = pd.parts || [];
                for (const pp of pParts) {
                    const priceStr = pp.unit_price != null ? `$${pp.unit_price}` : '';
                    const qtyStr = pp.qty_available != null ? `${pp.qty_available} avail` : '';
                    const ltStr = pp.lead_time || '';
                    const condStr = pp.condition || '';
                    const vals = [priceStr, qtyStr, condStr, ltStr].filter(Boolean).join(' · ');
                    if (vals) lines.push(`<div class="act-card-quote-line"><span class="act-card-quote-mpn">${esc(pp.mpn || '?')}</span> <span class="act-card-quote-val">${vals}</span></div>`);
                }
                if (!pParts.length && pd.sentiment) {
                    lines.push(`<div class="act-card-quote-line"><span class="act-card-quote-val">Sentiment: ${esc(pd.sentiment)}</span></div>`);
                }
            }
            if (lines.length) quoteHtml = `<div class="act-card-quote">${lines.join('')}</div>`;
        }

        // Email body preview — latest reply snippet, click to expand
        let emailPreviewHtml = '';
        if (v.responses && v.responses.length) {
            const latestReply = v.responses[v.responses.length - 1];
            const rawBody = (latestReply.body || '').replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
            if (rawBody) {
                const preview = rawBody.length > 150 ? rawBody.substring(0, 150) + '\u2026' : rawBody;
                emailPreviewHtml = `<div class="act-card-email-preview" onclick="event.stopPropagation();this.classList.toggle('expanded')">
                    <span class="act-card-email-label">Latest reply: </span>
                    <span class="act-card-email-short">${esc(preview)}</span>
                    <span class="act-card-email-full" style="display:none">${esc(rawBody)}</span>
                </div>`;
            }
        }

        const threadBtn = `<button class="btn btn-ghost btn-sm" onclick="viewThread('${escAttr(v.vendor_name)}')">View Thread</button>`;

        // Place Call / Note buttons (only when vendor_card_id is known)
        let logBtns = '';
        if (v.vendor_card_id) {
            var vendorPhones = v.vendor_phones || [];
            if (vendorPhones.length) {
                logBtns = vendorPhones.map(function(ph) {
                    return '<button class="btn btn-ghost btn-sm" onclick="placeVendorCall(' + v.vendor_card_id + ', \'' + escAttr(v.vendor_name) + '\', ' + currentReqId + ', \'' + escAttr(ph) + '\')">📞 ' + esc(ph) + '</button>';
                }).join('');
            }
            logBtns += '<button class="btn btn-ghost btn-sm" onclick="openVendorLogNoteModal(' + v.vendor_card_id + ', \'' + escAttr(v.vendor_name) + '\', ' + currentReqId + ')">📝 Note</button>';
        }

        // Conditional meta — hide To/By when they're empty
        const hasTo = v.last_contact_email && v.last_contact_email !== '—';
        const hasBy = v.last_contacted_by && v.last_contacted_by !== '—';
        let metaHtml = '';
        if (hasTo || hasBy) {
            metaHtml = '<div class="act-card-meta">';
            if (hasTo) metaHtml += '<div class="act-card-meta-item"><span class="act-card-meta-label">To</span> ' + esc(v.last_contact_email) + '</div>';
            if (hasBy) metaHtml += '<div class="act-card-meta-item"><span class="act-card-meta-label">By</span> ' + esc(v.last_contacted_by) + '</div>';
            metaHtml += '</div>';
        }

        return `<div class="act-card">
            <div class="act-card-header">
                <span class="act-card-vendor">${esc(v.vendor_name)}</span>
                ${statusBadge}
                <span class="act-card-date">${fmtRelative(v.last_contacted_at)}</span>
            </div>
            ${metaHtml}
            ${quoteHtml}
            ${emailPreviewHtml}
            <div class="act-card-actions">${followUpBtn}${logBtns}${threadBtn}</div>
        </div>`;
    }).join('');
}

export function fmtRelative(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    const now = new Date();
    const diff = Math.floor((now - d) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff/86400)}d ago`;
    return d.toLocaleDateString();
}

function fmtDollars(n) {
    if (n == null || isNaN(n)) return '';
    if (n >= 1000) return '$' + (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return '$' + Number(n).toFixed(2);
}
function fmtPrice(n) {
    if (n == null || isNaN(n)) return '\u2014';
    const v = Number(n);
    const cents = v % 1;
    if (cents >= 0.005) return '$' + v.toFixed(2);
    return '$' + v.toLocaleString(undefined, {maximumFractionDigits: 0});
}
function fmtLead(s) {
    if (!s) return '\u2014';
    s = s.trim();
    if (/^\d+$/.test(s)) return s + ' days';
    if (/^\d+\s*-\s*\d+$/.test(s)) return s.replace(/\s*/g, '') + ' days';
    if (/days?|wks?|weeks?/i.test(s)) return s;
    return s + ' days';
}

function threadSearchFilter(query) {
    const wrap = document.getElementById('threadEntries');
    if (!wrap) return;
    const entries = wrap.querySelectorAll('[data-searchable]');
    const q = query.toLowerCase().trim();
    let visible = 0;
    entries.forEach(el => {
        if (!q || el.dataset.searchable.includes(q)) {
            el.style.display = '';
            visible++;
        } else {
            el.style.display = 'none';
        }
    });
    const noResults = document.getElementById('threadNoResults');
    if (noResults) noResults.style.display = (q && !visible) ? '' : 'none';
}

async function viewThread(vendorName) {
    // Find contacts + responses for this vendor
    const v = (activityData.vendors || []).find(x => x.vendor_name === vendorName);
    if (!v) return;

    let html = '<div id="threadEntries" style="max-height:60vh;overflow-y:auto">';

    // Combine outbound + inbound + activities into a single chronological timeline
    const timeline = [];
    for (const c of (v.contacts || [])) {
        timeline.push({ type: 'outbound', date: c.created_at, data: c });
    }
    for (const r of (v.responses || [])) {
        timeline.push({ type: 'inbound', date: r.received_at, data: r });
    }
    for (const a of (v.activities || [])) {
        timeline.push({ type: 'activity', date: a.created_at, data: a });
    }
    timeline.sort((a, b) => (a.date || '').localeCompare(b.date || ''));

    for (const entry of timeline) {
        if (entry.type === 'outbound') {
            const c = entry.data;
            const bodyText = (c.body || '').replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
            const searchText = [c.vendor_contact, c.subject, c.user_name, bodyText, ...(c.parts_included||[])].filter(Boolean).join(' ').toLowerCase();
            // Render outbound email body as HTML (it's stored as HTML from the email composer)
            const bodyHtml = c.body
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(26,127,155,.1);line-height:1.5;max-height:300px;overflow-y:auto">${sanitizeRichHtml(c.body)}</div>`
                : '';
            let contactBadge = c.contact_type === 'email' ? '✉ Sent' : '📞 Called';
            let contactStyle = 'background:var(--teal-light);border:1px solid rgba(26,127,155,.15)';
            if (c.status === 'failed') {
                contactBadge = '✉ <span style="color:#ef4444;font-weight:700">FAILED</span>';
                contactStyle = 'background:#fef2f2;border:1px solid #fecaca';
            } else if (c.status === 'ooo') {
                contactBadge = '✉ <span style="color:#f59e0b;font-weight:700">OOO</span>';
                contactStyle = 'background:#fffbeb;border:1px solid #fde68a';
            } else if (c.status === 'bounced') {
                contactBadge = '✉ <span style="color:#f59e0b;font-weight:700">BOUNCED</span>';
                contactStyle = 'background:#fffbeb;border:1px solid #fde68a';
            }
            html += `<div data-searchable="${escAttr(searchText)}" style="margin-bottom:12px;padding:10px 14px;${contactStyle};border-radius:8px">
                <div style="font-size:11px;color:var(--teal);font-weight:600;margin-bottom:4px">
                    ${contactBadge} · ${esc(c.vendor_contact||'')} · ${fmtDateTime(c.created_at)} · by ${esc(c.user_name||'')}
                </div>
                ${c.subject ? `<div style="font-size:12px;font-weight:600;margin-bottom:2px">${esc(c.subject)}</div>` : ''}
                <div style="font-size:11px;color:var(--text2)">${(c.parts_included||[]).map(p => esc(typeof p === 'object' ? (p.mpn || p.part_number || JSON.stringify(p)) : p)).join(', ')}</div>
                ${bodyHtml}
            </div>`;
            if (c.status === 'failed') {
                html += `<div style="margin-top:4px"><button class="btn-sm" style="font-size:11px;color:#ef4444;border-color:#fecaca" onclick="event.stopPropagation();_retryRfq(${c.id})">↻ Retry Send</button>`;
                if (c.error_message) html += `<span style="font-size:10px;color:var(--muted);margin-left:6px">${esc(c.error_message)}</span>`;
                html += `</div>`;
            }
        } else if (entry.type === 'inbound') {
            // Inbound response
            const r = entry.data;
            const pd = r.parsed_data;
            let parsedHtml = '';
            const searchParts = [r.vendor_email, r.subject];

            if (pd && pd.parts && pd.parts.length) {
                const clsColors = {quote_provided:'var(--green)',no_stock:'var(--red)',counter_offer:'var(--amber)',clarification_needed:'#6366f1',ooo_bounce:'var(--muted)',follow_up:'var(--blue)'};
                const clsLabels = {quote_provided:'Quote Provided',no_stock:'No Stock',counter_offer:'Counter Offer',clarification_needed:'Clarification Needed',ooo_bounce:'OOO / Bounce',follow_up:'Follow Up'};
                const cls = pd.overall_classification || '';
                const clsColor = clsColors[cls] || '#6b7280';
                const conf = pd.confidence != null ? `<span style="font-size:10px;color:var(--text2);margin-left:6px">${Math.round(pd.confidence*100)}% confidence</span>` : '';
                parsedHtml += `<div style="margin-bottom:6px"><span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;color:#fff;background:${clsColor}">${esc(clsLabels[cls]||cls)}</span>${conf}</div>`;
                parsedHtml += '<table style="width:100%;font-size:11px;border-collapse:collapse;margin-bottom:4px">';
                parsedHtml += '<tr style="color:var(--text2);border-bottom:1px solid rgba(0,0,0,.08)"><th style="text-align:left;padding:3px 6px;font-weight:600">MPN</th><th style="text-align:left;padding:3px 6px;font-weight:600">Status</th><th style="text-align:right;padding:3px 6px;font-weight:600">Qty</th><th style="text-align:right;padding:3px 6px;font-weight:600">Price</th><th style="text-align:left;padding:3px 6px;font-weight:600">Lead Time</th><th style="text-align:left;padding:3px 6px;font-weight:600">Cond</th></tr>';
                for (const p of pd.parts) {
                    const statusColors = {quoted:'var(--green)',no_stock:'var(--red)',follow_up:'var(--blue)'};
                    const price = p.unit_price != null ? `${p.currency||'$'}${p.unit_price.toFixed(2)}` : '\u2014';
                    parsedHtml += `<tr style="border-bottom:1px solid rgba(0,0,0,.04)">
                        <td style="padding:3px 6px;font-weight:600">${esc(p.mpn||'')}</td>
                        <td style="padding:3px 6px;color:${statusColors[p.status]||'inherit'}">${esc(p.status||'')}</td>
                        <td style="padding:3px 6px;text-align:right">${p.qty_available != null ? p.qty_available.toLocaleString() : '\u2014'}</td>
                        <td style="padding:3px 6px;text-align:right">${price}</td>
                        <td style="padding:3px 6px">${esc(p.lead_time||'\u2014')}</td>
                        <td style="padding:3px 6px">${esc(p.condition||'\u2014')}</td>
                    </tr>`;
                    searchParts.push(p.mpn, p.status, p.lead_time, p.condition);
                }
                parsedHtml += '</table>';
                if (pd.vendor_notes) {
                    parsedHtml += `<div style="font-size:11px;color:var(--text2);font-style:italic">${esc(pd.vendor_notes)}</div>`;
                    searchParts.push(pd.vendor_notes);
                }
                searchParts.push(clsLabels[cls] || cls);
            }

            // Always show the full email body
            const rawBody = (r.body || '').replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
            searchParts.push(rawBody);
            const emailBodyHtml = r.body
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(16,185,129,.1);line-height:1.5;max-height:300px;overflow-y:auto">${sanitizeRichHtml(r.body)}</div>`
                : '';

            const searchText = searchParts.filter(Boolean).join(' ').toLowerCase();
            html += `<div data-searchable="${escAttr(searchText)}" style="margin-bottom:12px;padding:10px 14px;background:var(--green-light);border-radius:8px;border:1px solid rgba(16,185,129,.15)">
                <div style="font-size:11px;color:var(--green);font-weight:600;margin-bottom:4px">
                    Reply from ${esc(r.vendor_email||'')} · ${fmtDateTime(r.received_at)}
                </div>
                ${r.subject ? `<div style="font-size:12px;font-weight:600;margin-bottom:4px">${esc(r.subject)}</div>` : ''}
                ${parsedHtml}
                ${emailBodyHtml}
                ${r.status === 'new' ? `<div style="margin-top:6px;display:flex;gap:6px">
                    <button class="btn-sm" style="font-size:10px;color:var(--green);border-color:var(--green)" onclick="event.stopPropagation();_updateVrStatus(${r.id},'reviewed')">✓ Reviewed</button>
                    <button class="btn-sm" style="font-size:10px;color:var(--red);border-color:var(--red)" onclick="event.stopPropagation();_updateVrStatus(${r.id},'rejected')">✗ Reject</button>
                </div>` : r.status === 'reviewed' ? `<div style="margin-top:4px;font-size:10px;color:var(--green)">✓ Reviewed</div>` : r.status === 'rejected' ? `<div style="margin-top:4px;font-size:10px;color:var(--red)">✗ Rejected</div>` : ''}
            </div>`;
        } else if (entry.type === 'activity') {
            // Manual call or note
            const a = entry.data;
            const isCall = a.activity_type && a.activity_type.startsWith('call_');
            const icon = isCall ? '📞' : '📝';
            const label = isCall ? ('Call (' + (a.activity_type === 'call_inbound' ? 'inbound' : 'outbound') + ')') : 'Note';
            const bgColor = isCall ? 'rgba(245,158,11,.08)' : 'rgba(107,114,128,.08)';
            const borderColor = isCall ? 'rgba(245,158,11,.2)' : 'rgba(107,114,128,.15)';
            const labelColor = isCall ? 'var(--amber)' : 'var(--muted)';
            const durationStr = isCall && a.duration_seconds ? (' · ' + Math.floor(a.duration_seconds/60) + 'm ' + (a.duration_seconds%60) + 's') : '';
            const contactStr = a.contact_name ? (' · ' + esc(a.contact_name)) : '';
            const phoneStr = isCall && a.contact_phone ? (' · ' + phoneLink(a.contact_phone, {vendor_card_id: v.vendor_card_id || null, origin: 'activity_timeline'})) : '';
            const searchText = [a.contact_name, a.contact_phone, a.notes, a.user_name, label].filter(Boolean).join(' ').toLowerCase();
            html += '<div data-searchable="' + escAttr(searchText) + '" style="margin-bottom:12px;padding:10px 14px;background:' + bgColor + ';border-radius:8px;border:1px solid ' + borderColor + '">'
                + '<div style="font-size:11px;color:' + labelColor + ';font-weight:600;margin-bottom:4px">'
                + icon + ' ' + label + contactStr + phoneStr + durationStr + ' · ' + fmtDateTime(a.created_at) + ' · by ' + esc(a.user_name||'')
                + '</div>'
                + (a.notes ? '<div style="font-size:12px;color:var(--text);margin-top:4px;white-space:pre-wrap">' + esc(a.notes) + '</div>' : '')
                + '</div>';
        }
    }

    if (!timeline.length) {
        html += '<p class="empty">No thread data available</p>';
    }

    html += '<p id="threadNoResults" class="empty" style="display:none">No entries match your search</p>';
    html += '</div>';

    // Show in a simple modal
    const modal = document.getElementById('threadModal');
    if (!modal) {
        // Create thread modal dynamically
        const m = document.createElement('div');
        m.id = 'threadModal';
        m.className = 'modal-bg';
        m.innerHTML = `<div class="modal modal-lg"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><h2 id="threadTitle"></h2><button class="btn btn-ghost btn-sm" onclick="closeModal('threadModal')">✕ Close</button></div><input id="threadSearch" type="text" placeholder="Search thread..." oninput="threadSearchFilter(this.value)" style="width:100%;padding:7px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;margin-bottom:12px;outline:none;background:var(--bg2)"><div id="threadContent"></div></div>`;
        m.addEventListener('click', e => { if (e.target === m) closeModal('threadModal'); });
        document.body.appendChild(m);
    }
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('threadTitle', 'textContent', `Thread: ${vendorName}`);
    _s('threadContent', 'innerHTML', html); _s('threadSearch', 'value', '');
    document.getElementById('threadModal')?.classList.add('open');
    document.getElementById('threadSearch')?.focus();
}

async function _retryRfq(contactId) {
    try {
        const r = await apiFetch('/api/contacts/' + contactId + '/retry', {method:'POST'});
        if (r.status === 'sent') {
            showToast('RFQ resent successfully', 'success');
        } else {
            showToast(r.error || 'Retry failed', 'error');
        }
    } catch(e) {
        showToast('Retry failed: ' + e.message, 'error');
    }
}

async function _updateVrStatus(vrId, status) {
    try {
        await apiFetch('/api/vendor-responses/' + vrId + '/status', {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({status}),
        });
        showToast('Response marked ' + status, 'success');
    } catch(e) {
        showToast('Failed: ' + e.message, 'error');
    }
}

async function _resubmitBuyPlan(planId) {
    if (!confirm('Return this buy plan to Draft for re-submission?')) return;
    try {
        const r = await apiFetch('/api/buy-plans-v3/' + planId + '/resubmit', {method:'POST'});
        if (r.status === 'draft') {
            showToast('Buy plan returned to draft', 'success');
            location.reload();
        } else {
            showToast(r.error || 'Resubmit failed', 'error');
        }
    } catch(e) {
        showToast('Resubmit failed: ' + e.message, 'error');
    }
}

function _ensureEmailListModal() {
    if (document.getElementById('emailListModal')) return;
    const m = document.createElement('div');
    m.id = 'emailListModal';
    m.className = 'modal-bg';
    m.innerHTML = `<div class="modal modal-lg"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><h2 id="emailListTitle"></h2><button class="btn btn-ghost btn-sm" onclick="closeModal('emailListModal')">✕ Close</button></div><div id="emailListContent" style="max-height:60vh;overflow-y:auto"></div></div>`;
    m.addEventListener('click', e => { if (e.target === m) closeModal('emailListModal'); });
    document.body.appendChild(m);
}

function openSentEmailsModal() {
    _ensureEmailListModal();
    const vendors = activityData.vendors || [];
    let html = '';
    const allSent = [];
    for (const v of vendors) {
        for (const c of (v.contacts || [])) {
            if (c.contact_type === 'email') allSent.push({ ...c, vendor_name: v.vendor_name });
        }
    }
    allSent.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    if (!allSent.length) {
        html = '<p class="empty">No sent emails</p>';
    } else {
        for (const c of allSent) {
            const bodyHtml = c.body
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(26,127,155,.1);line-height:1.5;max-height:200px;overflow-y:auto">${sanitizeRichHtml(c.body)}</div>`
                : '';
            html += `<div style="margin-bottom:12px;padding:10px 14px;background:var(--teal-light);border-radius:8px;border:1px solid rgba(26,127,155,.15)">
                <div style="font-size:11px;color:var(--teal);font-weight:600;margin-bottom:4px">
                    To: ${esc(c.vendor_contact || '')} (${esc(c.vendor_name || '')}) · ${fmtDateTime(c.created_at)} · by ${esc(c.user_name || '')}
                </div>
                ${c.subject ? `<div style="font-size:12px;font-weight:600;margin-bottom:2px">${esc(c.subject)}</div>` : ''}
                <div style="font-size:11px;color:var(--text2)">${(c.parts_included || []).map(p => esc(typeof p === 'object' ? (p.mpn || p.part_number || JSON.stringify(p)) : p)).join(', ')}</div>
                ${bodyHtml}
            </div>`;
        }
    }
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('emailListTitle', 'textContent', `Sent Emails (${allSent.length})`);
    _s('emailListContent', 'innerHTML', html);
    document.getElementById('emailListModal')?.classList.add('open');
}

function openRepliedEmailsModal() {
    _ensureEmailListModal();
    const vendors = activityData.vendors || [];
    let html = '<p style="font-size:11px;color:var(--muted);margin-bottom:10px">Note: Not all vendors support read receipts — some replies may not appear here.</p>';
    const allReplies = [];
    for (const v of vendors) {
        for (const r of (v.responses || [])) {
            allReplies.push({ ...r, vendor_name: v.vendor_name });
        }
    }
    allReplies.sort((a, b) => (b.received_at || '').localeCompare(a.received_at || ''));
    if (!allReplies.length) {
        html += '<p class="empty">No replies received yet</p>';
    } else {
        for (const r of allReplies) {
            const pd = r.parsed_data || {};
            let parsedHtml = '';
            if (pd.parts && pd.parts.length) {
                const clsColors = {quote_provided:'var(--green)',no_stock:'var(--red)',counter_offer:'var(--amber)',clarification_needed:'#6366f1',ooo_bounce:'var(--muted)',follow_up:'var(--blue)'};
                const clsLabels = {quote_provided:'Quote Provided',no_stock:'No Stock',counter_offer:'Counter Offer',clarification_needed:'Clarification Needed',ooo_bounce:'OOO / Bounce',follow_up:'Follow Up'};
                const cls = pd.overall_classification || '';
                const clsColor = clsColors[cls] || '#6b7280';
                parsedHtml += `<div style="margin-bottom:4px"><span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;color:#fff;background:${clsColor}">${esc(clsLabels[cls]||cls)}</span></div>`;
                parsedHtml += '<table style="width:100%;font-size:11px;border-collapse:collapse;margin-bottom:4px"><tr style="color:var(--text2);border-bottom:1px solid rgba(0,0,0,.08)"><th style="text-align:left;padding:3px 6px;font-weight:600">MPN</th><th style="text-align:right;padding:3px 6px;font-weight:600">Qty</th><th style="text-align:right;padding:3px 6px;font-weight:600">Price</th><th style="text-align:left;padding:3px 6px;font-weight:600">Lead Time</th></tr>';
                for (const p of pd.parts) {
                    const price = p.unit_price != null ? `${p.currency||'$'}${p.unit_price.toFixed(2)}` : '\u2014';
                    parsedHtml += `<tr style="border-bottom:1px solid rgba(0,0,0,.04)"><td style="padding:3px 6px;font-weight:600">${esc(p.mpn||'')}</td><td style="padding:3px 6px;text-align:right">${p.qty_available != null ? p.qty_available.toLocaleString() : '\u2014'}</td><td style="padding:3px 6px;text-align:right">${price}</td><td style="padding:3px 6px">${esc(p.lead_time||'\u2014')}</td></tr>`;
                }
                parsedHtml += '</table>';
            }
            const emailBodyHtml = r.body
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(16,185,129,.1);line-height:1.5;max-height:200px;overflow-y:auto">${sanitizeRichHtml(r.body)}</div>`
                : '';
            html += `<div style="margin-bottom:12px;padding:10px 14px;background:var(--green-light);border-radius:8px;border:1px solid rgba(16,185,129,.15)">
                <div style="font-size:11px;color:var(--green);font-weight:600;margin-bottom:4px">
                    From: ${esc(r.vendor_email || '')} (${esc(r.vendor_name || '')}) · ${fmtDateTime(r.received_at)}
                </div>
                ${r.subject ? `<div style="font-size:12px;font-weight:600;margin-bottom:4px">${esc(r.subject)}</div>` : ''}
                ${parsedHtml}
                ${emailBodyHtml}
            </div>`;
        }
    }
    const _sr = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _sr('emailListTitle', 'textContent', `Replies Received (${allReplies.length})`);
    _sr('emailListContent', 'innerHTML', html);
    document.getElementById('emailListModal')?.classList.add('open');
}

// ── Stock List Import ────────────────────────────────────────────────────
function toggleStockImport() {
    const el = document.getElementById('stockImportArea');
    el.classList.toggle('u-hidden');
}

async function doStockImport() {
    const fileInput = document.getElementById('stockFileInput');
    const vendorInput = document.getElementById('stockVendorName');
    const statusEl = document.getElementById('stockImportStatus');
    const file = fileInput.files[0];
    if (!file) return;

    const vendorName = vendorInput.value.trim();
    if (!vendorName) {
        statusEl.className = 'ustatus err'; statusEl.style.display = 'block';
        statusEl.textContent = 'Please enter a vendor name';
        return;
    }

    statusEl.className = 'ustatus load'; statusEl.textContent = 'Importing...'; statusEl.style.display = 'block';
    const sfr = document.getElementById('stockFileReady'); if (sfr) sfr.classList.add('u-hidden');

    try {
        const form = new FormData();
        form.append('file', file);
        form.append('vendor_name', vendorName);
        const vendorWebsite = document.getElementById('stockVendorWebsite')?.value?.trim();
        if (vendorWebsite) form.append('vendor_website', vendorWebsite);

        const data = await apiFetch('/api/materials/import-stock', {
            method: 'POST', body: form
        });
        statusEl.className = 'ustatus ok';
        statusEl.textContent = `Imported ${data.imported_rows} parts from ${esc(data.vendor_name)} (${data.skipped_rows} rows skipped)`;
        if (typeof loadMaterialList === 'function') loadMaterialList();
        fileInput.value = '';
    } catch(e) {
        statusEl.className = 'ustatus err';
        statusEl.textContent = 'Import failed: ' + e.message;
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  EMAIL THREADS — Requirement + Vendor email viewing
// ═══════════════════════════════════════════════════════════════════════

let _emailThreadsLoaded = null; // reqId of last loaded threads
let _emailThreadsData = [];

async function loadEmailThreads() {
    if (!currentReqId) return;
    const reqId = currentReqId;
    const el = document.getElementById('emailsContent');
    if (!el) return;

    // Avoid reloading if already loaded for this req
    if (_emailThreadsLoaded === reqId && _emailThreadsData.length > 0) return;

    el.innerHTML = '<div class="spinner-row"><div class="spinner"></div> Loading email threads...</div>';

    try {
        // Use cached requirements if available, otherwise fetch
        const reqs = reqData.length ? reqData : await apiFetch(`/api/requisitions/${reqId}/requirements`);
        if (currentReqId !== reqId) return;
        if (!reqs || !reqs.length) {
            el.innerHTML = '<p class="empty">No requirements — add parts first to see related emails</p>';
            return;
        }

        // Fetch threads for all requirements in parallel
        const allThreads = new Map();
        const results = await Promise.allSettled(
            reqs.map(req => apiFetch(`/api/requirements/${req.id}/emails`))
        );
        if (currentReqId !== reqId) return;
        for (const result of results) {
            if (result.status === 'fulfilled') {
                const data = result.value;
                if (data.error) {
                    el.innerHTML = `<p class="empty" style="color:var(--red)">${esc(data.error)}</p>`;
                    return;
                }
                for (const t of (data.threads || [])) {
                    if (!allThreads.has(t.conversation_id)) {
                        allThreads.set(t.conversation_id, t);
                    }
                }
            } else if (result.reason && result.reason.status === 401) {
                el.innerHTML = '<p class="empty" style="color:var(--red)">Could not load emails — M365 connection may need refresh</p>';
                return;
            }
        }

        _emailThreadsData = Array.from(allThreads.values());
        _emailThreadsData.sort((a, b) => (b.last_message_date || '').localeCompare(a.last_message_date || ''));
        _emailThreadsLoaded = currentReqId;

        if (_emailThreadsData.length === 0) {
            el.innerHTML = '<p class="empty">No email threads found for this requirement</p>';
            return;
        }

        renderEmailThreads(el);
    } catch (e) {
        el.innerHTML = '<p class="empty" style="color:var(--red)">Could not load emails — M365 connection may need refresh</p>';
    }
}

function renderEmailThreads(el) {
    el.innerHTML = _emailThreadsData.map(t => {
        const needsBadge = t.needs_response ? '<span class="email-needs-response">Needs Response</span>' : '';
        const matchBadge = t.matched_via ? `<span class="email-match-badge">${esc(t.matched_via)}</span>` : '';
        const participants = (t.participants || []).join(', ');
        return `<div class="card email-thread-card" onclick="toggleThreadMessages('${escAttr(t.conversation_id)}', this)">
            <div class="email-thread-header">
                <div class="email-thread-subject">${esc(t.subject)} ${needsBadge} ${matchBadge}</div>
                <div class="email-thread-meta">
                    <span class="email-thread-count">${t.message_count} msg${t.message_count !== 1 ? 's' : ''}</span>
                    <span class="email-thread-date">${fmtDateTime(t.last_message_date)}</span>
                </div>
            </div>
            <div class="email-thread-participants">${esc(participants)}</div>
            ${t.snippet ? `<div class="email-thread-snippet">${esc(t.snippet)}</div>` : ''}
            <div class="email-thread-messages" id="thread-${CSS.escape(t.conversation_id)}" style="display:none"></div>
        </div>`;
    }).join('');
}

async function toggleThreadMessages(conversationId, cardEl) {
    const msgContainer = document.getElementById('thread-' + CSS.escape(conversationId));
    if (!msgContainer) return;

    if (msgContainer.style.display !== 'none') {
        msgContainer.style.display = 'none';
        return;
    }

    msgContainer.style.display = 'block';
    msgContainer.innerHTML = '<div class="spinner-row"><div class="spinner"></div></div>';

    try {
        const data = await apiFetch(`/api/emails/thread/${encodeURIComponent(conversationId)}`);
        if (data.error) {
            msgContainer.innerHTML = `<p class="empty" style="font-size:11px;color:var(--red)">${esc(data.error)}</p>`;
            return;
        }

        const messages = data.messages || [];
        if (!messages.length) {
            msgContainer.innerHTML = '<p class="empty" style="font-size:11px">No messages found</p>';
            return;
        }

        let html = messages.map(m => {
            const isSent = m.direction === 'sent';
            const cls = isSent ? 'email-msg-sent' : 'email-msg-received';
            const align = isSent ? 'right' : 'left';
            return `<div class="email-msg ${cls}">
                <div class="email-msg-header">
                    <strong>${esc(m.from_name || m.from_email)}</strong>
                    <span class="email-msg-date">${fmtDateTime(m.received_date)}</span>
                </div>
                <div class="email-msg-body">${esc(m.body_preview)}</div>
            </div>`;
        }).join('');

        // Reply button
        const lastMsg = messages[messages.length - 1];
        const replyTo = lastMsg.direction === 'sent' ? (lastMsg.to[0] || '') : lastMsg.from_email;
        const replySubject = lastMsg.subject.startsWith('Re:') ? lastMsg.subject : 'Re: ' + lastMsg.subject;
        html += `<div class="email-reply-area" id="reply-${CSS.escape(conversationId)}" style="display:none">
            <textarea class="email-reply-input" id="replyBody-${CSS.escape(conversationId)}" placeholder="Type your reply..." rows="3"></textarea>
            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:6px">
                <button class="btn btn-ghost btn-sm" onclick="document.getElementById('reply-${CSS.escape(conversationId)}').style.display='none'">Cancel</button>
                <button class="btn btn-primary btn-sm" onclick="sendEmailReply('${escAttr(conversationId)}','${escAttr(replyTo)}','${escAttr(replySubject)}')">Send Reply</button>
            </div>
        </div>`;
        html += `<button class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="document.getElementById('reply-${CSS.escape(conversationId)}').style.display='block'">Reply</button>`;

        msgContainer.innerHTML = html;
    } catch (e) {
        msgContainer.innerHTML = '<p class="empty" style="font-size:11px;color:var(--red)">Failed to load messages</p>';
    }
}

async function sendEmailReply(conversationId, to, subject) {
    const bodyEl = document.getElementById('replyBody-' + CSS.escape(conversationId));
    if (!bodyEl) return;
    const body = bodyEl.value.trim();
    if (!body) { showToast('Please type a reply', 'error'); return; }
    if (sendEmailReply._busy) return; sendEmailReply._busy = true;
    try {
        await apiFetch('/api/emails/reply', {
            method: 'POST',
            body: { conversation_id: conversationId, to: to, subject: subject, body: body }
        });
        showToast('Reply sent', 'success');
        _emailThreadsLoaded = null;
        loadEmailThreads();
    } catch (e) {
        showToast('Failed to send reply: ' + e.message, 'error');
    } finally { sendEmailReply._busy = false; }
}

// ── Vendor Popup Emails ──────────────────────────────────────────────

let _vendorEmailsLoaded = null;

async function toggleVendorEmails(vendorCardId) {
    const el = document.getElementById('vpEmails');
    if (!el) return;

    if (el.style.display !== 'none') {
        el.style.display = 'none';
        return;
    }
    el.style.display = 'block';

    if (_vendorEmailsLoaded === vendorCardId) return;

    el.innerHTML = '<div class="spinner-row"><div class="spinner"></div></div>';

    try {
        const data = await apiFetch(`/api/vendors/${vendorCardId}/emails`);
        _vendorEmailsLoaded = vendorCardId;

        if (data.error) {
            el.innerHTML = `<p class="vp-muted" style="font-size:11px;color:var(--red)">${esc(data.error)}</p>`;
            return;
        }

        const threads = data.threads || [];
        if (!threads.length) {
            el.innerHTML = '<p class="vp-muted" style="font-size:11px">No email threads found</p>';
            return;
        }

        el.innerHTML = threads.slice(0, 20).map(t => {
            const needsBadge = t.needs_response ? '<span class="email-needs-response" style="font-size:9px">Needs Response</span>' : '';
            return `<div class="vp-item" style="padding:6px 0;border-bottom:1px solid var(--border);cursor:pointer" onclick="toggleVpThreadMessages('${escAttr(t.conversation_id)}', this)">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <span style="font-size:12px;font-weight:500">${esc(t.subject)}</span>
                    ${needsBadge}
                </div>
                <div style="font-size:10px;color:var(--muted)">${t.message_count} msgs · ${fmtDate(t.last_message_date)}</div>
                <div class="vp-thread-msgs" style="display:none;margin-top:6px"></div>
            </div>`;
        }).join('');
    } catch (e) {
        el.innerHTML = '<p class="vp-muted" style="font-size:11px;color:var(--red)">Could not load emails</p>';
    }
}

async function toggleVpThreadMessages(conversationId, itemEl) {
    const msgContainer = itemEl.querySelector('.vp-thread-msgs');
    if (!msgContainer) return;

    if (msgContainer.style.display !== 'none') {
        msgContainer.style.display = 'none';
        return;
    }
    msgContainer.style.display = 'block';
    msgContainer.innerHTML = '<div class="spinner-row" style="padding:4px"><div class="spinner"></div></div>';

    try {
        const data = await apiFetch(`/api/emails/thread/${encodeURIComponent(conversationId)}`);
        const messages = data.messages || [];
        if (!messages.length) {
            msgContainer.innerHTML = '<p style="font-size:10px;color:var(--muted)">No messages</p>';
            return;
        }
        msgContainer.innerHTML = messages.map(m => {
            const isSent = m.direction === 'sent';
            return `<div style="padding:4px 8px;margin:3px 0;border-radius:6px;font-size:11px;background:${isSent ? 'var(--teal-bg, rgba(0,200,150,0.08))' : 'var(--surface2, #2a2a2a)'}">
                <div style="display:flex;justify-content:space-between">
                    <strong>${esc(m.from_name || m.from_email)}</strong>
                    <span style="color:var(--muted);font-size:10px">${fmtDateTime(m.received_date)}</span>
                </div>
                <div style="color:var(--text2);margin-top:2px">${esc(m.body_preview)}</div>
            </div>`;
        }).join('');
    } catch (e) {
        msgContainer.innerHTML = '<p style="font-size:10px;color:var(--red)">Failed to load</p>';
    }
}

// [NOTIFICATIONS REMOVED] — backend removed
function toggleNotifications() { /* removed */ }
function loadNotifications() { /* removed */ }
function loadNotificationBadge() { /* removed */ }
function markNotifRead() { /* removed */ }
function markAllNotifsRead() { /* removed */ }


// "/" keyboard shortcut to focus search bar
document.addEventListener('keydown', function(e) {
    // Escape — close topmost modal
    if (e.key === 'Escape') {
        // Check AI panel first
        var aiPanel = document.querySelector('.ai-panel-bg');
        if (aiPanel) { aiPanel.remove(); return; }
        // Close topmost modal from stack
        if (_modalStack.length > 0) {
            var top = _modalStack[_modalStack.length - 1];
            closeModal(top.id);
            return;
        }
        // Fallback: close any open modal
        var openModals = document.querySelectorAll('.modal-bg.open');
        if (openModals.length) {
            openModals[openModals.length - 1].classList.remove('open');
            return;
        }
    }
    // / — focus search
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        e.preventDefault();
        const sb = document.getElementById('mainSearch');
        if (sb) sb.focus();
    }
    // Keyboard shortcuts — only when not in an input/modal
    const _tag = document.activeElement?.tagName;
    const _inInput = _tag === 'INPUT' || _tag === 'TEXTAREA' || _tag === 'SELECT';
    const _inModal = _modalStack.length > 0;
    if (!_inInput && !_inModal) {
        // n — New Requisition
        if (e.key === 'n' && !e.ctrlKey && !e.metaKey && !e.altKey) {
            e.preventDefault(); openNewReqModal(); return;
        }
        // 1/2/3 — Switch tabs: Open / Sourcing / Archive (only when on list view)
        if (e.key === '1' && !e.ctrlKey && !e.metaKey && !e.altKey && _currentViewId === 'view-list') {
            e.preventDefault();
            const btn = document.querySelector('#mainPills .fp[data-view="reqs"]');
            if (btn) setMainView('reqs', btn);
            return;
        }
        if (e.key === '2' && !e.ctrlKey && !e.metaKey && !e.altKey && _currentViewId === 'view-list') {
            e.preventDefault();
            const btn = document.querySelector('#mainPills .fp[data-view="reqs"]');
            if (btn) setMainView('reqs', btn);
            return;
        }
        if (e.key === '3' && !e.ctrlKey && !e.metaKey && !e.altKey && _currentViewId === 'view-list') {
            e.preventDefault();
            const btn = document.querySelector('#mainPills .fp[data-view="archive"]');
            if (btn) setMainView('archive', btn);
            return;
        }
        // ? — Show keyboard shortcuts help
        if (e.key === '?' && !e.ctrlKey && !e.metaKey && !e.altKey) {
            e.preventDefault();
            showToast('Shortcuts: / Search, n New Req, 1 Pipeline, 2 Pipeline, 3 Archive, ? Help', 'info', 5000);
            return;
        }
    }
    // Tab — focus trap inside open modals
    if (e.key === 'Tab' && _modalStack.length > 0) {
        var topModal = document.getElementById(_modalStack[_modalStack.length - 1].id);
        if (!topModal || !topModal.classList.contains('open')) return;
        var focusable = topModal.querySelectorAll('input:not([type=hidden]),select,textarea,button,[tabindex]:not([tabindex="-1"]),a[href]');
        if (focusable.length === 0) return;
        var first = focusable[0], last = focusable[focusable.length - 1];
        if (e.shiftKey) {
            if (document.activeElement === first || !topModal.contains(document.activeElement)) {
                e.preventDefault(); last.focus();
            }
        } else {
            if (document.activeElement === last || !topModal.contains(document.activeElement)) {
                e.preventDefault(); first.focus();
            }
        }
    }
});

// Global handler for unhandled promise rejections
window.addEventListener('unhandledrejection', function(event) {
    console.error('Unhandled promise rejection:', event.reason);
    if (typeof showToast === 'function') {
        showToast('Something went wrong — please try again', 'error');
    }
});

// ── Network offline/online detection ────────────────────────────────
(function() {
    var _offlineBanner = null;
    function showOffline() {
        if (_offlineBanner) return;
        _offlineBanner = document.createElement('div');
        _offlineBanner.id = 'offlineBanner';
        _offlineBanner.setAttribute('role', 'alert');
        _offlineBanner.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#ef4444;color:#fff;text-align:center;padding:8px 16px;font-size:13px;font-weight:600;z-index:10000';
        _offlineBanner.textContent = 'You are offline — changes will not be saved until connection is restored';
        document.body.appendChild(_offlineBanner);
    }
    function hideOffline() {
        if (_offlineBanner) { _offlineBanner.remove(); _offlineBanner = null; }
        showToast('Back online', 'success');
    }
    window.addEventListener('offline', showOffline);
    window.addEventListener('online', hideOffline);
    if (!navigator.onLine) showOffline();
})();

// [TROUBLE CHAT + TICKET DETAIL REMOVED] — backend removed
function openTroubleChat() { /* removed */ }
function closeTroubleChat() { /* removed */ }
function submitTrouble() { /* removed */ }
function openTicketDetail() { /* removed */ }

// [AI DRAFT + COMPARE REMOVED] — backend removed
function aiDraftRfq() { /* removed */ }
function ddAiCompare() { /* removed */ }


// 3. Normalize Parts — canonicalize MPNs for requirements table
async function aiNormalizeParts(btn) {
    const mpns = reqData.map(r => r.primary_mpn).filter(Boolean);
    if (!mpns.length) { showToast('No parts to normalize', 'error'); return; }
    await guardBtn(btn, 'Normalizing…', async () => {
        let data;
        try {
            data = await apiFetch('/api/ai/normalize-parts', { method: 'POST', body: { parts: mpns } });
        } catch(e) {
            showToast('AI normalize failed: ' + e.message, 'error');
            return;
        }
        const changed = (data.parts || []).filter(p => p.original !== p.normalized);
        if (!changed.length) { showToast('All parts already normalized'); return; }
        // Build review modal
        let html = '<div style="max-width:600px">';
        html += '<h3 style="margin:0 0 12px;font-size:16px">Normalize Parts — ' + changed.length + ' change' + (changed.length !== 1 ? 's' : '') + '</h3>';
        html += '<table style="width:100%;font-size:12px;border-collapse:collapse">';
        html += '<thead><tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)">Apply</th><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)">Original</th><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)">Normalized</th><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)">Manufacturer</th><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)">Confidence</th></tr></thead><tbody>';
        changed.forEach((p, i) => {
            const pct = Math.round((p.confidence || 0) * 100);
            html += `<tr><td style="padding:4px 8px"><input type="checkbox" checked data-ai-norm-idx="${i}"></td><td class="mono" style="padding:4px 8px">${esc(p.original)}</td><td class="mono" style="padding:4px 8px;color:var(--teal);font-weight:600">${esc(p.normalized)}</td><td style="padding:4px 8px">${esc(p.manufacturer || '—')}</td><td style="padding:4px 8px">${pct}%</td></tr>`;
        });
        html += '</tbody></table>';
        html += '<div style="margin-top:12px;text-align:right"><button class="btn btn-primary btn-sm" onclick="_applyNormalized()">Apply Selected</button></div>';
        html += '</div>';
        window._aiNormData = changed;
        _showAiModal('AI Normalize Parts', html);
    });
}

async function _applyNormalized() {
    const changed = window._aiNormData || [];
    const checks = document.querySelectorAll('[data-ai-norm-idx]');
    let applied = 0;
    for (const cb of checks) {
        if (!cb.checked) continue;
        const idx = parseInt(cb.dataset.aiNormIdx);
        const p = changed[idx];
        if (!p) continue;
        const req = reqData.find(r => r.primary_mpn === p.original);
        if (!req) continue;
        try {
            await apiFetch(`/api/requirements/${req.id}`, { method: 'PATCH', body: { primary_mpn: p.normalized } });
            req.primary_mpn = p.normalized;
            applied++;
        } catch(e) { /* skip failed */ }
    }
    closeModal('aiModal');
    if (applied) {
        renderRequirementsTable();
        showToast(applied + ' part' + (applied !== 1 ? 's' : '') + ' normalized');
    } else {
        showToast('No changes applied', 'warn');
    }
}

// 4. Re-parse Email — AI parse/re-parse vendor reply
async function aiParseReply(reqId, responseId, vendorName, btn) {
    // Find the response in activity cache
    const actData = _ddTabCache[reqId]?.activity;
    if (!actData) { showToast('Activity data not loaded', 'error'); return; }
    let response = null;
    for (const v of (actData.vendors || [])) {
        for (const r of (v.responses || [])) {
            if (r.id === responseId) { response = r; break; }
        }
        if (response) break;
    }
    if (!response) { showToast('Reply not found', 'error'); return; }
    await guardBtn(btn, 'Parsing…', async () => {
        const payload = {
            email_body: response.body || '',
            email_subject: response.subject || '',
            vendor_name: vendorName || ''
        };
        let data;
        try {
            data = await apiFetch('/api/ai/parse-email', { method: 'POST', body: payload });
        } catch(e) {
            showToast('AI parse failed: ' + e.message, 'error');
            return;
        }
        if (data.parsed && data.quotes && data.quotes.length) {
            // Build parsed_data structure matching what _renderParsedSummary expects
            response.parsed_data = {
                parts: data.quotes.map(q => ({
                    mpn: q.part_number || '',
                    status: q.unit_price != null ? 'quoted' : 'no_stock',
                    unit_price: q.unit_price,
                    qty_available: q.quantity_available,
                    lead_time: q.lead_time_text || (q.lead_time_days ? q.lead_time_days + ' days' : ''),
                    condition: q.condition || '',
                    date_code: q.date_code || '',
                    moq: q.moq,
                    notes: q.notes || '',
                    currency: q.currency || 'USD'
                })),
                vendor_notes: data.vendor_notes || ''
            };
            // Re-render activity tab
            const panel = document.getElementById('d-' + reqId)?.querySelector('.dd-panel');
            if (panel) _renderDdActivity(reqId, actData, panel);
            showToast('Email re-parsed — review results');
        } else {
            showToast('Could not parse any quotes from this email', 'warn');
        }
    });
}

// Shared AI modal helper
function _showAiModal(title, contentHtml) {
    let modal = document.getElementById('aiModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'aiModal';
        modal.className = 'modal-bg';
        modal.onclick = function(e) { if (e.target === modal) closeModal('aiModal'); };
        modal.innerHTML = '<div class="modal" style="max-width:700px"><div id="aiModalContent"></div><div class="mactions"><button type="button" class="btn btn-ghost" onclick="closeModal(\'aiModal\')">Close</button></div></div>';
        document.body.appendChild(modal);
    }
    const amc = document.getElementById('aiModalContent'); if (amc) amc.innerHTML = contentHtml;
    openModal('aiModal');
}

// ═══════════════════════════════════════════════════════════════════════
// CONTEXT PANEL — cross-app right-side panel for AI summary, thread,
// tasks, files, history. Replaces single-purpose Tasks sidebar when a
// specific object (requirement, material, deal) is selected.
// Called by: toggleContextPanel(), switchCtxTab(), view-specific code
// Depends on: apiFetch, showToast, esc, fmtRelative
// ═══════════════════════════════════════════════════════════════════════

let _ctxOpen = false;
let _ctxActiveTab = 'summary';
let _ctxBoundObject = null; // { type: 'requisition'|'material'|'offer', id: number, label: string }
const _ctxTabContent = {}; // { summary: html, thread: html, tasks: html, files: html, history: html }

function toggleContextPanel() {
    const panel = document.getElementById('ctxPanel');
    const toggle = document.getElementById('ctxToggle');
    if (!panel) return;
    _ctxOpen = !_ctxOpen;
    panel.classList.toggle('open', _ctxOpen);
    toggle?.classList.toggle('shifted', _ctxOpen);
    document.body.classList.toggle('ctx-open', _ctxOpen);
    // Hide legacy tasks sidebar when context panel is active
    const tasksSidebar = document.getElementById('myTasksSidebar');
    if (tasksSidebar && _ctxOpen) {
        tasksSidebar.style.display = 'none';
        document.body.classList.remove('tasks-open');
    } else if (tasksSidebar && !_ctxOpen) {
        tasksSidebar.style.display = '';
    }
}

function switchCtxTab(tabName, btn) {
    _ctxActiveTab = tabName;
    const tabs = document.getElementById('ctxTabs');
    if (tabs) tabs.querySelectorAll('.ctx-tab').forEach(t => t.classList.toggle('active', t.dataset.ctxTab === tabName));
    const compose = document.getElementById('ctxCompose');
    if (compose) compose.style.display = (tabName === 'thread') ? '' : 'none';
    _renderCtxTab(tabName);
}

function _renderCtxTab(tabName) {
    const body = document.getElementById('ctxBody');
    if (!body) return;
    if (!_ctxBoundObject) {
        body.innerHTML = '<div class="ctx-empty">Select a requirement, material, or deal to see context here.</div>';
        return;
    }
    // If cached content exists, show it
    if (_ctxTabContent[tabName]) {
        body.innerHTML = _ctxTabContent[tabName];
        return;
    }
    // Otherwise show loading and fetch
    body.innerHTML = '<div class="ctx-empty"><div class="spinner" style="margin:0 auto"></div></div>';
    _loadCtxTab(tabName);
}

async function _loadCtxTab(tabName) {
    const obj = _ctxBoundObject;
    if (!obj) return;
    const body = document.getElementById('ctxBody');
    if (!body) return;
    try {
        if (tabName === 'summary') {
            _ctxTabContent.summary = _buildCtxSummary(obj);
        } else if (tabName === 'thread') {
            const activities = await apiFetch(`/api/activities?entity_type=${obj.type}&entity_id=${obj.id}&limit=30`).catch(() => []);
            _ctxTabContent.thread = _buildCtxThread(Array.isArray(activities) ? activities : (activities?.items || []));
        } else if (tabName === 'tasks') {
            if (obj.type === 'requisition') {
                const tasks = await apiFetch(`/api/requisitions/${obj.id}/tasks`).catch(() => []);
                _ctxTabContent.tasks = _buildCtxTasks(Array.isArray(tasks) ? tasks : []);
            } else {
                _ctxTabContent.tasks = '<div class="ctx-empty">Tasks are available for requirements.</div>';
            }
        } else if (tabName === 'files') {
            if (obj.type === 'requisition') {
                const files = await apiFetch(`/api/requisition-attachments?requisition_id=${obj.id}`).catch(() => []);
                _ctxTabContent.files = _buildCtxFiles(Array.isArray(files) ? files : []);
            } else {
                _ctxTabContent.files = '<div class="ctx-empty">No files attached.</div>';
            }
        } else if (tabName === 'history') {
            const changes = await apiFetch(`/api/changelog?entity_type=${obj.type}&entity_id=${obj.id}&limit=20`).catch(() => []);
            _ctxTabContent.history = _buildCtxHistory(Array.isArray(changes) ? changes : []);
        }
    } catch (e) {
        _ctxTabContent[tabName] = '<div class="ctx-empty">Failed to load content.</div>';
    }
    // Only render if still on the same tab and object
    if (_ctxActiveTab === tabName && _ctxBoundObject === obj) {
        body.innerHTML = _ctxTabContent[tabName] || '<div class="ctx-empty">No content.</div>';
    }
}

function _buildCtxSummary(obj) {
    // Lightweight status overview — no AI summary or suggested steps
    let html = '';
    html += `<div class="ctx-section">
        <div class="ctx-section-title">Overview</div>
        <div id="ctxOverviewBody" style="font-size:12px;color:var(--muted);padding:4px 0">
            <p>${esc(obj.type.charAt(0).toUpperCase() + obj.type.slice(1))} — ${esc(obj.label)}</p>
        </div>
    </div>`;
    // Open questions
    html += `<div class="ctx-section">
        <div class="ctx-section-title">Open Questions</div>
        <div id="ctxQuestions" class="ctx-empty" style="padding:8px 0">No unresolved questions.</div>
    </div>`;
    return html;
}

function _buildCtxThread(activities) {
    if (!activities.length) return '<div class="ctx-empty">No activity yet. Start a conversation.</div>';
    let html = '';
    for (const a of activities) {
        const initials = (a.user_name || 'U').substring(0, 2).toUpperCase();
        const tagMap = { question: 'question', decision: 'decision', blocker: 'blocker', note: 'action' };
        const tag = tagMap[a.activity_type] ? `<span class="thread-item-tag ${tagMap[a.activity_type]}">${a.activity_type}</span>` : '';
        html += `<div class="thread-item">
            <div class="thread-item-header">
                <div class="thread-item-avatar">${esc(initials)}</div>
                <span class="thread-item-name">${esc(a.user_name || 'System')}</span>
                <span class="thread-item-time">${a.created_at ? fmtRelative(a.created_at) : ''}</span>
            </div>
            <div class="thread-item-body">${tag}${esc(a.subject || a.body || a.notes || '')}</div>
        </div>`;
    }
    return html;
}

function _buildCtxTasks(tasks) {
    if (!tasks.length) return '<div class="ctx-empty">No tasks yet.</div>';
    const pending = tasks.filter(t => t.status !== 'done');
    const done = tasks.filter(t => t.status === 'done');
    const sorted = pending.concat(done);
    let html = '<div class="ctx-section"><div class="ctx-section-title">Tasks (' + pending.length + ' open)</div>';
    for (const t of sorted) {
        const isDone = t.status === 'done';
        const priMap = { 1: 'pri-low', 2: 'pri-med', 3: 'pri-high' };
        const pri = priMap[t.priority] || 'pri-low';
        html += `<div class="task-check-item ${pri}${isDone ? ' task-done' : ''}" style="margin-bottom:4px">
            <span class="task-check-title">${isDone ? '&#10003; ' : '&#9675; '}${esc(t.title || '')}</span>
            ${t.due_at ? '<span class="task-check-due">' + _shortDate(t.due_at) + '</span>' : ''}
        </div>`;
    }
    html += '</div>';
    return html;
}

function _buildCtxFiles(files) {
    if (!files.length) return '<div class="ctx-empty">No files attached.</div>';
    let html = '<div class="ctx-section"><div class="ctx-section-title">Files</div>';
    for (const f of files) {
        const size = f.file_size ? (f.file_size > 1048576 ? (f.file_size / 1048576).toFixed(1) + ' MB' : (f.file_size / 1024).toFixed(0) + ' KB') : '';
        html += `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.filename || f.name || 'file')}</span>
            <span style="font-size:10px;color:var(--muted)">${size}</span>
            ${f.url ? `<a href="${esc(f.url)}" target="_blank" class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 6px">Open</a>` : ''}
        </div>`;
    }
    html += '</div>';
    return html;
}

function _buildCtxHistory(changes) {
    if (!changes.length) return '<div class="ctx-empty">No change history recorded.</div>';
    let html = '<div class="ctx-section"><div class="ctx-section-title">Change History</div>';
    for (const c of changes) {
        html += `<div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:11px">
            <div style="display:flex;justify-content:space-between"><span style="font-weight:500">${esc(c.field_name || c.action || 'change')}</span><span style="color:var(--muted)">${c.created_at ? fmtRelative(c.created_at) : ''}</span></div>
            <div style="color:var(--muted);margin-top:2px">${c.old_value ? esc(String(c.old_value).substring(0, 50)) + ' → ' : ''}${c.new_value ? esc(String(c.new_value).substring(0, 50)) : ''}</div>
            ${c.user_name ? `<div style="color:var(--muted);font-size:10px">${esc(c.user_name)}</div>` : ''}
        </div>`;
    }
    html += '</div>';
    return html;
}

// Bind context panel to a specific object (requirement, material, etc.)
function bindContextPanel(type, id, label) {
    // If already bound to the same object, skip reset to preserve tab state
    if (_ctxBoundObject && _ctxBoundObject.type === type && _ctxBoundObject.id === id) {
        return;
    }
    _ctxBoundObject = { type, id, label };
    // Clear cached tab content
    Object.keys(_ctxTabContent).forEach(k => delete _ctxTabContent[k]);
    // Update title
    const title = document.getElementById('ctxTitle');
    if (title) title.textContent = label || (type + ' #' + id);
    // Show toggle button
    const toggle = document.getElementById('ctxToggle');
    if (toggle) toggle.style.display = '';
    // Keep panel closed by default — just show a badge with pending task count
    _updateCtxTaskBadge(type, id);
    // Pre-render current tab content (so it's ready if user opens panel)
    _renderCtxTab(_ctxActiveTab);
}

// Fetch pending task count and update the toggle badge
async function _updateCtxTaskBadge(type, id) {
    if (type !== 'requisition') return;
    try {
        const tasks = await apiFetch(`/api/requisitions/${id}/tasks`).catch(() => []);
        const arr = Array.isArray(tasks) ? tasks : [];
        const pending = arr.filter(t => t.status !== 'done').length;
        const badge = document.getElementById('ctxToggleBadge');
        const taskBadge = document.getElementById('ctxTaskBadge');
        if (badge) {
            badge.textContent = pending;
            badge.style.display = pending > 0 ? '' : 'none';
        }
        if (taskBadge) {
            taskBadge.textContent = pending;
            taskBadge.style.display = pending > 0 ? '' : 'none';
        }
    } catch (e) { /* ignore */ }
}

function unbindContextPanel() {
    _ctxBoundObject = null;
    Object.keys(_ctxTabContent).forEach(k => delete _ctxTabContent[k]);
    const body = document.getElementById('ctxBody');
    if (body) body.innerHTML = '<div class="ctx-empty">Select a requirement, material, or deal to see context here.</div>';
    const title = document.getElementById('ctxTitle');
    if (title) title.textContent = 'Context';
    // Clear badge counts
    const badge = document.getElementById('ctxToggleBadge');
    if (badge) { badge.textContent = '0'; badge.style.display = 'none'; }
    const taskBadge = document.getElementById('ctxTaskBadge');
    if (taskBadge) { taskBadge.textContent = '0'; taskBadge.style.display = 'none'; }
    // Close panel if open
    if (_ctxOpen) toggleContextPanel();
}

// Send message from thread compose
async function ctxSendMessage() {
    const input = document.getElementById('ctxComposeInput');
    if (!input || !input.value.trim() || !_ctxBoundObject) return;
    const msg = input.value.trim();
    input.value = '';
    try {
        await apiFetch('/api/activities', {
            method: 'POST',
            body: { entity_type: _ctxBoundObject.type, entity_id: _ctxBoundObject.id, activity_type: 'note', notes: msg }
        });
        showToast('Note added', 'success');
        // Refresh thread tab
        delete _ctxTabContent.thread;
        if (_ctxActiveTab === 'thread') _renderCtxTab('thread');
    } catch (e) {
        showToast('Failed to add note', 'error');
    }
}

function ctxAttachFile() {
    showToast('File attachment coming soon', 'info');
}


// ═══════════════════════════════════════════════════════════════════════
// UNIVERSAL INTAKE BAR — paste, upload, or API import of parts/offers.
// Provides a single shared entry point for getting data into the system.
// Called by: _intakeInputChange(), _intakePaste(), _intakeUpload(), etc.
// Depends on: apiFetch, showToast, esc, currentReqId
// ═══════════════════════════════════════════════════════════════════════

let _intakeParsedRows = [];
let _intakeTargetType = 'requirement'; // 'requirement' | 'sighting' | 'offer'

function showIntakeBar() {
    const bar = document.getElementById('intakeBar');
    if (bar) bar.style.display = '';
}

function hideIntakeBar() {
    const bar = document.getElementById('intakeBar');
    if (bar) bar.style.display = 'none';
    _intakeClose();
}

function _intakeInputChange(value) {
    // Multi-line paste triggers review
    if (value.includes('\n') || value.includes('\t')) {
        _intakeParseText(value);
    }
}

function _intakePaste(event) {
    const text = (event.clipboardData || window.clipboardData)?.getData('text') || '';
    if (text.includes('\n') || text.includes('\t')) {
        event.preventDefault();
        const input = document.getElementById('intakeInput');
        if (input) input.value = text;
        _intakeParseText(text);
    }
}

function _intakeParseText(text) {
    // Parse tab/newline separated data into rows
    const lines = text.trim().split('\n').filter(l => l.trim());
    if (lines.length === 0) return;
    _intakeParsedRows = [];
    // Detect header row
    const firstLine = lines[0].toLowerCase();
    const hasHeader = firstLine.includes('mpn') || firstLine.includes('part') || firstLine.includes('qty') || firstLine.includes('price');
    const startIdx = hasHeader ? 1 : 0;
    for (let i = startIdx; i < lines.length; i++) {
        const cols = lines[i].split('\t');
        if (cols.length === 0 || !cols[0].trim()) continue;
        const row = {
            mpn: cols[0]?.trim() || '',
            qty: cols[1]?.trim() || '',
            price: cols[2]?.trim() || '',
            manufacturer: cols[3]?.trim() || '',
            confidence: 'high',
            type: _intakeTargetType,
            duplicate: false
        };
        // Simple confidence heuristic
        if (!row.mpn || row.mpn.length < 3) row.confidence = 'low';
        else if (!row.qty && !row.price) row.confidence = 'med';
        _intakeParsedRows.push(row);
    }
    _intakeRenderDrawer();
}

function _intakeRenderDrawer() {
    const drawer = document.getElementById('intakeDrawer');
    const body = document.getElementById('intakeDrawerBody');
    const titleEl = document.getElementById('intakeDrawerTitle');
    if (!drawer || !body) return;
    drawer.classList.add('open');
    if (titleEl) titleEl.textContent = `AI Review — ${_intakeParsedRows.length} items detected`;
    let html = '';
    for (let i = 0; i < _intakeParsedRows.length; i++) {
        const r = _intakeParsedRows[i];
        const confClass = r.confidence === 'high' ? 'high' : r.confidence === 'med' ? 'med' : 'low';
        const confPct = r.confidence === 'high' ? '95' : r.confidence === 'med' ? '65' : '30';
        html += `<div class="intake-row">
            <div class="intake-row-confidence ${confClass}" title="Confidence: ${r.confidence}">${confPct}%</div>
            <div class="intake-row-data">
                <div class="intake-row-mpn">${esc(r.mpn)}</div>
                <div class="intake-row-detail">Qty: ${esc(r.qty || '—')} · Price: ${esc(r.price || '—')} ${r.manufacturer ? '· ' + esc(r.manufacturer) : ''}</div>
            </div>
            <span class="intake-row-tag ${r.type === 'requirement' ? 'req' : r.type === 'sighting' ? 'sighting' : 'offer'}">${r.type}</span>
            ${r.duplicate ? '<span class="intake-row-tag dup">DUP</span>' : ''}
            <div class="intake-row-actions">
                <select style="font-size:10px;padding:1px 4px;border:1px solid var(--border);border-radius:3px" onchange="_intakeChangeType(${i},this.value)">
                    <option value="requirement" ${r.type === 'requirement' ? 'selected' : ''}>Requirement</option>
                    <option value="sighting" ${r.type === 'sighting' ? 'selected' : ''}>Sighting</option>
                    <option value="offer" ${r.type === 'offer' ? 'selected' : ''}>Offer</option>
                </select>
                <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 4px;color:var(--red)" onclick="_intakeRemoveRow(${i})" title="Remove">✕</button>
            </div>
        </div>`;
    }
    body.innerHTML = html;
}

function _intakeChangeType(idx, type) {
    if (_intakeParsedRows[idx]) {
        _intakeParsedRows[idx].type = type;
    }
}

function _intakeRemoveRow(idx) {
    _intakeParsedRows.splice(idx, 1);
    _intakeRenderDrawer();
}

function _intakeClose() {
    const drawer = document.getElementById('intakeDrawer');
    if (drawer) drawer.classList.remove('open');
    _intakeParsedRows = [];
    const input = document.getElementById('intakeInput');
    if (input) input.value = '';
}

const _intakeSubmit = () => _intakeConfirm();
async function _intakeConfirm() {
    if (!_intakeParsedRows.length) return;
    const reqRows = _intakeParsedRows.filter(r => r.type === 'requirement');
    // For requirements, add them to the current requisition
    if (reqRows.length && currentReqId) {
        try {
            for (const r of reqRows) {
                await apiFetch(`/api/requisitions/${currentReqId}/requirements`, {
                    method: 'POST',
                    body: { primary_mpn: r.mpn, target_qty: r.qty || '1', target_price: r.price ? parseFloat(r.price) : null }
                });
            }
            showToast(`${reqRows.length} requirement(s) added`, 'success');
        } catch (e) {
            showToast('Failed to add some requirements', 'error');
        }
    } else if (reqRows.length && !currentReqId) {
        // If on materials view, search for these MPNs instead
        if (_currentMainView === 'materials' || (document.getElementById('view-materials') && !document.getElementById('view-materials').classList.contains('hidden') && !document.getElementById('view-materials').classList.contains('u-hidden'))) {
            const mpns = reqRows.map(r => r.mpn).filter(Boolean);
            if (mpns.length) {
                const searchBox = document.getElementById('materialSearch');
                if (searchBox) { searchBox.value = mpns.join(', '); }
                showToast(`Searching ${mpns.length} part number(s) in materials`, 'info');
                if (typeof loadMaterialList === 'function') loadMaterialList();
            }
        } else {
            showToast('Open a requisition first to add requirements', 'warn');
        }
    }
    // For sightings and offers, log them (placeholder for full implementation)
    const otherRows = _intakeParsedRows.filter(r => r.type !== 'requirement');
    if (otherRows.length) {
        showToast(`${otherRows.length} sighting/offer items logged (intake processing)`, 'info');
    }
    _intakeClose();
    // Refresh requirement list if we added any
    if (reqRows.length && currentReqId && typeof loadRequirements === 'function') {
        loadRequirements();
    }
}

function _intakeUpload() {
    const input = document.getElementById('intakeFileInput');
    if (input) input.click();
}

function _intakeFileSelected(input) {
    if (!input.files || !input.files[0]) return;
    const file = input.files[0];
    const reader = new FileReader();
    reader.onload = function(e) {
        const text = e.target.result;
        _intakeParseText(text);
    };
    // Read as text for CSV/TSV; for Excel, show toast about future support
    if (file.name.match(/\.(xlsx|xls)$/i)) {
        showToast('Excel import: use the existing Import Stock feature for now', 'info');
        input.value = '';
        return;
    }
    reader.readAsText(file);
    input.value = '';
}

function _intakeImportApi() {
    showToast('API import: use Search to pull live availability from supplier APIs', 'info');
}


// ═══════════════════════════════════════════════════════════════════════
// SHARED PAGE HELPERS — reusable HTML builders for object headers,
// status strips, blocker strips, AI cards, and action bars.
// Called by: view-specific rendering functions
// Depends on: esc
// ═══════════════════════════════════════════════════════════════════════

function renderObjHeader(opts) {
    // opts: { icon: svg, title, subtitle, actions: [{label, onclick, cls}] }
    let actHtml = '';
    if (opts.actions) {
        actHtml = opts.actions.map(a =>
            `<button class="btn ${a.cls || 'btn-ghost btn-sm'}" onclick="${a.onclick || ''}" ${a.title ? 'title="' + esc(a.title) + '"' : ''}>${a.label}</button>`
        ).join('');
    }
    return `<div class="obj-header">
        ${opts.icon ? `<div class="obj-header-icon">${opts.icon}</div>` : ''}
        <div class="obj-header-info">
            <div class="obj-header-title">${esc(opts.title || '')}</div>
            ${opts.subtitle ? `<div class="obj-header-subtitle">${opts.subtitle}</div>` : ''}
        </div>
        <div class="obj-header-actions">${actHtml}</div>
    </div>`;
}

function renderStatusStrip(items) {
    // items: [{ value, label, cls: 'alert'|'warn'|'good'|'', color: 'var(--green)' }]
    return `<div class="status-strip">${items.map(i => {
        const colorStyle = i.color ? ' style="color:' + i.color + '"' : '';
        const titleAttr = i.title ? ' title="' + escAttr(i.title) + '"' : '';
        return `<div class="status-strip-item ${i.cls || ''}"${titleAttr}>
            <div class="status-strip-value"${colorStyle}>${i.value}</div>
            <div class="status-strip-label">${esc(i.label)}</div>
        </div>`;
    }).join('')}</div>`;
}

function renderBlockerStrip(blockers, actionLabel, actionOnclick) {
    if (!blockers) return '';
    // Support both array of {text, level} objects and plain string
    if (Array.isArray(blockers)) {
        if (blockers.length === 0) return '';
        return blockers.map(b => {
            const lvl = b.level || 'info';
            const iconColor = lvl === 'error' ? 'var(--red)' : lvl === 'warn' ? 'var(--amber)' : 'var(--muted)';
            return `<div class="blocker-strip" style="--blocker-color:${iconColor}">
                <div class="blocker-strip-icon" style="color:${iconColor}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div>
                <span class="blocker-strip-text">${esc(b.text || '')}</span>
            </div>`;
        }).join('');
    }
    // Legacy: plain string
    return `<div class="blocker-strip">
        <div class="blocker-strip-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div>
        <span class="blocker-strip-text">${esc(blockers)}</span>
        ${actionLabel ? `<button class="blocker-strip-action" onclick="${actionOnclick || ''}">${esc(actionLabel)}</button>` : ''}
    </div>`;
}

function renderAiCard(opts) {
    // opts: { label, body (html), confidence: 0-100, actions: [{label, onclick, primary}] }
    let confHtml = '';
    if (opts.confidence !== undefined) {
        const cls = opts.confidence >= 80 ? 'high' : opts.confidence >= 50 ? 'med' : 'low';
        confHtml = `<span class="ai-card-confidence">
            <span class="ai-card-confidence-bar"><span class="ai-card-confidence-fill ${cls}" style="width:${opts.confidence}%"></span></span>
            ${opts.confidence}%
        </span>`;
    }
    let actHtml = '';
    if (opts.actions) {
        actHtml = `<div class="ai-card-actions">${opts.actions.map(a =>
            `<button class="ai-card-action ${a.primary ? 'primary' : ''}" onclick="${a.onclick || ''}">${esc(a.label)}</button>`
        ).join('')}${confHtml}</div>`;
    }
    return `<div class="ai-card">
        <div class="ai-card-header">
            <svg class="ai-card-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4c0 1.95-1.4 3.58-3.25 3.93"/><path d="M8.56 2.75a4 4 0 0 0-1.09 6.89"/><path d="M12 8v14"/><path d="M5 18a7 7 0 0 1 14 0"/></svg>
            <span class="ai-card-label">${esc(opts.label || 'AI Insight')}</span>
        </div>
        <div class="ai-card-body">${opts.body || ''}</div>
        ${actHtml}
    </div>`;
}


// ══════════════════════════════════════════════════════════════════════
// RFQ WORKSPACE — Part-centric left/right layout
// Left: part list with chip clusters + stepper
// Right: persistent part transaction panel with tabbed views
// ══════════════════════════════════════════════════════════════════════

let _rfqActiveReqId = null;    // Currently expanded requisition
let _rfqActivePartId = null;   // Currently selected part (requirement)
let _rfqPanelTab = 'offers';   // Active right-panel tab
let _rfqPartsData = [];        // Cached parts list for active req
let _rfqPanelCache = {};       // { [tabName]: data }

const _rfqSteps = ['sourced','offers','selected','quoted'];
const _rfqStepLabels = { sourced:'Source', offers:'Offer', selected:'Select', quoted:'Quote' };

function _rfqStepIndex(step) {
    const map = { new:-1, sourced:0, offers:1, selected:2, quoted:3 };
    return map[step] ?? -1;
}

/**
 * Render the RFQ workspace inside the drill-down panel.
 * Called when a requisition is expanded in the new unified view.
 */
async function rfqOpenWorkspace(reqId, container) {
    _rfqActiveReqId = reqId;
    _rfqActivePartId = null;
    _rfqPanelCache = {};

    container.innerHTML = `<div class="rfq-workspace" id="rfqWorkspace-${reqId}">
        <div class="rfq-left" id="rfqLeft-${reqId}">
            <table class="rfq-part-list"><thead><tr>
                <th>MPN</th><th>Qty / Target</th><th>Status</th><th>Progress</th>
            </tr></thead><tbody id="rfqPartBody-${reqId}"></tbody></table>
        </div>
        <div class="rfq-right empty" id="rfqRight-${reqId}">
            <span>Select a part to view details</span>
        </div>
    </div>`;

    // Load parts
    try {
        const parts = await apiFetch(`/api/requisitions/${reqId}/requirements`);
        _rfqPartsData = parts || [];
        _rfqRenderPartList(reqId);
        // Auto-select first part
        if (_rfqPartsData.length > 0) {
            rfqSelectPart(_rfqPartsData[0].id);
        }
    } catch(e) {
        document.getElementById('rfqPartBody-' + reqId).innerHTML =
            '<tr><td colspan="4" style="color:var(--red);padding:12px">Failed to load parts</td></tr>';
    }
}

function _rfqRenderPartList(reqId) {
    const tbody = document.getElementById('rfqPartBody-' + reqId);
    if (!tbody) return;
    if (_rfqPartsData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-placeholder">No parts added yet</td></tr>';
        return;
    }
    tbody.innerHTML = _rfqPartsData.map(p => _rfqPartRow(p)).join('');
}

function _rfqPartRow(p) {
    const active = p.id === _rfqActivePartId ? ' active' : '';
    const oc = p.offer_count || 0;
    const sc = p.selected_count || 0;
    const tc = p.task_count || 0;
    const sightings = p.sighting_count || 0;

    // Chip cluster
    let chips = '';
    if (oc > 0) chips += `<span class="rfq-chip rfq-chip-offer">${oc} Offer${oc>1?'s':''}</span>`;
    if (sc > 0) chips += `<span class="rfq-chip rfq-chip-sel">${sc} Sel</span>`;
    if (tc > 0) chips += `<span class="rfq-chip rfq-chip-task">${tc} Task${tc>1?'s':''}</span>`;
    if (sightings > 0 && oc === 0) chips += `<span class="rfq-chip rfq-chip-hist">${sightings} Source${sightings>1?'s':''}</span>`;
    if (!chips) chips = '<span class="rfq-chip rfq-chip-none">No data</span>';

    // Progress stepper
    const si = _rfqStepIndex(p.step || 'new');
    let stepper = '<div class="rfq-stepper">';
    _rfqSteps.forEach((s, i) => {
        let cls = 'rfq-step';
        if (i < si) cls += ' done';
        else if (i === si) cls += ' current';
        stepper += `<div class="${cls}" title="${_rfqStepLabels[s]}"></div>`;
    });
    const stepLabel = si >= 0 ? _rfqStepLabels[_rfqSteps[si]] : 'New';
    stepper += `<span class="rfq-step-label">${stepLabel}</span></div>`;

    const target = p.target_price ? '$' + Number(p.target_price).toFixed(4) : '\u2014';

    const qtyTarget = target ? `${p.target_qty} @ ${target}` : `${p.target_qty}`;

    return `<tr class="rfq-part-row${active}" onclick="rfqSelectPart(${p.id})" data-part-id="${p.id}">
        <td><div class="rfq-mpn">${esc(p.primary_mpn)}</div>${p.brand ? '<div class="rfq-brand">' + esc(p.brand) + '</div>' : ''}</td>
        <td class="mono" style="font-size:10px;white-space:nowrap;color:var(--text2)">${qtyTarget}</td>
        <td><div class="rfq-chips">${chips}</div></td>
        <td>${stepper}</td>
    </tr>`;
}

async function rfqSelectPart(partId) {
    _rfqActivePartId = partId;
    _rfqPanelCache = {};
    _rfqPanelTab = 'offers';

    // Update active row highlight
    const ws = document.getElementById('rfqWorkspace-' + _rfqActiveReqId);
    if (ws) {
        ws.querySelectorAll('.rfq-part-row').forEach(r => {
            r.classList.toggle('active', Number(r.dataset.partId) === partId);
        });
    }

    const part = _rfqPartsData.find(p => p.id === partId);
    if (!part) return;

    const right = document.getElementById('rfqRight-' + _rfqActiveReqId);
    if (!right) return;
    right.className = 'rfq-right';

    // Build panel header
    const target = part.target_price ? '$' + Number(part.target_price).toFixed(4) : '';
    const stepHtml = _rfqBuildStepper(part.step || 'new');

    let flags = '';
    if (part.offer_count > 0) flags += '<span class="rfq-panel-flag rfq-chip-offer">Offers Available</span>';
    if (part.selected_count > 0) flags += '<span class="rfq-panel-flag rfq-chip-sel">' + part.selected_count + ' Selected</span>';
    if (part.task_count > 0) flags += '<span class="rfq-panel-flag rfq-chip-task">' + part.task_count + ' Open Tasks</span>';

    right.innerHTML = `
        <div class="rfq-panel-header">
            <div class="rfq-panel-mpn">${esc(part.primary_mpn)}</div>
            <div class="rfq-panel-meta">
                ${part.brand ? '<span>' + esc(part.brand) + '</span>' : ''}
                <span>Qty: <b>${part.target_qty}</b></span>
                ${target ? '<span>Target: <b>' + target + '</b></span>' : ''}
                ${stepHtml}
                ${flags ? '<div class="rfq-panel-flags">' + flags + '</div>' : ''}
            </div>
        </div>
        <div class="rfq-panel-tabs">
            <button class="rfq-panel-tab on" data-tab="offers" onclick="rfqSwitchTab('offers')">Offers</button>
            <button class="rfq-panel-tab" data-tab="tasks" onclick="rfqSwitchTab('tasks')">Tasks</button>
            <button class="rfq-panel-tab" data-tab="notes" onclick="rfqSwitchTab('notes')">Notes</button>
            <button class="rfq-panel-tab" data-tab="history" onclick="rfqSwitchTab('history')">History</button>
            <button class="rfq-panel-tab" data-tab="sightings" onclick="rfqSwitchTab('sightings')">Sightings</button>
        </div>
        <div class="rfq-panel-body" id="rfqPanelBody"></div>`;

    // Load default tab
    await _rfqLoadTab('offers');
}

function _rfqBuildStepper(step) {
    const si = _rfqStepIndex(step);
    let html = '<div class="rfq-stepper" style="margin-left:8px">';
    _rfqSteps.forEach((s, i) => {
        let cls = 'rfq-step';
        if (i < si) cls += ' done';
        else if (i === si) cls += ' current';
        html += `<div class="${cls}" title="${_rfqStepLabels[s]}"></div>`;
    });
    html += '</div>';
    return html;
}

async function rfqSwitchTab(tab) {
    _rfqPanelTab = tab;
    const right = document.getElementById('rfqRight-' + _rfqActiveReqId);
    if (right) {
        right.querySelectorAll('.rfq-panel-tab').forEach(t => t.classList.toggle('on', t.dataset.tab === tab));
    }
    await _rfqLoadTab(tab);
}

async function _rfqLoadTab(tab) {
    const body = document.getElementById('rfqPanelBody');
    if (!body) return;
    const partId = _rfqActivePartId;
    if (!partId) return;

    // Check cache
    if (_rfqPanelCache[tab]) {
        _rfqRenderTab(tab, _rfqPanelCache[tab], body);
        return;
    }

    body.innerHTML = '<span class="loading-placeholder">Loading\u2026</span>';
    try {
        let data;
        switch (tab) {
            case 'offers':
                data = await apiFetch(`/api/requirements/${partId}/offers`);
                break;
            case 'tasks':
                data = await apiFetch(`/api/requirements/${partId}/tasks`);
                break;
            case 'notes':
                data = await apiFetch(`/api/requirements/${partId}/notes`);
                break;
            case 'history':
                data = await apiFetch(`/api/requirements/${partId}/history`);
                break;
            case 'sightings':
                data = await apiFetch(`/api/requisitions/${_rfqActiveReqId}/sightings`);
                break;
        }
        // Abort if part changed while loading
        if (_rfqActivePartId !== partId) return;
        _rfqPanelCache[tab] = data;
        _rfqRenderTab(tab, data, body);
    } catch(e) {
        if (_rfqActivePartId !== partId) return;
        body.innerHTML = '<span class="error-placeholder">Failed to load</span>';
    }
}

function _rfqRenderTab(tab, data, body) {
    switch (tab) {
        case 'offers': _rfqRenderOffers(data, body); break;
        case 'tasks': _rfqRenderTasks(data, body); break;
        case 'notes': _rfqRenderNotes(data, body); break;
        case 'history': _rfqRenderHistory(data, body); break;
        case 'sightings': _rfqRenderSightings(data, body); break;
        default: body.innerHTML = '';
    }
}

// ── OFFERS TAB ────────────────────────────────────────────────────────

function _rfqRenderOffers(offers, body) {
    if (!offers || offers.length === 0) {
        body.innerHTML = '<div class="empty-placeholder">No offers yet for this part</div>';
        return;
    }

    const part = _rfqPartsData.find(p => p.id === _rfqActivePartId);
    const targetPrice = part?.target_price;

    // Summary counts
    const total = offers.length;
    const exact = offers.filter(o => !o.is_substitute).length;
    const subs = offers.filter(o => o.is_substitute).length;
    const hist = offers.filter(o => o.is_historical).length;
    const selected = offers.filter(o => o.selected_for_quote).length;

    let html = `<div class="rfq-offer-toolbar">
        <span style="font-size:12px;font-weight:600">${total} Offer${total>1?'s':''}</span>
        <span class="rfq-chip rfq-chip-exact">${exact} Exact</span>
        ${subs > 0 ? '<span class="rfq-chip rfq-chip-sub">' + subs + ' Sub</span>' : ''}
        ${hist > 0 ? '<span class="rfq-chip rfq-chip-hist">' + hist + ' Hist</span>' : ''}
        ${selected > 0 ? '<span class="rfq-chip rfq-chip-sel">' + selected + ' Selected</span>' : ''}
        <span class="rfq-offer-summary">${selected > 0 ? selected + ' selected for quote' : ''}</span>
    </div>`;

    // Offer cards — three-line hierarchy: vendor+flags+price → commercial terms → metadata
    offers.forEach(o => {
        const selCls = o.selected_for_quote ? ' selected' : '';
        const histCls = o.is_historical ? ' historical' : '';

        // Price color vs target
        let priceCls = '';
        let priceTooltip = '';
        if (targetPrice && o.unit_price) {
            const pct = Math.round((o.unit_price / targetPrice - 1) * 100);
            if (pct <= 0) { priceCls = ' under'; priceTooltip = pct + '% vs target'; }
            else if (pct <= 15) { priceCls = ' near'; priceTooltip = '+' + pct + '% vs target'; }
            else { priceCls = ' over'; priceTooltip = '+' + pct + '% vs target'; }
        }

        // Flags — compact inline badges after vendor name
        let flags = '';
        if (o.is_substitute) flags += '<span class="rfq-offer-flag rfq-offer-flag-sub">SUB</span>';
        else flags += '<span class="rfq-offer-flag rfq-offer-flag-exact">EXACT</span>';
        if (o.is_historical) flags += '<span class="rfq-offer-flag rfq-offer-flag-hist">HIST</span>';

        // Age badge
        let ageBadge = '';
        if (o.age_days > 14) ageBadge = '<span class="rfq-offer-age rfq-offer-age-stale">STALE</span>';
        else if (o.age_days > 7) ageBadge = '<span class="rfq-offer-age rfq-offer-age-aging">AGING</span>';

        const price = o.unit_price ? '$' + Number(o.unit_price).toFixed(4) : '\u2014';
        const currency = o.currency && o.currency !== 'USD' ? ' ' + esc(o.currency) : '';

        // ── LINE 2: commercial terms (structured, aligned) ──
        // MPN/Mfr shown inline only when relevant (sub or manufacturer present)
        let mpnChip = '';
        if (o.is_substitute && o.mpn) mpnChip = '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">MPN</span>' + esc(o.mpn) + '</span>';
        if (o.manufacturer) mpnChip += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">Mfr</span>' + esc(o.manufacturer) + '</span>';

        let terms = mpnChip;
        if (o.qty_available) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">Qty</span><b>' + Number(o.qty_available).toLocaleString() + '</b></span>';
        if (o.lead_time) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">Lead</span>' + esc(o.lead_time) + '</span>';
        if (o.condition) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">Cond</span>' + esc(o.condition) + '</span>';
        if (o.date_code) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">DC</span>' + esc(o.date_code) + '</span>';
        if (o.moq) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">MOQ</span>' + o.moq.toLocaleString() + '</span>';
        if (o.packaging) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">Pkg</span>' + esc(o.packaging) + '</span>';
        if (o.warranty) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">Wrty</span>' + esc(o.warranty) + '</span>';
        if (o.country_of_origin) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">COO</span>' + esc(o.country_of_origin) + '</span>';
        if (o.firmware) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">FW</span>' + esc(o.firmware) + '</span>';
        if (o.hardware_code) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">HW</span>' + esc(o.hardware_code) + '</span>';
        if (o.valid_until) terms += '<span class="rfq-ocard-term"><span class="rfq-ocard-term-l">Valid</span>' + esc(o.valid_until) + '</span>';
        // Status/expiry alerts inline
        if (o.status && o.status !== 'active') terms += '<span class="rfq-ocard-term rfq-ocard-term-warn">' + esc(o.status.toUpperCase()) + '</span>';
        if (o.expires_at) {
            const expDate = new Date(o.expires_at);
            const expDays = Math.round((expDate - Date.now()) / 86400000);
            if (expDays <= 3 && expDays >= 0) terms += '<span class="rfq-ocard-term rfq-ocard-term-warn">Exp ' + (expDays === 0 ? 'today' : expDays + 'd') + '</span>';
            else if (expDays < 0) terms += '<span class="rfq-ocard-term rfq-ocard-term-expired">Expired</span>';
        }

        // ── LINE 3: metadata (compressed) ──
        let meta = [];
        if (o.source && o.source !== 'manual') meta.push(esc(o.source));
        if (o.entered_by) meta.push(esc(o.entered_by));
        if (o.created_at) {
            if (o.age_days === 0) meta.push('today');
            else if (o.age_days === 1) meta.push('1d');
            else if (o.age_days < 30) meta.push(o.age_days + 'd');
            else meta.push(new Date(o.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric'}));
        }
        if (o.from_requisition_id) meta.push('RFQ#' + o.from_requisition_id);
        const noteIndicator = o.notes ? '<span class="rfq-ocard-note" title="' + escAttr(o.notes) + '">\ud83d\udcdd</span>' : '';

        html += `<div class="rfq-ocard${selCls}${histCls}" data-offer-id="${o.id}">
            <div class="rfq-ocard-top">
                <input type="checkbox" class="rfq-offer-check" ${o.selected_for_quote ? 'checked' : ''}
                    onclick="event.stopPropagation();rfqToggleOfferSelection(${o.id})"
                    title="Select for quote">
                <div class="rfq-ocard-vendor" title="${escAttr(o.vendor_name)}">${esc(o.vendor_name)}</div>
                <div class="rfq-offer-flags">${flags}${ageBadge}</div>
                <div class="rfq-ocard-price${priceCls}" title="${priceTooltip}">${price}${currency}</div>
            </div>
            ${terms ? '<div class="rfq-ocard-terms">' + terms + '</div>' : ''}
            ${meta.length || noteIndicator ? '<div class="rfq-ocard-meta">' + meta.join(' \u00b7 ') + ' ' + noteIndicator + '</div>' : ''}
        </div>`;
    });

    body.innerHTML = html;
}

async function rfqToggleOfferSelection(offerId) {
    try {
        const res = await apiFetch(`/api/offers/${offerId}/toggle-quote-selection`, { method: 'POST' });
        // Update cached data
        if (_rfqPanelCache.offers) {
            const offer = _rfqPanelCache.offers.find(o => o.id === offerId);
            if (offer) {
                offer.selected_for_quote = res.selected_for_quote;
                offer.selected_at = res.selected_for_quote ? new Date().toISOString() : null;
            }
            _rfqRenderOffers(_rfqPanelCache.offers, document.getElementById('rfqPanelBody'));
        }
        // Update part list chip cluster
        if (_rfqActivePartId) {
            const part = _rfqPartsData.find(p => p.id === _rfqActivePartId);
            if (part) {
                part.selected_count = (_rfqPanelCache.offers || []).filter(o => o.selected_for_quote).length;
                _rfqRenderPartList(_rfqActiveReqId);
            }
        }
    } catch(e) {
        console.error('Toggle selection failed:', e);
    }
}

// ── TASKS TAB ─────────────────────────────────────────────────────────

function _rfqRenderTasks(tasks, body) {
    if (!tasks || tasks.length === 0) {
        body.innerHTML = `<div class="empty-placeholder">No tasks for this part</div>
            <button class="btn btn-sm" style="margin-top:8px" onclick="rfqAddTask()">+ Add Task</button>`;
        return;
    }

    let html = `<div style="margin-bottom:8px;display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:12px;font-weight:600">${tasks.length} Task${tasks.length>1?'s':''}</span>
        <button class="btn btn-sm" onclick="rfqAddTask()">+ Add Task</button>
    </div>`;

    tasks.forEach(t => {
        const dept = t.task_type || 'general';
        const deptLabel = dept === 'sourcing' ? 'Purchasing' : dept === 'sales' ? 'Sales' : 'General';
        const deptCls = dept === 'sourcing' ? 'purchasing' : dept;
        const statusCls = 'rfq-task-status-' + (t.status || 'todo');

        let dueLine = '';
        if (t.due_date) {
            const d = new Date(t.due_date);
            const now = new Date();
            const overdue = d < now && t.status !== 'done';
            dueLine = `<span class="${overdue ? 'task-overdue' : ''}">${overdue ? '\u26a0 ' : ''}Due ${fmtDate(t.due_date)}</span>`;
        }

        html += `<div class="rfq-task-item">
            <div class="rfq-task-dept rfq-task-dept-${deptCls}">
                <span style="font-weight:700;text-transform:uppercase">${deptLabel}</span>
                <span style="font-weight:500;opacity:.85">${esc(t.assigned_to || t.created_by_name || '')}</span>
            </div>
            <div class="rfq-task-body">
                <div class="rfq-task-title">${esc(t.title)}</div>
                ${t.ai_risk_flag ? '<div class="task-risk-flag">\u26a0 ' + esc(t.ai_risk_flag) + '</div>' : ''}
                <div class="rfq-task-meta">
                    <span class="rfq-task-status ${statusCls}">${(t.status || 'todo').replace('_',' ')}</span>
                    ${dueLine}
                    ${t.source === 'ai' || t.source === 'system' ? '<span class="task-auto-tag">auto</span>' : ''}
                </div>
            </div>
        </div>`;
    });

    body.innerHTML = html;
}

async function rfqAddTask() {
    const title = prompt('Task title:');
    if (!title || !title.trim()) return;
    try {
        await apiFetch(`/api/requirements/${_rfqActivePartId}/tasks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: title.trim() }),
        });
        delete _rfqPanelCache.tasks;
        await _rfqLoadTab('tasks');
        // Update part chip cluster
        const part = _rfqPartsData.find(p => p.id === _rfqActivePartId);
        if (part) {
            part.task_count = (part.task_count || 0) + 1;
            _rfqRenderPartList(_rfqActiveReqId);
        }
    } catch(e) {
        console.error('Add task failed:', e);
    }
}

// ── NOTES TAB ─────────────────────────────────────────────────────────

function _rfqRenderNotes(data, body) {
    if (!data) { body.innerHTML = '<div class="empty-placeholder">No notes</div>'; return; }

    let html = '<div style="margin-bottom:8px;display:flex;justify-content:space-between;align-items:center">';
    html += '<span style="font-size:12px;font-weight:600">Notes</span>';
    html += '<button class="btn btn-sm" onclick="rfqAddNote()">+ Add Note</button></div>';

    let hasNotes = false;

    // Requirement notes (may contain multiple timestamped entries)
    if (data.requirement_notes) {
        hasNotes = true;
        const lines = data.requirement_notes.split('\n').filter(l => l.trim());
        lines.forEach(line => {
            html += `<div class="rfq-note-item">
                <div class="rfq-note-label rfq-note-label-req">Requirement Note</div>
                <div class="rfq-note-text">${esc(line)}</div>
            </div>`;
        });
    }

    // Offer notes
    if (data.notes && data.notes.length > 0) {
        hasNotes = true;
        data.notes.forEach(n => {
            html += `<div class="rfq-note-item">
                <div class="rfq-note-label rfq-note-label-offer">Offer Note \u2014 ${esc(n.vendor_name)}</div>
                <div class="rfq-note-text">${esc(n.note)}</div>
                <div class="rfq-note-meta">${n.created_at ? fmtDateTime(n.created_at) : ''}</div>
            </div>`;
        });
    }

    if (!hasNotes) html += '<div class="empty-placeholder">No notes yet</div>';
    body.innerHTML = html;
}

async function rfqAddNote() {
    const text = prompt('Add note:');
    if (!text || !text.trim()) return;
    try {
        await apiFetch(`/api/requirements/${_rfqActivePartId}/notes`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text.trim() }),
        });
        delete _rfqPanelCache.notes;
        await _rfqLoadTab('notes');
    } catch(e) {
        console.error('Add note failed:', e);
    }
}

// ── HISTORY TAB ───────────────────────────────────────────────────────

function _rfqRenderHistory(events, body) {
    if (!events || events.length === 0) {
        body.innerHTML = '<div class="empty-placeholder">No history events</div>';
        return;
    }

    let html = '<div style="font-size:12px;font-weight:600;margin-bottom:8px">History Timeline</div>';

    events.forEach(ev => {
        let icon = '', iconCls = '', text = '';
        const time = ev.created_at ? `<div class="rfq-hist-time">${fmtDateTime(ev.created_at)}</div>` : '';

        switch (ev.type) {
            case 'change':
                icon = '\u270f\ufe0f';
                iconCls = 'rfq-hist-icon-change';
                text = `<b>${esc(ev.user || 'System')}</b> changed ${esc(ev.entity)} <b>${esc(ev.field)}</b>`;
                if (ev.old_value || ev.new_value) {
                    const _fmtVal = v => !v ? '\u2014' : typeof v === 'object' ? JSON.stringify(v) : String(v);
                    text += `<div style="font-size:10px;color:var(--muted);margin-top:2px">${esc(_fmtVal(ev.old_value))} \u2192 ${esc(_fmtVal(ev.new_value))}</div>`;
                }
                break;
            case 'offer_created':
                icon = '\ud83d\udcb0';
                iconCls = 'rfq-hist-icon-offer';
                text = `Offer from <b>${esc(ev.vendor_name)}</b>`;
                if (ev.unit_price) text += ` at $${Number(ev.unit_price).toFixed(4)}`;
                if (ev.qty_available) text += ` \u00d7 ${ev.qty_available}`;
                break;
            case 'rfq_sent':
                icon = '\u2709\ufe0f';
                iconCls = 'rfq-hist-icon-rfq';
                text = `RFQ ${ev.contact_type === 'phone' ? 'called' : 'sent'} to <b>${esc(ev.vendor_name)}</b>`;
                if (ev.status && ev.status !== 'sent') text += ` \u2014 ${ev.status}`;
                break;
            case 'task_done':
                icon = '\u2705';
                iconCls = 'rfq-hist-icon-task';
                text = `Task completed: <b>${esc(ev.title)}</b>`;
                break;
            default:
                icon = '\u2022';
                text = JSON.stringify(ev);
        }

        html += `<div class="rfq-hist-item">
            <div class="rfq-hist-icon ${iconCls}">${icon}</div>
            <div class="rfq-hist-body">${text}${time}</div>
        </div>`;
    });

    body.innerHTML = html;
}

// ── SIGHTINGS TAB ─────────────────────────────────────────────────────

function _rfqRenderSightings(data, body) {
    if (!data) { body.innerHTML = '<div class="empty-placeholder">No sightings data</div>'; return; }

    // Filter sightings to current part only
    const partId = _rfqActivePartId;
    const partData = data[String(partId)];
    if (!partData || !partData.sightings || partData.sightings.length === 0) {
        body.innerHTML = '<div class="empty-placeholder">No sightings for this part. Run a search to find sources.</div>';
        return;
    }

    const sightings = partData.sightings.slice();
    const blCount = partData.blacklisted_count || 0;

    // Sort: exact MPN match first, then by score/qty descending
    const part = _rfqPartsData.find(p => p.id === partId);
    const partMpn = (part?.mpn || partData.label || '').trim().toUpperCase();
    sightings.sort((a, b) => {
        const aExact = (a.mpn_matched || '').trim().toUpperCase() === partMpn ? 1 : 0;
        const bExact = (b.mpn_matched || '').trim().toUpperCase() === partMpn ? 1 : 0;
        if (aExact !== bExact) return bExact - aExact;
        const sa = a.score ?? a.vendor_card?.vendor_score ?? 0;
        const sb = b.score ?? b.vendor_card?.vendor_score ?? 0;
        if (sb !== sa) return sb - sa;
        return (b.qty_available || 0) - (a.qty_available || 0);
    });

    // Look up target price for price-vs-target indicator
    const targetPrice = part?.target_price ?? null;

    let html = `<div style="font-size:12px;font-weight:600;margin-bottom:8px">${sightings.length} Source${sightings.length>1?'s':''} Found</div>`;
    if (blCount > 0) {
        html += `<div style="font-size:10px;color:var(--muted);margin-bottom:6px;padding:3px 6px;background:var(--bg-alt);border-radius:4px">\ud83d\udeab ${blCount} vendor${blCount>1?'s':''} hidden (blacklisted)</div>`;
    }

    // Compact sightings table
    html += '<table class="dtbl" style="font-size:10px"><thead><tr>';
    html += '<th>Vendor</th><th>MPN</th><th>Qty</th><th>Price</th><th>Cond</th><th>Lead</th><th>Source</th>';
    html += '</tr></thead><tbody>';

    sightings.slice(0, 100).forEach(s => {
        const price = s.unit_price ? '$' + Number(s.unit_price).toFixed(4) : '\u2014';
        let priceColor = '';
        let priceTitle = '';
        if (targetPrice != null && s.unit_price) {
            const pctD = ((s.unit_price - targetPrice) / targetPrice) * 100;
            priceColor = pctD <= 0 ? 'color:var(--green)' : pctD <= 15 ? 'color:var(--amber)' : 'color:var(--red)';
            priceTitle = ' title="' + (pctD > 0 ? '+' : '') + pctD.toFixed(0) + '% vs target ($' + Number(targetPrice).toFixed(4) + ')"';
        }
        const isExact = (s.mpn_matched || '').trim().toUpperCase() === partMpn;
        const matchBadge = !isExact ? ' <span style="font-size:8px;color:var(--blue);font-weight:600">ALT</span>' : '';
        const histFlag = s.is_historical || s.is_material_history ? ' <span style="font-size:8px;color:var(--muted);font-weight:600">HIST</span>' : '';
        html += `<tr>
            <td style="font-weight:600;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.vendor_name || '')}</td>
            <td class="mono">${esc(s.mpn_matched || '')}${matchBadge}</td>
            <td class="mono">${s.qty_available || '\u2014'}</td>
            <td class="mono" style="${priceColor}"${priceTitle}>${price}</td>
            <td>${esc(s.condition || '')}</td>
            <td>${s.lead_time || s.lead_time_days ? (s.lead_time_days || '') + 'd' : '\u2014'}</td>
            <td>${esc(s.source_type || '')}${histFlag}</td>
        </tr>`;
    });

    html += '</tbody></table>';
    if (sightings.length > 100) {
        html += `<div style="font-size:10px;color:var(--muted);margin-top:4px">${sightings.length - 100} more sources not shown</div>`;
    }

    body.innerHTML = html;
}


// ── Freeform AI Parse helpers (called from inline onclick in index.html) ──
async function parseFreeformOffer() {
    const raw = document.getElementById('loPasteText')?.value?.trim();
    if (!raw) { showToast('Paste vendor text first', 'warning'); return; }
    const btn = document.getElementById('loParseBtn');
    btn.disabled = true; btn.textContent = 'Parsing…';
    try {
        const data = await apiFetch('/api/ai/parse-freeform-offer', { method: 'POST', body: { raw_text: raw } });
        const container = document.getElementById('loParsedOffers');
        if (!data.offers?.length) { showToast('No offers found in text', 'info'); return; }
        container.textContent = '';
        data.offers.forEach(o => {
            const div = document.createElement('div');
            div.style.cssText = 'padding:4px 0;border-bottom:1px solid var(--border);font-size:12px';
            const b = document.createElement('b');
            b.textContent = o.mpn || '';
            div.appendChild(b);
            div.appendChild(document.createTextNode(` — ${o.qty||'?'} pcs @ $${o.unit_price||'?'} (${o.vendor_name||''})`));
            container.appendChild(div);
        });
        container.classList.remove('u-hidden');
        showToast(`Parsed ${data.offers.length} offer(s)`, 'success');
    } catch(e) { showToast('AI parse failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Parse with AI'; }
}

async function parseFreeformRfq() {
    const raw = document.getElementById('nrPasteText')?.value?.trim();
    if (!raw) { showToast('Paste customer text first', 'warning'); return; }
    const btn = document.getElementById('nrParseBtn');
    btn.disabled = true; btn.textContent = 'Parsing…';
    try {
        const data = await apiFetch('/api/ai/parse-freeform-rfq', { method: 'POST', body: { raw_text: raw } });
        const container = document.getElementById('nrParsedReqs');
        if (!data.parts?.length) { showToast('No parts found in text', 'info'); return; }
        container.textContent = '';
        data.parts.forEach(p => {
            const div = document.createElement('div');
            div.style.cssText = 'padding:4px 0;border-bottom:1px solid var(--border);font-size:12px';
            const b = document.createElement('b');
            b.textContent = p.mpn || '';
            div.appendChild(b);
            const desc = ` — qty ${p.qty||'?'}` + (p.manufacturer ? ` (${p.manufacturer})` : '');
            div.appendChild(document.createTextNode(desc));
            container.appendChild(div);
        });
        container.classList.remove('u-hidden');
        showToast(`Parsed ${data.parts.length} part(s)`, 'success');
    } catch(e) { showToast('AI parse failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Parse with AI'; }
}

// ── ESM: expose all inline-handler functions to window ────────────────
Object.assign(window, {
    // Public functions referenced in onclick/onchange/oninput/onkeydown handlers
    addDrillRow, archiveFromList, _archiveWithOutcome, autoLogEmail, autoLogVendorCall, checkForReplies, openContactTimeline, loadContactNudges,
    cloneFromList, closeModal, ddApplyGlobalMarkup, ddApplyQuoteMarkup, ddBuildQuote,
    ddAddNewContact, ddConfirmBuildQuote, ddConfirmSendQuote, ddDeleteOffer, ddDeleteQuote, ddEditOffer, ddExpandQuote,
    ddFindContacts, ddMarkQuoteResult, ddOnContactSelect, ddOpenBuyPlanModal, ddPasteRows, ddPickEnrichedContact, ddRefreshPreview,
    ddSubmitBuyPlan, ddUpdateBpTotals, ddUpdateQuoteField,
    ddExportSightingsCsv, ddPromptVendorEmail, ddResearchAll, ddResearchPart, ddReviseQuote, ddSaveEditOffer, ddSaveQuoteDraft, ddSendBulkRfq, ddSendQuote,
    ddToggleAllOffers, ddToggleGroupOffers, ddToggleHistory, ddToggleHistorySightings,
    ddToggleOffer, ddToggleSighting, ddToggleGroupSightings, ddQuickRfq,
    _ddCopyContact, _ddFilterSightings, _ddClearFilters, _ddSetTypeFilter,
    ddReconfirmOffer, ddLogFromHistorical,
    ddUploadFile, debounceOfferHistorySearch, debouncePartsSightingsSearch,
    deleteDrillRow, deleteMaterial, deleteReq, deleteVendor,
    deleteVendorContact, editDeadline, editDrillCell, editMaterialField,
    editReqCell, editReqCustomer, editReqName, editVendorField, escAttr,
    expandMatchingGroups, expandToSubTab, inlineSourceAll,
    loadMoreOfferHistory, loadMorePartsSightings, loadRequisitions,
    logCall, markAllNotifsRead, markNotifRead, markUnavailable, openAddVendorContact,
    openEditVendorContact, openLogOfferFromList, openMaterialPopup,
    openMaterialPopupByMpn, openVendorLogNoteModal,
    openVendorPopup, placeVendorCall, renderReqList, requoteFromList,
    rfqConfirmCustomEmail, rfqIncludeRepeats, rfqManualEmail, rfqRemoveVendor, rfqSelectEmail,
    rfqToggleVendor, saveDeadline, sendEmailReply, sendFollowUp,
    setToolbarQuickFilter, showView, sidebarNav, sortMatList, sortReqList,
    selectVendor,
    openVendorDrawer, closeVendorDrawer, switchVendorDrawerTab,
    sortVendorList, threadSearchFilter, toggleAllDrillRows,
    archiveGoPage, toggleArchiveGroup, toggleConfirmedQuotes, toggleDrillDown,
    toggleGroup, toggleOfferHistory, togglePartsSightings,
    toggleReqSelection, toggleSighting, toggleThreadMessages,
    toggleVendorEmails, toggleVpThreadMessages, unifiedEnrichVendor,
    viewThread, vpSetRating, vpSubmitReview, vpToggleBlacklist,
    // Internal/underscore-prefixed functions used in inline handlers
    _acceptParsedOffers, _appendAddRow, _autoPollReplies, _buildEffortTip, _cancelAddRow, ddUpdateQuoteLine,
    ddInlineEditOffer, ddApproveOffer, ddRejectOffer, ddShowChangelog, ddRefreshTab,
    _clearSightingSelection, _collapseAllDrillDowns, _ddDefaultTab, _ddPromptFallback, _syncMobilePills,
    _ddRenderTierRows, _ddSaveEmail, _ddSearchOverlay, _ddSubTabs,
    _ddTabLabel, _ddVendorInlineBadges, _ddVendorLinkPill,
    _ddVendorScoreRing, _debouncedPartsSightingsSearch,
    _ensureEmailListModal, _formatEmailBody,
    _ddRefreshQuoteTotals, _loadDdSubTab, _matSortArrow,
    _parseTsvInput, _previewPaste, _pushNav,
    _rebuildSightingIndex, _renderDdActivity, _renderDdDetails, _renderDdQA, _renderDdTasks, _renderParsedSummary,
    _renderQAEntry, _filterQA, _openAskQuestionModal, _submitQuestion,
    _openAnswerModal, _submitAnswer, _renderInsightsCard, _toggleInsightsCard, _refreshInsights,
    _renderDdOffers, _renderDdQuotes, _renderDdTab, _renderDdTabPills,
    _renderDetailDeadline, _renderDrillDownTable, _renderReqRow,
    _renderRfqPrepareProgress, _renderSourcingDrillDown, _reqBadge,
    _saveAddRow, _selfGuard, _sortArrow, _switchDdTab,
    _timeAgo, _updateDdBulkButton, _updateDrillToggleLabel, _updateInlineRfqBar,
    _isDeadlineUrgent,
    _updateBulkFollowUpBtn, _updateToolbarStats, _vendorHasPartsToSend,
    // HTML template inline handlers
    _clearNrValidation, clearFileInput, clearNrSite, closeLogOfferModal,
    createRequisition, debouncedFilterSites, debouncedFilterVendors,
    debouncedLoadCustomers, debouncedLoadMaterials, debouncedMainSearch,
    doStockImport, filterVendorList, openNewReqModal,
    openRfqDrawer, closeRfqDrawer,
    rfqDeselectAllVendors, rfqSelectAllVendors, saveVendorContact, sendBulkFollowUp,
    saveVendorLogCall, saveVendorLogNote, setMainView,
    setRfqCondition, setStatusFilter, setVendorTier, showFileReady,
    submitLogOffer, submitPastedRows, toggleMobileSidebar,
    toggleMyAccounts, toggleSidebar, toggleSidebarGroup, toggleStockImport,
    triggerMainSearch,
    // AI feature functions
    aiNormalizeParts, aiParseReply, parseFreeformOffer, parseFreeformRfq,
    _applyNormalized, _showAiModal,
    // v2 visual helpers
    engRing, healthDot, statCard, daysSince, recencyColor,
    ownerAvatar, factorBar, relationshipHealthBar, contactStatusBar,
    statusPill, activityIcon, getRelativeTime, filterChip,
    CONTACT_STATUS, CONTACT_STATUS_ORDER,
    // Dashboard
    setDashPeriod, setDashScope, setBuyerScope, setDashPerspective, setDashUserFilter,
    setUserFilter, _populateUserFilter, _populateDashUserSelect,
    goToReq, _toggleColGear, toggleColVisibility,
    // Unified state helpers
    stateLoading, stateEmpty, stateError,
    // Mobile navigation & drill-down
    mobileTabNav, mobileMoreNav, toggleMobileMore, mobileBack,
    _openMobileDrillDown, _closeMobileDrillDown, _mobileDdSwitchTab, _dealCardClick,
    _renderMobilePartsList, _renderMobileOffersList, _renderMobileQuotesList,
    _renderMobileBuyPlansList, _renderMobileActivityList,
    // Mobile top bar — search toggle & user menu
    _toggleMobileSearch, _showMobileUserMenu,
    // Mobile req list — card redesign
    renderMobileReqList, mobileReqPillTap, _syncMobileReqPills, _renderReqCardMobile,
    // Context panel
    toggleContextPanel, switchCtxTab, bindContextPanel, unbindContextPanel, ctxSendMessage, ctxAttachFile,
    // Universal intake bar
    showIntakeBar, hideIntakeBar, _intakeInputChange, _intakePaste, _intakeSubmit,
    _intakeUpload, _intakeFileSelected, _intakeImportApi, _intakeClose, _intakeChangeType, _intakeRemoveRow, _intakeConfirm,
    // Shared page helpers
    renderObjHeader, renderStatusStrip, renderBlockerStrip, renderAiCard,
    // RFQ workspace — part-centric layout
    rfqOpenWorkspace, rfqSelectPart, rfqSwitchTab, rfqToggleOfferSelection, rfqAddTask, rfqAddNote,
});
