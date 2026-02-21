/* AVAIL v1.2.0 — CRM, offers, quotes, target pricing */

// ── Bootstrap: read server-rendered config from JSON block ────────────
(function() {
    var el = document.getElementById('app-config');
    if (el) {
        try {
            var cfg = JSON.parse(el.textContent);
            window.__userName = cfg.userName || '';
            window.__userEmail = cfg.userEmail || '';
            window.__isAdmin = !!cfg.isAdmin;
            window.__isDevAssistant = !!cfg.isDevAssistant;
        } catch(e) { console.warn('Failed to parse app-config', e); }
    }
})();

// ── Early stubs (available before full init for onclick handlers) ──────

function toggleMobileSidebar() {
    var sb = document.getElementById('sidebar');
    var ov = document.getElementById('sidebarOverlay');
    if (sb) sb.classList.toggle('mobile-open');
    if (ov) ov.classList.toggle('open');
}

// Close filter panel on outside click
document.addEventListener('click', function(e) {
    document.querySelectorAll('.filter-panel.open').forEach(function(p) {
        if (!p.closest('.filter-wrap').contains(e.target)) p.classList.remove('open');
    });
});

// Sync mobile <-> desktop search + mirror notification badge
document.addEventListener('DOMContentLoaded', function() {
    var ms = document.getElementById('mobileMainSearch');
    var ds = document.getElementById('mainSearch');
    if (ms && ds) {
        ms.addEventListener('input', function() { ds.value = ms.value; });
        ds.addEventListener('input', function() { ms.value = ds.value; });
    }
    var nb = document.getElementById('notifBadge');
    var mb = document.getElementById('mobileNotifBadge');
    if (nb && mb) {
        new MutationObserver(function() {
            mb.textContent = nb.textContent;
            mb.style.display = nb.style.display;
        }).observe(nb, {childList:true, attributes:true, attributeFilter:['style']});
    }
});

let currentReqId = null;
let currentReqName = '';
let searchResults = {};
let _sightingIndex = {};  // sightingId → {reqId, sighting} for O(1) lookups
let searchResultsCache = {};  // keyed by reqId
let selectedSightings = new Set();
let rfqVendorData = [];
let activeTabCache = {};  // reqId → tab name
let _vendorListData = [];   // cached vendor list for client-side filtering
let _vendorTierFilter = 'all';  // all|proven|developing|caution|new
let expandedGroups = new Set();  // reqIds that are expanded (default: all collapsed)
let _ddReqCache = {};  // drill-down requirements cache: rfqId → [requirements]
let _ddSightingsCache = {};      // reqId -> sightings API response
let _ddSelectedSightings = {};   // reqId -> Set of sighting IDs
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
async function apiFetch(url, opts = {}) {
    // CSRF: include double-submit cookie value as header
    const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1];
    if (csrf) opts.headers = {...(opts.headers || {}), 'x-csrftoken': csrf};
    if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
        opts.headers = {'Content-Type': 'application/json', ...(opts.headers || {})};
        opts.body = JSON.stringify(opts.body);
    }
    const method = (opts.method || 'GET').toUpperCase();
    const maxRetries = method === 'GET' ? 2 : 0;
    let lastErr;
    for (let attempt = 0; attempt <= maxRetries; attempt++) {
        if (attempt > 0) {
            await new Promise(r => setTimeout(r, Math.pow(2, attempt - 1) * 1000));
        }
        const res = await fetch(url, opts);
        if (!res.ok) {
            const msg = await res.text().catch(() => res.statusText);
            lastErr = Object.assign(new Error(msg), {status: res.status});
            if (res.status >= 500 && attempt < maxRetries) continue;
            throw lastErr;
        }
        const ct = res.headers.get('content-type') || '';
        return ct.includes('json') ? res.json() : res.text();
    }
    throw lastErr;
}

function debounce(fn, ms = 300) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// Debounced input handlers — client-side filters at 150ms, API calls at 300ms
const debouncedRenderReqTable = debounce(() => renderRequirementsTable(), 150);
const debouncedRenderSources = debounce(() => renderSources(), 150);
const debouncedRenderActivity = debounce(() => renderActivityCards(), 150);
const debouncedLoadCustomers = debounce(() => loadCustomers(), 300);
const debouncedFilterVendors = debounce(() => filterVendorList(), 150);
const debouncedLoadMaterials = debounce(() => loadMaterialList(), 300);
const debouncedFilterSites = debounce((v) => filterSiteTypeahead(v), 150);

// ── Utilities ───────────────────────────────────────────────────────────
function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function escAttr(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function logCatchError(ctx, err) { if (err) console.warn('[' + ctx + ']', err); }

function openModal(id, focusId) {
    var el = document.getElementById(id);
    if (el) el.classList.add('open');
    if (focusId) setTimeout(function() { var f = document.getElementById(focusId); if (f) f.focus(); }, 100);
}

async function guardBtn(btn, loadingText, action) {
    if (!btn || btn.disabled) return;
    var orig = btn.textContent;
    btn.disabled = true;
    if (loadingText) btn.textContent = loadingText;
    try { return await action(); }
    finally { btn.disabled = false; btn.textContent = orig; }
}

function _timeAgo(iso) {
    if (!iso) return '';
    const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}
function fmtDate(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleDateString();
}
function fmtDateTime(iso) {
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

// ── Name Autocomplete ───────────────────────────────────────────────────
function initNameAutocomplete(inputId, listId, hiddenId, opts = {}) {
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
        if (row) row.style.display = show ? '' : 'none';
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
    initNameAutocomplete('stockVendorName', 'stockVendorNameList', null, { types: 'vendor', websiteId: 'stockVendorWebsite' });
    // Route based on URL hash (supports bookmarks + page refresh)
    const initHash = location.hash.replace('#', '');
    const initView = _hashToView[initHash];
    if (initView && initView !== 'view-list') {
        _navFromPopstate = true;
        const initRoutes = {
            'view-vendors': () => showVendors(),
            'view-materials': () => showMaterials(),
            'view-customers': () => showCustomers(),
            'view-buyplans': () => showBuyPlans(),
            'view-proactive': () => showProactiveOffers(),
            'view-performance': () => showPerformance(),
            'view-settings': () => showSettings(),
        };
        if (initRoutes[initView]) initRoutes[initView]();
        const sidebarMap = {'view-vendors':'navVendors','view-materials':'navMaterials','view-customers':'navCustomers','view-buyplans':'navBuyPlans','view-proactive':'navProactive','view-performance':'navScorecards','view-settings':'navSettings'};
        const navBtn = document.getElementById(sidebarMap[initView]);
        if (navBtn) navHighlight(navBtn);
        _navFromPopstate = false;
    }
    await loadRequisitions();
    // Restore last viewed requisition on page reload
    if (!initView || initView === 'view-list') {
        try {
            const lastId = parseInt(localStorage.getItem('lastReqId'));
            const lastName = localStorage.getItem('lastReqName') || '';
            if (lastId) {
                const found = _reqListData.find(r => r.id === lastId);
                if (found) setTimeout(() => toggleDrillDown(lastId), 300);
            }
        } catch(e) { logCatchError('restoreLastReq', e); }
    }
    checkM365Status();
    const dz = document.getElementById('dropZone');
    if (dz) {
        dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
        dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
        dz.addEventListener('drop', e => {
            e.preventDefault(); dz.classList.remove('drag');
            if (e.dataTransfer.files.length) {
                document.getElementById('fileInput').files = e.dataTransfer.files;
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
window.addEventListener('beforeunload', () => clearInterval(_m365Timer));

// ── Role-Based UI Gating ────────────────────────────────────────────────
function applyRoleGating() {
    // Elements with data-role="buyer" are visible for buyer, manager, and admin
    const canBuy = ['buyer','trader','manager','admin'].includes(window.userRole) || window.__isAdmin;
    document.querySelectorAll('[data-role="buyer"]').forEach(el => {
        el.style.display = canBuy ? '' : 'none';
    });
    // Role badge hidden — keep element for JS role gating but don't display
    const roleBadge = document.getElementById('roleBadge');
    if (roleBadge) {
        const roleLabels = {buyer:'Buyer', sales:'Sales', trader:'Trader', manager:'Manager', admin:'Admin', dev_assistant:'Dev'};
        roleBadge.textContent = roleLabels[window.userRole] || window.userRole;
        roleBadge.className = `role-badge role-${window.userRole}`;
        roleBadge.style.display = 'none';
    }
    // Show Buy Plans nav for all roles except dev_assistant
    const bpNav = document.getElementById('navBuyPlans');
    if (bpNav && window.userRole !== 'dev_assistant') bpNav.style.display = '';
    // Proactive nav visible for sales + admin (old + sidebar)
    const pNav = document.getElementById('navProactive');
    if (pNav && (['sales','trader'].includes(window.userRole) || window.__isAdmin)) {
        pNav.style.display = '';
        refreshProactiveBadge();
    }
    // Performance nav visible to all (old + sidebar)
    const perfNav = document.getElementById('navPerformance');
    if (perfNav) perfNav.style.display = '';
    // v7 sidebar nav items
    const perfNav2 = document.getElementById('navScorecards');
    if (perfNav2) perfNav2.style.display = '';
    const pNav2 = document.getElementById('navProactive');
    if (pNav2 && (['sales','trader'].includes(window.userRole) || window.__isAdmin)) pNav2.style.display = '';
    // Enrichment nav visible to admin, manager, trader
    const enrichNav = document.getElementById('navEnrichment');
    if (enrichNav && (window.__isAdmin || ['manager','trader'].includes(window.userRole))) {
        enrichNav.style.display = '';
        refreshEnrichmentBadge();
    }
    // "My Reqs" toggle: hide for sales (they already only see their own)
    const myReqsBtn = document.getElementById('myReqsBtn');
    const myReqsDiv = document.getElementById('myReqsDivider');
    if (window.userRole === 'sales') {
        if (myReqsBtn) myReqsBtn.style.display = 'none';
        if (myReqsDiv) myReqsDiv.style.display = 'none';
    }
    // Settings nav visible to admin and dev_assistant
    const navSettings = document.getElementById('navSettings');
    if (navSettings && (window.__isAdmin || window.__isDevAssistant)) navSettings.style.display = '';
    // Dev assistants: hide Users/Scoring/Create User/Data Import tabs
    if (window.__isDevAssistant && !window.__isAdmin) {
        document.querySelectorAll('.settings-tab-users, .settings-tab-scoring').forEach(el => el.style.display = 'none');
    }
}
function isBuyer() { return ['buyer','trader','manager','admin'].includes(window.userRole) || window.__isAdmin; }

async function refreshProactiveBadge() {
    try {
        const data = await apiFetch('/api/proactive/count');
        const badge = document.getElementById('proactiveBadge');
        if (badge) {
            if (data.count > 0) { badge.textContent = data.count; badge.style.display = ''; }
            else { badge.style.display = 'none'; }
        }
    } catch (e) { logCatchError('proactiveBadge', e); }
}

// ── Navigation ──────────────────────────────────────────────────────────
const ALL_VIEWS = ['view-list', 'view-vendors', 'view-materials', 'view-customers', 'view-buyplans', 'view-proactive', 'view-performance', 'view-settings'];

// Hash-based routing for browser back/forward
const _viewToHash = {'view-list':'rfqs','view-vendors':'vendors','view-materials':'materials','view-customers':'customers','view-buyplans':'buyplans','view-proactive':'proactive','view-performance':'performance','view-settings':'settings'};
const _hashToView = Object.fromEntries(Object.entries(_viewToHash).map(([k,v])=>[v,k]));
let _navFromPopstate = false;

let _lastPushedHash = '';
function _pushNav(viewId) {
    if (_navFromPopstate) return;
    const hash = '#' + (_viewToHash[viewId] || 'rfqs');
    if (hash === _lastPushedHash) return;
    _lastPushedHash = hash;
    if (!location.hash || location.hash === '#') {
        history.replaceState({view: viewId}, '', hash);
    } else {
        history.pushState({view: viewId}, '', hash);
    }
}

window.addEventListener('popstate', (e) => {
    const hash = location.hash.replace('#','');
    // Skip approve-token hashes (handled by crm.js)
    if (hash.startsWith('approve-token/')) return;
    const viewId = _hashToView[hash] || 'view-list';
    _navFromPopstate = true;
    // Route to the correct view
    const routes = {
        'view-list': () => { showList(); setMainPill('rfq'); },
        'view-vendors': () => showVendors(),
        'view-materials': () => showMaterials(),
        'view-customers': () => showCustomers(),
        'view-buyplans': () => showBuyPlans(),
        'view-proactive': () => showProactiveOffers(),
        'view-performance': () => showPerformance(),
        'view-settings': () => showSettings(),
    };
    if (routes[viewId]) routes[viewId]();
    // Highlight correct sidebar button
    const sidebarMap = {'view-list':'navReqs','view-vendors':'navVendors','view-materials':'navMaterials','view-customers':'navCustomers','view-buyplans':'navBuyPlans','view-proactive':'navProactive','view-performance':'navScorecards','view-settings':'navSettings'};
    const navBtn = document.getElementById(sidebarMap[viewId]);
    if (navBtn) navHighlight(navBtn);
    _navFromPopstate = false;
});

function showView(viewId) {
    try { _pushNav(viewId); } catch(e) { console.warn('pushNav:', e); }
    for (const id of ALL_VIEWS) {
        const el = document.getElementById(id);
        if (el) el.style.display = id === viewId ? '' : 'none';
    }
    // Clean up background polling when navigating away from settings/enrichment
    if (typeof _bfPollInterval !== 'undefined' && _bfPollInterval) {
        clearInterval(_bfPollInterval);
        _bfPollInterval = null;
    }
    // Toggle body class so CSS can adjust sidebar for settings view
    const isSettings = viewId === 'view-settings';
    document.body.classList.toggle('on-settings', isSettings);
    // Hide topcontrols on settings; show pills/search/filters only on list view
    const topcontrols = document.getElementById('topcontrols');
    if (topcontrols) {
        topcontrols.style.display = isSettings ? 'none' : '';
        const isListView = viewId === 'view-list';
        topcontrols.querySelectorAll('.fpills, .filter-wrap').forEach(el => {
            el.style.display = isListView ? '' : 'none';
        });
    }
}

function showList() {
    showView('view-list');
    currentReqId = null;
    try { localStorage.removeItem('lastReqId'); localStorage.removeItem('lastReqName'); } catch(e) {}
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
    try { localStorage.setItem('lastReqId', id); localStorage.setItem('lastReqName', name || ''); } catch(e) {}
    setTimeout(() => toggleDrillDown(id), 200);
}

function showVendors() {
    showView('view-vendors');
    currentReqId = null;
    loadVendorList();
}

function showMaterials() {
    showView('view-materials');
    currentReqId = null;
    loadMaterialList();
}

// ── Modals ──────────────────────────────────────────────────────────────
function openNewReqModal() {
    openModal('newReqModal', 'nrName');
}
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

function showToast(msg, type = 'info') {
    let container = document.getElementById('toastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toastContainer';
        container.style.cssText = 'position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    const colors = { info: 'var(--teal)', success: 'var(--green)', error: 'var(--red)', warn: 'var(--amber)' };
    toast.style.cssText = `background:var(--bg2);border-left:4px solid ${colors[type]||colors.info};color:var(--text);padding:10px 16px;border-radius:6px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.25);max-width:340px;opacity:0;transition:opacity .2s`;
    toast.textContent = msg;
    container.appendChild(toast);
    requestAnimationFrame(() => toast.style.opacity = '1');
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, 3000);
}

const _statusLabels = {draft:'Draft',active:'Sourcing',closed:'Closed',offers:'Offers',quoting:'Quoting',quoted:'Quoted',won:'Won',lost:'Lost',archived:'Archived'};
function updateDetailStatus(status) {
    const chip = document.getElementById('detailStatus');
    if (!chip) return;
    chip.className = 'status-chip status-' + status;
    chip.textContent = _statusLabels[status] || status;
    chip.classList.remove('pulse');
    void chip.offsetWidth;
    chip.classList.add('pulse');
}
function notifyStatusChange(data) {
    if (!data || !data.status_changed) return;
    updateDetailStatus(data.req_status);
    const reqInfo = _reqListData.find(r => r.id === currentReqId);
    if (reqInfo) reqInfo.status = data.req_status;
}

// ── Requisitions ────────────────────────────────────────────────────────
let _reqCustomerMap = {};  // id → customer_display
let _reqListData = [];     // cached list for client-side filtering
let _reqStatusFilter = 'all';
let _reqListSort = 'newest';
let _myReqsOnly = false;   // "My Reqs" toggle for non-sales roles
let _serverSearchActive = false; // True when server-side search returned filtered results
let _currentMainView = 'rfq';  // 'rfq' | 'sourcing' | 'archive'
let _archiveGroupsOpen = new Set();  // company_id or customer_display keys that are expanded



const debouncedReqListSearch = debounce(() => {
    const q = (document.getElementById('reqListFilter')?.value || '').trim();
    if (q.length >= 2) loadRequisitions(q);
    else if (q.length === 0) loadRequisitions();
    else renderReqList();  // Short input: client-side only
}, 300);


let _reqAbort = null;  // AbortController for in-flight requisition searches

async function loadRequisitions(query = '') {
    // Cancel any in-flight request
    if (_reqAbort) { try { _reqAbort.abort(); } catch(e){} }
    _reqAbort = new AbortController();
    const signal = _reqAbort.signal;
    try {
        const status = _currentMainView === 'archive' ? '&status=archive' : '';
        const url = query ? `/api/requisitions?q=${encodeURIComponent(query)}${status}` : `/api/requisitions?limit=200${status}`;
        _serverSearchActive = !!query;
        const resp = await apiFetch(url, { signal });
        _reqListData = resp.requisitions || resp;
        _reqListData.forEach(r => { if (r.customer_display) _reqCustomerMap[r.id] = r.customer_display; });
        renderReqList();
    } catch (e) {
        if (e.name === 'AbortError') return;  // superseded by newer search
        logCatchError('loadRequisitions', e); showToast('Failed to load requisitions', 'error');
    }
}

// v7 table sort state
let _reqSortCol = null;
let _reqSortDir = 'asc';

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
const _ddActiveTab = {};  // reqId → current sub-tab name

function _ddSubTabs(mainView) {
    if (mainView === 'sourcing') return ['details', 'sightings', 'activity', 'offers'];
    if (mainView === 'archive') return ['parts'];
    return ['parts', 'offers', 'quotes']; // rfq tab
}

function _ddDefaultTab(mainView) {
    return mainView === 'sourcing' ? 'sightings' : 'parts';
}

function _ddTabLabel(tab) {
    const map = {details:'Details', sightings:'Sightings', activity:'Activity', offers:'Offers', parts:'Parts', quotes:'Quotes'};
    return map[tab] || tab;
}

async function expandToSubTab(reqId, tabName) {
    const drow = document.getElementById('d-' + reqId);
    if (!drow) return;
    if (!drow.classList.contains('open')) {
        await toggleDrillDown(reqId);
    }
    _switchDdTab(reqId, tabName);
}

function _renderDdTabPills(reqId) {
    const tabs = _ddSubTabs(_currentMainView);
    const active = _ddActiveTab[reqId] || _ddDefaultTab(_currentMainView);
    return tabs.map(t =>
        `<button class="dd-tab${t === active ? ' on' : ''}" data-tab="${t}" onclick="event.stopPropagation();_switchDdTab(${reqId},'${t}')">${_ddTabLabel(t)}</button>`
    ).join('');
}

async function _switchDdTab(reqId, tabName) {
    _ddActiveTab[reqId] = tabName;
    const drow = document.getElementById('d-' + reqId);
    if (!drow) return;
    // Update pill state
    drow.querySelectorAll('.dd-tab').forEach(t => t.classList.toggle('on', t.dataset.tab === tabName));
    const panel = drow.querySelector('.dd-panel');
    if (!panel) return;
    await _loadDdSubTab(reqId, tabName, panel);
}

async function _loadDdSubTab(reqId, tabName, panel) {
    if (!_ddTabCache[reqId]) _ddTabCache[reqId] = {};
    const cached = _ddTabCache[reqId][tabName];
    if (cached) { _renderDdTab(reqId, tabName, cached, panel); return; }

    panel.innerHTML = '<span style="font-size:11px;color:var(--muted)">Loading\u2026</span>';
    try {
        let data;
        switch (tabName) {
            case 'details':
            case 'parts':
                data = _ddReqCache[reqId] || await apiFetch(`/api/requisitions/${reqId}/requirements`);
                _ddReqCache[reqId] = data;
                break;
            case 'sightings':
                data = _ddSightingsCache[reqId] || await apiFetch(`/api/requisitions/${reqId}/sightings`);
                _ddSightingsCache[reqId] = data;
                if (!_ddSelectedSightings[reqId]) _ddSelectedSightings[reqId] = new Set();
                break;
            case 'activity':
                data = await apiFetch(`/api/requisitions/${reqId}/activity`);
                break;
            case 'offers':
                data = await apiFetch(`/api/requisitions/${reqId}/offers`);
                break;
            case 'quotes':
                data = await apiFetch(`/api/requisitions/${reqId}/quote`);
                break;
        }
        _ddTabCache[reqId][tabName] = data;
        _renderDdTab(reqId, tabName, data, panel);
    } catch(e) {
        panel.innerHTML = '<span style="font-size:11px;color:var(--red)">Failed to load</span>';
    }
}

function _renderDdTab(reqId, tabName, data, panel) {
    switch (tabName) {
        case 'details': _renderDdDetails(reqId, panel); break;
        case 'parts': _renderDrillDownTable(reqId, panel); break;
        case 'sightings': _renderSourcingDrillDown(reqId, panel); break;
        case 'activity': _renderDdActivity(reqId, data, panel); break;
        case 'offers': _renderDdOffers(reqId, data, panel); break;
        case 'quotes': _renderDdQuotes(reqId, data, panel); break;
        default: panel.innerHTML = '';
    }
}

function _renderDdActivity(reqId, data, panel) {
    const vendors = data.vendors || [];
    if (!vendors.length) { panel.innerHTML = '<span style="font-size:11px;color:var(--muted)">No activity yet</span>'; return; }
    // Summary stats
    let totalContacts = 0, totalReplies = 0, totalCalls = 0, totalEmails = 0;
    for (const v of vendors) {
        totalContacts += (v.contacts || []).length;
        totalReplies += (v.responses || []).length;
        for (const a of (v.activities || [])) {
            if (a.channel === 'phone') totalCalls++;
            if (a.channel === 'email') totalEmails++;
        }
    }
    let html = `<div style="display:flex;gap:16px;margin-bottom:8px;font-size:11px">
        <span><b>${totalContacts}</b> RFQs sent</span>
        <span><b>${totalReplies}</b> replies</span>
        <span><b>${totalCalls}</b> calls</span>
        <span><b>${totalEmails}</b> emails</span>
    </div>`;
    let msgIdx = 0;
    html += '<div style="max-height:500px;overflow-y:auto">';
    for (const v of vendors) {
        const contacts = v.contacts || [];
        const responses = v.responses || [];
        const activities = v.activities || [];
        const hasReply = responses.length > 0;
        const dotColor = hasReply ? 'var(--green)' : 'var(--amber)';
        html += `<div class="act-vendor-card">`;
        html += `<div style="font-size:12px;font-weight:700;display:flex;align-items:center;gap:6px;margin-bottom:6px"><span style="width:7px;height:7px;border-radius:50%;background:${dotColor};display:inline-block"></span>${esc(v.vendor_name)} <span style="font-weight:400;color:var(--muted);font-size:11px">${contacts.length} sent, ${responses.length} replied</span></div>`;
        // Build timeline with email bodies
        const timeline = [];
        for (const c of contacts) timeline.push({type:'sent', date: c.created_at, subject: c.subject || '', body: c.body || '', text: `${c.contact_type} to ${c.vendor_contact || 'vendor'}`, user: c.user_name, parts: c.parts_included || []});
        for (const r of responses) timeline.push({type:'reply', date: r.received_at, subject: r.subject || '', body: r.body || '', text: r.vendor_email || 'vendor', status: r.status, confidence: r.confidence, classification: r.classification});
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
                    confBadge = ` <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:${cc}20;color:${cc}">${pct}%</span>`;
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
                    if (isSent && t.parts && t.parts.length) bodyHtml += `<div style="color:var(--muted);margin-bottom:4px">Parts: ${t.parts.map(p => esc(p)).join(', ')}</div>`;
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

function _formatEmailBody(text) {
    if (!text) return '';
    // Convert plain text email to readable HTML — preserve line breaks, linkify URLs
    let safe = esc(text);
    // Linkify URLs
    safe = safe.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener" style="color:var(--teal)">$1</a>');
    // Preserve line breaks
    safe = safe.replace(/\n/g, '<br>');
    return safe;
}

let _ddSelectedOffers = {};   // reqId → Set of offer IDs

function _renderDdOffers(reqId, data, panel) {
    const groups = data.groups || data || [];
    // Flatten all offers
    let allOffers = [];
    if (Array.isArray(groups)) {
        for (const g of groups) {
            for (const o of (g.offers || [])) {
                allOffers.push({...o, mpn: g.mpn || g.label || ''});
            }
        }
    }
    if (!allOffers.length) { panel.innerHTML = '<span style="font-size:11px;color:var(--muted)">No offers yet</span>'; return; }
    if (!_ddSelectedOffers[reqId]) _ddSelectedOffers[reqId] = new Set();
    const sel = _ddSelectedOffers[reqId];
    // Sort by price
    allOffers.sort((a, b) => (a.unit_price || 999999) - (b.unit_price || 999999));
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-size:11px"><b>${allOffers.length}</b> offer${allOffers.length !== 1 ? 's' : ''}${sel.size > 0 ? ` &middot; <b>${sel.size}</b> selected` : ''}</span>
        <button class="btn btn-primary btn-sm" id="ddBuildQuoteBtn-${reqId}" ${sel.size === 0 ? 'disabled style="opacity:.5"' : ''} onclick="event.stopPropagation();ddBuildQuote(${reqId})">Build Quote (${sel.size})</button>
    </div>`;
    html += `<table class="dtbl"><thead><tr><th style="width:28px"><input type="checkbox" onchange="ddToggleAllOffers(${reqId},this.checked)" ${sel.size === allOffers.length ? 'checked' : ''}></th><th>MPN</th><th>Vendor</th><th>Qty</th><th>Price</th><th>Lead Time</th><th>Condition</th><th>Date</th><th>Source</th></tr></thead><tbody>`;
    for (const o of allOffers) {
        const oid = o.id || o.offer_id;
        const checked = sel.has(oid) ? 'checked' : '';
        const price = o.unit_price != null ? '$' + parseFloat(o.unit_price).toFixed(2) : '\u2014';
        const date = o.created_at ? fmtRelative(o.created_at) : '';
        const src = o.source || o.offer_source || '';
        html += `<tr class="${checked ? 'selected' : ''}" onclick="ddToggleOffer(${reqId},${oid},event)">
            <td><input type="checkbox" ${checked} onclick="event.stopPropagation();ddToggleOffer(${reqId},${oid},event)" data-oid="${oid}"></td>
            <td class="mono">${esc(o.mpn || o.offered_mpn || '')}</td>
            <td>${esc(o.vendor_name || '')}</td>
            <td class="mono">${o.quantity || '\u2014'}</td>
            <td class="mono" style="color:var(--teal)">${price}</td>
            <td>${esc(o.lead_time || '\u2014')}</td>
            <td>${esc(o.condition || '')}</td>
            <td style="font-size:10px">${date}</td>
            <td style="font-size:10px;color:var(--muted)">${esc(src)}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    panel.innerHTML = html;
}

function ddToggleOffer(reqId, offerId, event) {
    if (event) event.stopPropagation();
    if (!_ddSelectedOffers[reqId]) _ddSelectedOffers[reqId] = new Set();
    const sel = _ddSelectedOffers[reqId];
    if (sel.has(offerId)) sel.delete(offerId); else sel.add(offerId);
    // Re-render to update button and checkboxes
    const data = _ddTabCache[reqId]?.offers;
    const drow = document.getElementById('d-' + reqId);
    if (data && drow) {
        const panel = drow.querySelector('.dd-panel');
        if (panel) _renderDdOffers(reqId, data, panel);
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

async function ddBuildQuote(reqId) {
    const sel = _ddSelectedOffers[reqId];
    if (!sel || sel.size === 0) return;
    try {
        await apiFetch('/api/requisitions/' + reqId + '/quote', {
            method: 'POST', body: { offer_ids: Array.from(sel) }
        });
        showToast('Quote built — switching to Quotes tab', 'success');
        // Clear cache and switch to quotes tab
        if (_ddTabCache[reqId]) delete _ddTabCache[reqId].quotes;
        _switchDdTab(reqId, 'quotes');
    } catch (e) {
        const msg = (e.message || '').toLowerCase();
        if (e.status === 400 && msg.includes('customer site')) {
            showToast('Link this requisition to a customer site first', 'error');
        } else {
            showToast('Error building quote: ' + (e.message || 'unknown'), 'error');
        }
    }
}

function _renderDdQuotes(reqId, data, panel) {
    if (!data || (!data.id && !data.quote_id && !(data.lines || []).length)) {
        panel.innerHTML = '<span style="font-size:11px;color:var(--muted)">No quote prepared yet</span>';
        return;
    }
    const q = data;
    const lines = q.lines || q.line_items || [];
    let html = '';
    // Quote header
    const statusMap = {draft:'Draft',sent:'Sent',revised:'Revised',won:'Won',lost:'Lost'};
    const statusLabel = statusMap[q.status] || q.status || 'Draft';
    const statusColor = q.status === 'won' ? 'var(--green)' : q.status === 'lost' ? 'var(--red)' : q.status === 'sent' ? 'var(--blue)' : 'var(--muted)';
    html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:12px;font-weight:700">Quote #${q.id || q.quote_id || ''} <span style="color:${statusColor};font-weight:600">${statusLabel}</span></span>
        ${q.sent_at ? `<span style="font-size:10px;color:var(--muted)">Sent ${fmtRelative(q.sent_at)}</span>` : ''}
    </div>`;
    if (lines.length) {
        html += `<table class="dtbl"><thead><tr><th>MPN</th><th>Qty</th><th>Buy $</th><th>Sell $</th><th>Margin</th><th>Vendor</th></tr></thead><tbody>`;
        let totalCost = 0, totalRev = 0;
        for (const l of lines) {
            const buy = l.buy_price || l.unit_cost || 0;
            const sell = l.sell_price || l.unit_sell || 0;
            const qty = l.quantity || 0;
            const margin = sell > 0 ? Math.round(((sell - buy) / sell) * 100) : 0;
            const marginColor = margin >= 20 ? 'var(--green)' : margin >= 10 ? 'var(--amber)' : 'var(--red)';
            totalCost += buy * qty;
            totalRev += sell * qty;
            html += `<tr>
                <td class="mono">${esc(l.mpn || l.offered_mpn || '')}</td>
                <td class="mono">${qty}</td>
                <td class="mono">$${parseFloat(buy).toFixed(2)}</td>
                <td class="mono" style="color:var(--teal)">$${parseFloat(sell).toFixed(2)}</td>
                <td style="color:${marginColor};font-weight:600">${margin}%</td>
                <td>${esc(l.vendor_name || '')}</td>
            </tr>`;
        }
        const totalMargin = totalRev > 0 ? Math.round(((totalRev - totalCost) / totalRev) * 100) : 0;
        html += `</tbody><tfoot><tr style="font-weight:700">
            <td colspan="2">Total</td>
            <td class="mono">$${totalCost.toFixed(2)}</td>
            <td class="mono" style="color:var(--teal)">$${totalRev.toFixed(2)}</td>
            <td>${totalMargin}%</td>
            <td></td>
        </tr></tfoot></table>`;
    }
    panel.innerHTML = html;
}

async function toggleDrillDown(reqId) {
    const drow = document.getElementById('d-' + reqId);
    const arrow = document.getElementById('a-' + reqId);
    if (!drow) return;
    const opening = !drow.classList.contains('open');
    drow.classList.toggle('open');
    if (arrow) arrow.classList.toggle('open');
    _updateDrillToggleLabel();
    if (!opening) return;

    // Load default sub-tab
    const defaultTab = _ddActiveTab[reqId] || _ddDefaultTab(_currentMainView);
    _ddActiveTab[reqId] = defaultTab;
    // Update pill active state
    drow.querySelectorAll('.dd-tab').forEach(t => t.classList.toggle('on', t.dataset.tab === defaultTab));
    const panel = drow.querySelector('.dd-panel');
    if (!panel) return;
    await _loadDdSubTab(reqId, defaultTab, panel);
}

function _renderDdDetails(reqId, targetPanel) {
    const dd = targetPanel || (document.getElementById('d-' + reqId) || {}).querySelector?.('.dd-panel');
    if (!dd) return;
    const reqs = _ddReqCache[reqId] || [];
    const meta = _reqListData.find(r => r.id === reqId) || {};

    let html = '<div class="dd-details">';

    // Requisition header
    html += '<div class="dd-det-header">';
    html += `<div class="dd-det-row"><span class="dd-det-label">Customer</span><span class="dd-det-val">${esc(meta.customer_display || '—')}</span></div>`;
    html += `<div class="dd-det-row"><span class="dd-det-label">RFQ</span><span class="dd-det-val">${esc(meta.name || '')}</span></div>`;
    if (meta.deadline) html += `<div class="dd-det-row"><span class="dd-det-label">Need By</span><span class="dd-det-val">${meta.deadline === 'ASAP' ? '<b style="color:var(--red)">ASAP</b>' : esc(meta.deadline)}</span></div>`;
    if (meta.created_by_name) html += `<div class="dd-det-row"><span class="dd-det-label">Created By</span><span class="dd-det-val">${esc(meta.created_by_name)}</span></div>`;
    if (meta.created_at) html += `<div class="dd-det-row"><span class="dd-det-label">Created</span><span class="dd-det-val">${new Date(meta.created_at).toLocaleDateString()}</span></div>`;
    html += '</div>';

    // Parts table
    if (!reqs.length) {
        html += '<p style="font-size:11px;color:var(--muted)">No parts</p>';
    } else {
        html += `<div class="dd-det-parts"><div class="dd-det-row" style="margin-bottom:4px"><span class="dd-det-label">${reqs.length} Part${reqs.length > 1 ? 's' : ''}</span></div>`;
        for (const r of reqs) {
            const subs = (r.substitutes || []).filter(s => s);
            html += '<div class="dd-det-part">';
            html += `<div class="dd-det-mpn"><span class="mono">${esc(r.primary_mpn || '—')}</span>`;
            if (subs.length) html += ` <span class="dd-det-subs">+ ${subs.map(s => '<span class="mono">' + esc(s) + '</span>').join(', ')}</span>`;
            html += '</div>';
            // Detail chips
            const chips = [];
            if (r.brand) chips.push(`<span class="dd-det-chip">${esc(r.brand)}</span>`);
            if (r.target_qty) chips.push(`<span class="dd-det-chip">Qty: <b>${Number(r.target_qty).toLocaleString()}</b></span>`);
            if (r.target_price != null) chips.push(`<span class="dd-det-chip">Target: <b>$${parseFloat(r.target_price).toFixed(2)}</b></span>`);
            if (r.date_codes) chips.push(`<span class="dd-det-chip">DC: ${esc(r.date_codes)}</span>`);
            if (r.condition) chips.push(`<span class="dd-det-chip">Cond: ${esc(r.condition)}</span>`);
            if (r.firmware) chips.push(`<span class="dd-det-chip">FW: ${esc(r.firmware)}</span>`);
            if (r.hardware_codes) chips.push(`<span class="dd-det-chip">HW: ${esc(r.hardware_codes)}</span>`);
            if (r.packaging) chips.push(`<span class="dd-det-chip">Pkg: ${esc(r.packaging)}</span>`);
            if (chips.length) html += `<div class="dd-det-chips">${chips.join('')}</div>`;
            if (r.notes) html += `<div class="dd-det-notes">${esc(r.notes)}</div>`;
            html += '</div>';
        }
        html += '</div>';
    }

    html += '</div>';
    dd.innerHTML = html;
}

function _renderDrillDownTable(rfqId, targetPanel) {
    const dd = targetPanel || (document.getElementById('d-' + rfqId) || {}).querySelector?.('.dd-panel');
    if (!dd) return;
    const reqs = _ddReqCache[rfqId] || [];
    if (!reqs.length) { dd.innerHTML = '<span style="font-size:11px;color:var(--muted)">No parts yet</span>'; return; }
    const DD_LIMIT = 100;
    const showAll = dd.dataset.showAll === '1';
    const visible = showAll ? reqs : reqs.slice(0, DD_LIMIT);
    let html = `<table class="dtbl"><thead><tr>
        <th>MPN</th><th>Qty</th><th>Target $</th><th>Subs</th><th>Condition</th><th>Date Codes</th><th>FW</th><th>HW</th><th>Pkg</th><th>Notes</th><th>Vendors</th><th style="width:24px"></th>
    </tr></thead><tbody>`;
    for (const r of visible) {
        const subsText = (r.substitutes || []).length ? r.substitutes.join(', ') : '—';
        const notesTrunc = (r.notes || '').length > 30 ? r.notes.substring(0, 30) + '\u2026' : (r.notes || '—');
        html += `<tr>
            <td class="mono dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'primary_mpn')">${esc(r.primary_mpn || '—')}</td>
            <td class="mono dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'target_qty')">${r.target_qty || 0}</td>
            <td class="mono dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'target_price')" style="color:${r.target_price ? 'var(--teal)' : 'var(--muted)'}">${r.target_price != null ? '$' + parseFloat(r.target_price).toFixed(2) : '—'}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'substitutes')" style="font-size:10px">${esc(subsText)}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'condition')">${esc(r.condition || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'date_codes')">${esc(r.date_codes || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'firmware')" style="font-size:10px">${esc(r.firmware || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'hardware_codes')" style="font-size:10px">${esc(r.hardware_codes || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'packaging')" style="font-size:10px">${esc(r.packaging || '—')}</td>
            <td class="dd-edit" onclick="event.stopPropagation();editDrillCell(this,${rfqId},${r.id},'notes')" title="${escAttr(r.notes || '')}" style="font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(notesTrunc)}</td>
            <td class="mono">${r.sighting_count || 0}</td>
            <td><button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteDrillRow(${rfqId},${r.id})" title="Remove" style="font-size:10px;padding:1px 5px">\u2715</button></td>
        </tr>`;
    }
    html += '</tbody></table>';
    if (!showAll && reqs.length > DD_LIMIT) {
        html += `<a onclick="event.stopPropagation();this.closest('.dd-panel').dataset.showAll='1';_renderDrillDownTable(${rfqId})" style="font-size:11px;color:var(--blue);cursor:pointer;display:inline-block;margin-top:4px">Show all ${reqs.length} parts\u2026</a>`;
    }
    dd.innerHTML = html;
}

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
    el.focus();
    if (el.select) el.select();

    let _cancelled = false;
    const save = async () => {
        if (_cancelled) return;
        const val = el.value.trim();
        if (val === currentVal) { _renderDrillDownTable(rfqId); return; }
        const body = {};
        if (field === 'target_price') body[field] = val ? parseFloat(val) : null;
        else if (field === 'target_qty') body[field] = parseInt(val) || 1;
        else if (field === 'substitutes') body[field] = val ? val.split(',').map(s => s.trim()).filter(Boolean) : [];
        else body[field] = val;
        try {
            await apiFetch(`/api/requirements/${reqId}`, { method: 'PUT', body });
            const idx = reqs.findIndex(x => x.id === reqId);
            if (idx >= 0) Object.assign(reqs[idx], body);
        } catch(e) { logCatchError('editDrillCell', e); }
        _renderDrillDownTable(rfqId);
    };

    el.addEventListener('blur', save);
    if (field === 'condition') {
        el.addEventListener('change', () => el.blur());
    }
    el.addEventListener('keydown', e => {
        if (e.key === 'Enter' && field !== 'notes') { e.preventDefault(); el.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); _cancelled = true; _renderDrillDownTable(rfqId); }
    });
}

async function addDrillRow(rfqId) {
    const mpn = prompt('Part number (MPN):');
    if (!mpn || !mpn.trim()) return;
    try {
        await apiFetch(`/api/requisitions/${rfqId}/requirements`, {
            method: 'POST', body: { primary_mpn: mpn.trim(), target_qty: 1 }
        });
        delete _ddReqCache[rfqId];
        // Update the count in the list data
        const rfq = _reqListData.find(r => r.id === rfqId);
        if (rfq) rfq.requirement_count = (rfq.requirement_count || 0) + 1;
        // Re-fetch and render
        _ddReqCache[rfqId] = await apiFetch(`/api/requisitions/${rfqId}/requirements`);
        _renderDrillDownTable(rfqId);
        // Update header count
        const drow = document.getElementById('d-' + rfqId);
        if (drow) {
            const hdr = drow.querySelector('span[style*="font-weight:700"]');
            const total = _ddReqCache[rfqId].length;
            if (hdr) hdr.textContent = `${total} part${total !== 1 ? 's' : ''}`;
        }
    } catch(e) { showToast('Failed to add part', 'error'); }
}

async function deleteDrillRow(rfqId, reqId) {
    if (!confirm('Remove this part?')) return;
    try {
        await apiFetch(`/api/requirements/${reqId}`, { method: 'DELETE' });
        const reqs = _ddReqCache[rfqId];
        if (reqs) {
            const idx = reqs.findIndex(x => x.id === reqId);
            if (idx >= 0) reqs.splice(idx, 1);
        }
        // Update the count in the list data
        const rfq = _reqListData.find(r => r.id === rfqId);
        if (rfq && rfq.requirement_count > 0) rfq.requirement_count--;
        _renderDrillDownTable(rfqId);
        // Update header count
        const drow = document.getElementById('d-' + rfqId);
        if (drow) {
            const hdr = drow.querySelector('span[style*="font-weight:700"]');
            const total = (reqs || []).length;
            if (hdr) hdr.textContent = `${total} part${total !== 1 ? 's' : ''}`;
        }
    } catch(e) { showToast('Failed to remove part', 'error'); }
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

function _renderSourcingDrillDown(reqId, targetPanel) {
    const dd = targetPanel || (document.getElementById('d-' + reqId) || {}).querySelector?.('.dd-panel');
    if (!dd) return;
    const data = _ddSightingsCache[reqId] || {};
    const groups = Object.entries(data); // [ [reqId, {label, sightings}], ... ]
    if (!groups.length) { dd.innerHTML = '<span style="font-size:11px;color:var(--muted)">No sightings yet</span>'; return; }

    // Fetch per-requirement scores if not cached
    if (!_ddScoreCache[reqId]) {
        apiFetch(`/api/requisitions/${reqId}/sourcing-score`).then(scores => {
            _ddScoreCache[reqId] = {};
            for (const rs of (scores.requirements || [])) {
                _ddScoreCache[reqId][rs.requirement_id] = rs;
            }
            _renderSourcingDrillDown(reqId); // re-render with scores
        }).catch(() => {});
        _ddScoreCache[reqId] = {}; // mark as loading
    }
    const scoreMap = _ddScoreCache[reqId] || {};

    const sel = _ddSelectedSightings[reqId] || new Set();
    const DD_LIMIT = 100;
    const showAll = dd.dataset.showAll === '1';
    let html = '';
    for (const [rId, group] of groups) {
        const sightings = group.sightings || [];
        const label = group.label || 'Unknown MPN';
        // Per-requirement sourcing score dot with tooltip
        const rs = scoreMap[rId];
        let effortBadge = '';
        if (rs) {
            const dotColor = rs.color === 'green' ? 'var(--green)' : rs.color === 'yellow' ? 'var(--amber)' : 'var(--red)';
            effortBadge = ` <span class="effort-wrap"><span class="effort-dot" style="background:${dotColor}"></span><span style="font-size:9px;color:var(--muted);margin-left:2px">${Math.round(rs.score)}</span>${_buildEffortTip(rs.score, rs.color, rs.signals)}</span>`;
        }
        html += `<div style="margin-bottom:10px">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
                <span style="font-size:11px;font-weight:700;color:var(--text2)">${esc(label)}${effortBadge} <span style="font-weight:400;color:var(--muted)">(${sightings.length} source${sightings.length !== 1 ? 's' : ''})</span></span>
                <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 6px;margin-left:4px" onclick="event.stopPropagation();ddResearchPart(${reqId},${rId})" title="Re-search this part">\u21bb Search</button>
            </div>`;
        if (!sightings.length) {
            html += '<div style="font-size:11px;color:var(--muted);margin-bottom:6px">No sources found</div></div>';
            continue;
        }
        const visible = showAll ? sightings : sightings.slice(0, DD_LIMIT);
        html += `<table class="dtbl"><thead><tr>
            <th style="width:24px"></th><th>Vendor</th><th>MPN</th><th>Qty</th><th>Price</th><th>Source</th><th>Score</th><th>Condition</th><th>Date</th>
        </tr></thead><tbody>`;
        for (const s of visible) {
            const hasEmail = !!(s.vendor_email || (s.vendor_card && s.vendor_card.has_emails));
            const checked = sel.has(s.id) ? 'checked' : '';
            const dimStyle = !hasEmail ? 'opacity:.5' : '';
            const disabledAttr = !hasEmail ? 'disabled title="No vendor email"' : '';
            const price = s.unit_price != null ? '$' + parseFloat(s.unit_price).toFixed(2) : '\u2014';
            const qty = s.qty_available != null ? Number(s.qty_available).toLocaleString() : '\u2014';
            const scoreVal = s.score != null ? parseFloat(s.score).toFixed(1) : '\u2014';
            const safeVName = (s.vendor_name||'').replace(/'/g, "\\'");
            const needsEmail = !hasEmail ? ` <a onclick="event.stopPropagation();ddPromptVendorEmail(${reqId},${s.id},'${safeVName}')" style="color:var(--red);font-size:10px;cursor:pointer;font-weight:600">needs email</a>` : '';
            const sAge = s.created_at ? fmtRelative(s.created_at) : '\u2014';
            html += `<tr style="${dimStyle}">
                <td><input type="checkbox" ${checked} ${disabledAttr} onclick="event.stopPropagation();ddToggleSighting(${reqId},${s.id})"></td>
                <td>${esc(s.vendor_name || '\u2014')}${needsEmail}</td>
                <td class="mono">${esc(s.mpn_matched || '\u2014')}</td>
                <td class="mono">${qty}</td>
                <td class="mono" style="color:${s.unit_price ? 'var(--teal)' : 'var(--muted)'}">${price}</td>
                <td style="font-size:10px">${esc(s.source_type || '\u2014')}</td>
                <td class="mono">${scoreVal}</td>
                <td style="font-size:10px">${esc(s.condition || '\u2014')}</td>
                <td style="font-size:10px;color:var(--muted)">${sAge}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        if (!showAll && sightings.length > DD_LIMIT) {
            html += `<a onclick="event.stopPropagation();this.closest('.dd-panel').dataset.showAll='1';_renderSourcingDrillDown(${reqId})" style="font-size:11px;color:var(--blue);cursor:pointer;display:inline-block;margin-top:4px">Show all ${sightings.length} sources\u2026</a>`;
        }
        html += '</div>';
    }
    dd.innerHTML = html;
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
    if (!btn) return;
    const sel = _ddSelectedSightings[reqId];
    const count = sel ? sel.size : 0;
    btn.style.display = count > 0 ? '' : 'none';
    btn.textContent = `Send Bulk RFQ (${count})`;
}

async function ddPromptVendorEmail(reqId, sightingId, vendorName) {
    const email = prompt(`Enter email for ${vendorName}:`);
    if (!email || !email.trim()) return;
    const trimmed = email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) { showToast('Invalid email address', 'error'); return; }
    try {
        await apiFetch('/api/vendor-card/add-email', {
            method: 'POST', body: { vendor_name: vendorName, email: trimmed }
        });
        showToast(`Email added for ${vendorName}`, 'success');
        // Update cached sighting so re-render picks it up
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

function ddSendBulkRfq(reqId) {
    const sel = _ddSelectedSightings[reqId];
    if (!sel || !sel.size) return;
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
        showToast('Search failed: ' + (e.message || e), 'error');
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
        showToast('Search failed: ' + (e.message || e), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '&#x1f50d; Search All'; }
    }
}

// ── Log Offer Modal ─────────────────────────────────────────────────────
async function openLogOfferFromList(reqId) {
    document.getElementById('loReqId').value = reqId;
    // Load requirements to populate part picker
    const reqs = _ddReqCache[reqId] || await apiFetch(`/api/requisitions/${reqId}/requirements`).catch(() => []);
    _ddReqCache[reqId] = reqs;
    const sel = document.getElementById('loReqPart');
    sel.innerHTML = '<option value="">Select part...</option>';
    for (const r of (reqs || [])) {
        sel.innerHTML += `<option value="${r.id}" data-mpn="${escAttr(r.primary_mpn || '')}">${esc(r.primary_mpn || 'Part #' + r.id)}${r.target_qty ? ' (qty ' + r.target_qty + ')' : ''}</option>`;
    }
    // Clear form fields
    document.getElementById('loVendor').value = '';
    document.getElementById('loQty').value = '';
    document.getElementById('loPrice').value = '';
    document.getElementById('loLead').value = '';
    document.getElementById('loMoq').value = '';
    document.getElementById('loCond').value = 'new';
    document.getElementById('loDc').value = '';
    document.getElementById('loPkg').value = '';
    document.getElementById('loMfr').value = '';
    document.getElementById('loNotes').value = '';
    openModal('logOfferModal', 'loVendor');
}

function closeLogOfferModal() {
    closeModal('logOfferModal');
}

async function submitLogOffer() {
    const reqId = parseInt(document.getElementById('loReqId').value);
    const partSel = document.getElementById('loReqPart');
    const reqPartId = partSel.value ? parseInt(partSel.value) : null;
    const mpn = partSel.selectedOptions[0]?.dataset?.mpn || partSel.selectedOptions[0]?.textContent || '';
    const vendor = document.getElementById('loVendor').value.trim();
    if (!vendor) { showToast('Vendor name is required', 'error'); return; }
    if (!mpn) { showToast('Select a part', 'error'); return; }
    const btn = document.getElementById('loSubmitBtn');
    btn.disabled = true; btn.textContent = 'Saving\u2026';
    try {
        const body = {
            mpn: mpn,
            vendor_name: vendor,
            requirement_id: reqPartId,
            qty_available: parseInt(document.getElementById('loQty').value) || null,
            unit_price: parseFloat(document.getElementById('loPrice').value) || null,
            lead_time: document.getElementById('loLead').value.trim() || null,
            moq: parseInt(document.getElementById('loMoq').value) || null,
            condition: document.getElementById('loCond').value || 'new',
            date_code: document.getElementById('loDc').value.trim() || null,
            packaging: document.getElementById('loPkg').value.trim() || null,
            manufacturer: document.getElementById('loMfr').value.trim() || null,
            notes: document.getElementById('loNotes').value.trim() || null,
            source: 'manual',
            status: 'active',
        };
        await apiFetch(`/api/requisitions/${reqId}/offers`, { method: 'POST', body });
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
        showToast('Failed to log offer: ' + (e.message || e), 'error');
    } finally {
        btn.disabled = false; btn.textContent = 'Log Offer';
    }
}

function renderReqList() {
    _ddReqCache = {};
    _ddSightingsCache = {};
    _ddSelectedSightings = {};
    const el = document.getElementById('reqList');
    let data = _reqListData;
    // When server search is active, skip status/text filters (server already filtered)
    if (!_serverSearchActive) {
        if (_reqStatusFilter === 'all') {
            data = data.filter(r => !['archived', 'won', 'lost', 'closed'].includes(r.status));
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
    if (_myReqsOnly && window.userId) {
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
                case 'status': va = a.status || ''; vb = b.status || ''; break;
                case 'sales': va = a.created_by_name || ''; vb = b.created_by_name || ''; break;
                case 'age': va = a.created_at || ''; vb = b.created_at || ''; break;
                case 'deadline': va = a.deadline === 'ASAP' ? '0000-00-00' : (a.deadline || '9999-12-31'); vb = b.deadline === 'ASAP' ? '0000-00-00' : (b.deadline || '9999-12-31'); break;
                case 'sent': va = a.rfq_sent_count || 0; vb = b.rfq_sent_count || 0; break;
                case 'resp': { const sa = a.rfq_sent_count || 0; const sb = b.rfq_sent_count || 0; va = sa > 0 ? (a.reply_count || 0) / sa : 0; vb = sb > 0 ? (b.reply_count || 0) / sb : 0; break; }
                case 'searched': va = a.last_searched_at || ''; vb = b.last_searched_at || ''; break;
                case 'matches': va = a.proactive_match_count || 0; vb = b.proactive_match_count || 0; break;
                case 'score': va = a.sourcing_score || 0; vb = b.sourcing_score || 0; break;
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
    const countEl = document.getElementById('reqStatusCount');
    if (countEl) countEl.textContent = `${data.length}`;

    if (!data.length) {
        const labels = {all:'',draft:'Draft',active:'Sourcing',offers:'Offers',quoted:'Quoted',archive:'Archive'};
        el.innerHTML = '<p class="empty">No ' + (labels[_reqStatusFilter] || '') + ' requisitions</p>';
        return;
    }

    // Tab-aware table headers
    const thClass = (col) => _reqSortCol === col ? ' class="sorted"' : '';
    const sa = (col) => `<span class="sort-arrow">${_sortArrow(col)}</span>`;
    const v = _currentMainView;
    let thead;
    if (v === 'sourcing') {
        thead = `<thead><tr>
            <th style="width:36px;cursor:pointer;font-size:10px" onclick="toggleAllDrillRows()" id="ddToggleAll">\u25b6</th>
            <th onclick="sortReqList('name')"${thClass('name')} style="min-width:200px">RFQ ${sa('name')}</th>
            <th onclick="sortReqList('score')"${thClass('score')} title="Sourcing score">Score ${sa('score')}</th>
            <th onclick="sortReqList('deadline')"${thClass('deadline')}>Need By ${sa('deadline')}</th>
            <th onclick="sortReqList('offers')"${thClass('offers')}>Offers ${sa('offers')}</th>
            <th onclick="sortReqList('reqs')"${thClass('reqs')}>Parts ${sa('reqs')}</th>
            <th onclick="sortReqList('sourced')"${thClass('sourced')}>Sourced ${sa('sourced')}</th>
            <th onclick="sortReqList('sent')"${thClass('sent')}>RFQs Sent ${sa('sent')}</th>
            <th onclick="sortReqList('resp')"${thClass('resp')}>Resp % ${sa('resp')}</th>
            <th onclick="sortReqList('searched')"${thClass('searched')}>Searched ${sa('searched')}</th>
            <th onclick="sortReqList('age')"${thClass('age')}>Age ${sa('age')}</th>
            <th style="width:100px"></th>
        </tr></thead>`;
    } else if (v === 'archive') {
        thead = `<thead><tr>
            <th style="width:36px;cursor:pointer;font-size:10px" onclick="toggleAllDrillRows()" id="ddToggleAll">\u25b6</th>
            <th onclick="sortReqList('name')"${thClass('name')} style="min-width:200px">RFQ ${sa('name')}</th>
            <th onclick="sortReqList('reqs')"${thClass('reqs')}>Parts ${sa('reqs')}</th>
            <th onclick="sortReqList('offers')"${thClass('offers')}>Offers ${sa('offers')}</th>
            <th onclick="sortReqList('status')"${thClass('status')}>Outcome ${sa('status')}</th>
            <th onclick="sortReqList('matches')"${thClass('matches')}>Matches ${sa('matches')}</th>
            <th onclick="sortReqList('sales')"${thClass('sales')}>Sales ${sa('sales')}</th>
            <th onclick="sortReqList('age')"${thClass('age')}>Age ${sa('age')}</th>
            <th style="width:90px"></th>
        </tr></thead>`;
    } else {
        thead = `<thead><tr>
            <th style="width:36px;cursor:pointer;font-size:10px" onclick="toggleAllDrillRows()" id="ddToggleAll">\u25b6</th>
            <th onclick="sortReqList('name')"${thClass('name')} style="min-width:200px">RFQ ${sa('name')}</th>
            <th onclick="sortReqList('reqs')"${thClass('reqs')}>Parts ${sa('reqs')}</th>
            <th>Quote</th>
            <th>Sourcing</th>
            <th>Offers</th>
            <th onclick="sortReqList('sales')"${thClass('sales')}>Sales ${sa('sales')}</th>
            <th onclick="sortReqList('age')"${thClass('age')}>Age ${sa('age')}</th>
            <th onclick="sortReqList('deadline')"${thClass('deadline')}>Need By ${sa('deadline')}</th>
            <th style="width:60px"></th>
        </tr></thead>`;
    }

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
    el.innerHTML = `<table class="tbl">${thead}<tbody>${rowsHtml}</tbody></table>`;
    _updateToolbarStats();
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

function _updateToolbarStats() {
    const el = document.getElementById('toolbarStats');
    if (!el) return;
    const all = _reqListData;
    const now = new Date(); now.setHours(0,0,0,0);

    let nGreen = 0, nYellow = 0;
    for (const r of all) {
        if (r.reply_count > 0) nGreen++;
        const dl = r.deadline;
        if (!dl) continue;
        const isAsap = String(dl).toUpperCase() === 'ASAP';
        if (isAsap) continue;
        const d = new Date(dl); d.setHours(0,0,0,0);
        const diff = Math.round((d - now) / 86400000);
        if (diff === 0) nYellow++;
    }

    const qf = _toolbarQuickFilter;
    const pill = (color, count, label) =>
        `<div class="ts-pill ts-${color}${qf === color ? ' active' : ''}" onclick="setToolbarQuickFilter('${color}')"><span class="ts-n">${count}</span> ${label}</div>`;

    el.innerHTML =
        pill('green', nGreen, 'Offers') +
        pill('yellow', nYellow, 'Bid Due') +
        `<div class="ts-pill ts-grey${qf ? '' : ' active'}" onclick="setToolbarQuickFilter('')"><span class="ts-n">${all.length}</span> All</div>`;
}

function _renderReqRow(r) {
    const total = r.requirement_count || 0;
    const sourced = r.sourced_count || 0;
    const offers = r.reply_count || 0;
    const pct = total > 0 ? Math.round((sourced / total) * 100) : 0;
    const v = _currentMainView;

    // Status badge mapping
    const badgeMap = {draft:'b-draft',active:'b-src',sourcing:'b-src',closed:'b-comp',offers:'b-off',quoted:'b-qtd',quoting:'b-qtd',archived:'b-draft',won:'b-off',lost:'b-draft'};
    const bc = badgeMap[r.status] || 'b-draft';

    // Age — days since created
    let age = '';
    if (r.created_at) {
        const days = Math.floor((Date.now() - new Date(r.created_at).getTime()) / 86400000);
        age = days === 0 ? 'Today' : days === 1 ? '1d' : days + 'd';
    }

    // Need By — v7 deadline alert system
    let dl = '', dlClass = '';
    if (r.deadline === 'ASAP') {
        dl = '<span class="dl dl-asap">ASAP</span>';
    } else if (r.deadline) {
        const d = new Date(r.deadline);
        const now = new Date(); now.setHours(0,0,0,0);
        const diff = Math.round((d - now) / 86400000);
        const fmt = fmtDate(r.deadline);
        if (diff < 0) { dl = `<span class="dl dl-u">\ud83d\udd34 OVERDUE ${fmt}</span>`; dlClass = ' dl-row-overdue'; }
        else if (diff === 0) { dl = `<span class="dl dl-u dl-flash">\ud83d\udd34 DUE TODAY</span>`; dlClass = ' dl-row-today'; }
        else if (diff <= 3) { dl = `<span class="dl dl-w">\u26a0\ufe0f ${fmt}</span>`; dlClass = ' dl-row-warn'; }
        else dl = `<span class="dl dl-ok">\u2713 ${fmt}</span>`;
    } else if (v === 'sourcing') {
        dl = '<span class="dl dl-asap">ASAP</span>';
    } else {
        dl = '<span class="dl dl-set" title="Click to set deadline">+ Set date</span>';
    }

    // Customer display — dedup "Company — Company"
    let cust = r.customer_display || '';
    const dp = cust.split(' \u2014 ');
    if (dp.length === 2 && dp[0].trim() === dp[1].trim()) cust = dp[0].trim();
    if (!cust) cust = r.name || '';

    // New-offers dot
    let dot = '';
    if (r.has_new_offers && r.latest_offer_at) {
        const h = (Date.now() - new Date(r.latest_offer_at).getTime()) / 3600000;
        if (h < 12) dot = ' <span class="new-offers-dot" title="New offers"></span>';
        else if (h < 96) dot = ' <span class="new-offers-dot red" title="New offers"></span>';
    }

    // Name cell — shared across all tabs (no summary div — info goes in dedicated columns)
    const nameCell = `<td><b class="cust-link" onclick="event.stopPropagation();toggleDrillDown(${r.id})">${esc(cust)}</b>${dot} <span style="font-size:10px;color:var(--muted)">${esc(r.name || '')}</span></td>`;

    // Last Searched — relative timestamp
    let searched = '';
    if (r.last_searched_at) {
        const h = (Date.now() - new Date(r.last_searched_at).getTime()) / 3600000;
        if (h < 1) searched = '<' + Math.max(1, Math.round(h * 60)) + 'm ago';
        else if (h < 24) searched = Math.round(h) + 'h ago';
        else searched = Math.round(h / 24) + 'd ago';
    } else {
        searched = '<span style="color:var(--muted)">\u2014</span>';
    }

    // Per-tab data cells and actions
    let dataCells, actions, colspan;

    if (v === 'sourcing') {
        // Sourcing: Score, Need By, Offers, Parts, Sourced, RFQs Sent, Resp %, Searched, Age, Status
        const sent = r.rfq_sent_count || 0;
        const respPct = sent > 0 ? Math.round((offers / sent) * 100) + '%' : '\u2014';

        // Sourcing score indicator with tooltip
        const scVal = r.sourcing_score != null ? r.sourcing_score : 0;
        const scColor = r.sourcing_color || 'red';
        const scDotColor = scColor === 'green' ? 'var(--green)' : scColor === 'yellow' ? 'var(--amber)' : 'var(--red)';
        const effortCell = `<td style="text-align:center"><span class="effort-wrap"><span class="effort-dot" style="background:${scDotColor}"></span><span style="font-size:10px;color:var(--muted);margin-left:3px">${Math.round(scVal)}</span>${_buildEffortTip(scVal, scColor, r.sourcing_signals)}</span></td>`;

        // Offers cell — clickable to expand offers sub-tab
        let offersCell;
        if (offers > 0) {
            offersCell = `<td class="mono"><b class="cust-link" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')" title="View offers">${offers}</b></td>`;
        } else {
            offersCell = `<td class="mono">${offers}</td>`;
        }

        dataCells = `
            ${effortCell}
            <td class="dl-cell" onclick="event.stopPropagation();editDeadline(${r.id},this)" title="Click to edit deadline">${dl}</td>
            ${offersCell}
            <td class="mono">${total}</td>
            <td><div class="prog"><div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div><span class="prog-txt">${sourced}/${total}</span></div></td>
            <td class="mono">${sent}</td>
            <td class="mono">${respPct}</td>
            <td style="font-size:11px">${searched}</td>
            <td class="mono" style="font-size:11px">${age}</td>`;
        actions = `<td style="white-space:nowrap"><button class="btn btn-primary btn-sm" onclick="event.stopPropagation();openLogOfferFromList(${r.id})" title="Log a confirmed offer">+ Log Offer</button></td>`;
        colspan = 12;
    } else if (v === 'archive') {
        // Archive: Parts, Offers, Outcome · $value, Matches, Sales, Age
        const wonVal = r.quote_won_value ? ` <span style="font-size:10px;color:var(--green)">\u00b7 ${fmtDollars(r.quote_won_value)}</span>` : '';
        const pmCnt = r.proactive_match_count || 0;
        const matchBadge = pmCnt > 0
            ? `<span style="color:var(--green);font-weight:600">${pmCnt}</span>`
            : '<span style="color:var(--muted)">\u2014</span>';
        dataCells = `
            <td class="mono">${total}</td>
            <td class="mono">${offers}</td>
            <td style="white-space:nowrap"><span class="badge ${bc}">${_statusLabels[r.status] || r.status}</span>${wonVal}</td>
            <td class="mono" style="font-size:11px">${matchBadge}</td>
            <td>${esc(r.created_by_name || '')}</td>
            <td class="mono" style="font-size:11px">${age}</td>`;
        actions = `<td style="white-space:nowrap"><button class="btn btn-sm" onclick="event.stopPropagation();archiveFromList(${r.id})" title="Restore from archive">&#x21a9; Restore</button> <button class="btn btn-sm" onclick="event.stopPropagation();cloneFromList(${r.id})" title="Clone as new draft">&#x1f4cb; Clone</button> <button class="btn btn-sm" onclick="event.stopPropagation();requoteFromList(${r.id})" title="Re-quote this RFQ">&#x1f4dd; Re-quote</button></td>`;
        colspan = 9;
    } else {
        // RFQ: Parts, Quote, Sourcing, Offers, Sales, Age, Need By
        // Quote status cell
        let qCell = '<span style="color:var(--muted)">\u2014</span>';
        if (r.quote_status === 'won') qCell = `<span style="color:var(--green);font-weight:600">Won${r.quote_won_value ? ' ' + fmtDollars(r.quote_won_value) : ''}</span>`;
        else if (r.quote_status === 'lost') qCell = '<span style="color:var(--red)">Lost</span>';
        else if (r.quote_status === 'sent') qCell = `<span style="color:var(--blue)">Sent ${fmtRelative(r.quote_sent_at)}</span>`;
        else if (r.quote_status === 'revised') qCell = '<span style="color:var(--amber)">Revised</span>';
        else if (r.quote_status === 'draft') qCell = '<span style="color:var(--muted)">Draft</span>';
        // Source Progress cell — compact sourcing status
        const _srcPct = total > 0 ? Math.round((sourced / total) * 100) : 0;
        let srcCell;
        if (total === 0) srcCell = '<span style="color:var(--muted)">\u2014</span>';
        else {
            const barColor = _srcPct >= 80 ? 'var(--green)' : _srcPct >= 40 ? 'var(--amber)' : 'var(--red)';
            srcCell = `<div style="display:flex;align-items:center;gap:4px"><div style="flex:1;height:4px;background:var(--bg3,#e2e8f0);border-radius:2px;overflow:hidden;min-width:32px"><div style="height:100%;width:${_srcPct}%;background:${barColor};border-radius:2px"></div></div><span class="mono" style="font-size:10px">${sourced}/${total}</span></div>`;
        }
        // Offers cell
        let offCell = '<span style="color:var(--muted)">\u2014</span>';
        const _oCnt = r.offer_count || 0;
        if (_oCnt > 0) {
            offCell = `<b>${_oCnt}</b>`;
            if (r.best_offer_price) offCell += ` \u00b7 ${fmtDollars(r.best_offer_price)}`;
        }

        dataCells = `
            <td class="mono">${total}</td>
            <td style="font-size:11px;white-space:nowrap">${qCell}</td>
            <td style="font-size:11px;white-space:nowrap;min-width:80px">${srcCell}</td>
            <td style="font-size:11px;white-space:nowrap">${offCell}</td>
            <td>${esc(r.created_by_name || '')}</td>
            <td class="mono" style="font-size:11px">${age}</td>
            <td class="dl-cell" onclick="event.stopPropagation();editDeadline(${r.id},this)" title="Click to edit deadline">${dl}</td>`;
        // RFQ tab button state machine: blue Source → yellow Sourcing → green Offers
        let rfqBtn;
        if (r.status === 'draft') {
            rfqBtn = `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();inlineSourceAll(${r.id})" title="Submit to sourcing">&#x25b6; Source</button>`;
        } else if (r.status === 'quoted' || r.status === 'quoting') {
            rfqBtn = `<button class="btn btn-q btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'quotes')" title="View quote">Quoted</button>`;
        } else if (offers > 0 && r.has_new_offers) {
            rfqBtn = `<button class="btn btn-g btn-sm btn-flash" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')" title="New offers — click to review">Offers (${offers})</button>`;
        } else if (offers > 0) {
            rfqBtn = `<button class="btn btn-g btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')" title="View offers">Offers (${offers})</button>`;
        } else {
            rfqBtn = `<button class="btn btn-y btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'parts')" title="Sourcing in progress">Sourcing</button>`;
        }
        actions = `<td style="white-space:nowrap">${rfqBtn} <button class="btn-archive" onclick="event.stopPropagation();archiveFromList(${r.id})" title="Archive">&#x1f4e5; Archive</button></td>`;
        colspan = 10;
    }

    // Build drill-down header: action buttons vary by tab
    let ddHeader;
    if (v === 'sourcing') {
        const lastSearch = r.last_searched_at ? _timeAgo(r.last_searched_at) : 'never';
        ddHeader = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px">
            <span style="font-size:12px;font-weight:700">${total} part${total !== 1 ? 's' : ''} <span style="font-weight:400;font-size:10px;color:var(--muted)">searched ${lastSearch}</span></span>
            <div style="display:flex;gap:6px">
                <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();ddResearchAll(${r.id})" title="Search all supplier APIs">&#x1f50d; Search All</button>
                <button class="btn btn-primary btn-sm" id="bulkRfqBtn-${r.id}" style="display:none" onclick="event.stopPropagation();ddSendBulkRfq(${r.id})">Send Bulk RFQ (0)</button>
            </div>
        </div>`;
    } else if (v === 'archive') {
        ddHeader = `<div style="margin-bottom:2px"><span style="font-size:12px;font-weight:700">${total} part${total !== 1 ? 's' : ''}</span></div>`;
    } else {
        ddHeader = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px">
            <span style="font-size:12px;font-weight:700">${total} part${total !== 1 ? 's' : ''}</span>
            <div style="display:flex;gap:6px">
                <button class="btn btn-sm" onclick="event.stopPropagation();addDrillRow(${r.id})" title="Add part">+ Add Part</button>
                <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')" title="Select offers and build quote">+ Quote</button>
            </div>
        </div>`;
    }

    return `<tr class="${dlClass}" onclick="toggleDrillDown(${r.id})">
        <td><button class="ea" id="a-${r.id}">\u25b6</button></td>
        ${nameCell}
        ${dataCells}
        ${actions}
    </tr>
    <tr class="drow" id="d-${r.id}"><td colspan="${colspan}">
        ${ddHeader}
        <div class="dd-tabs">${_renderDdTabPills(r.id)}</div>
        <div class="dd-panel"><span style="font-size:11px;color:var(--muted)">${total} parts \u2014 expand for details</span></div>
    </td></tr>`;
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


// ── v7 Filter Panel (unified) ───────────────────────────────────────────
let _activeFilters = {};
let _toolbarQuickFilter = '';

function toggleFilter(panelId) {
    const el = document.getElementById(panelId);
    if (!el) return;
    const opening = !el.classList.contains('open');
    el.classList.toggle('open');
    if (opening) buildFilterGroups();
}

function toggleFilterSection(titleEl) {
    titleEl.classList.toggle('open');
    const body = titleEl.nextElementSibling;
    if (body) body.classList.toggle('open');
}

function buildFilterGroups() {
    const container = document.getElementById('filterGroups');
    if (!container) return;

    // Left column: Quick Filters (always visible)
    let left = _filterGroupHtml('Quick Filters', [
        {value:'my_accounts', label:'My Accounts'},
        {value:'has_review', label:'Needs Review'},
        {value:'high_value', label:'High Value'},
        {value:'has_quote', label:'Has Quote'}
    ]);

    // Right column: Sales Person (collapsible) with Customer nested inside
    let right = '';
    const salesPeople = [...new Set(_reqListData.map(r => r.created_by_name).filter(Boolean))].sort();
    const customers = [...new Set(_reqListData.map(r => r.customer_display).filter(Boolean))].sort();
    if (salesPeople.length) {
        let spItems = salesPeople.map(n => `<label><input type="checkbox" value="sales_${n}" onchange="countActiveFilters()"> ${esc(n)}</label>`).join('');
        // Nest Customer as sub-section inside Sales Person
        if (customers.length) {
            spItems += `<div class="filter-group" style="margin-top:6px;padding-left:4px">
                <div class="filter-group-title collapsible" style="font-size:11px" onclick="toggleFilterSection(this)">Customer</div>
                <div class="filter-group-body">${customers.map(c => `<label><input type="checkbox" value="cust_${c}" onchange="countActiveFilters()"> ${esc(c)}</label>`).join('')}</div>
            </div>`;
        }
        right += `<div class="filter-group"><div class="filter-group-title collapsible" onclick="toggleFilterSection(this)">Sales Person</div><div class="filter-group-body">${spItems}</div></div>`;
    }

    container.innerHTML = `<div class="filter-cols"><div class="filter-col">${left}</div><div class="filter-col">${right}</div></div>`;

    // Restore checked state
    container.querySelectorAll('input[type=checkbox]').forEach(cb => {
        cb.checked = !!(_activeFilters[cb.value]);
    });
    // Auto-expand sections that have active filters
    container.querySelectorAll('.filter-group-title.collapsible').forEach(t => {
        const body = t.nextElementSibling;
        if (body && body.querySelector('input:checked')) {
            t.classList.add('open');
            body.classList.add('open');
        }
    });
}

function _filterGroupHtml(title, items) {
    return `<div class="filter-group"><div class="filter-group-title">${title}</div>${
        items.map(i => `<label><input type="checkbox" value="${i.value}" onchange="countActiveFilters()"> ${esc(i.label)}</label>`).join('')
    }</div>`;
}


function countActiveFilters() {
    const panel = document.getElementById('mainFilterPanel');
    if (!panel) return;
    const n = panel.querySelectorAll('input[type=checkbox]:checked').length;
    const btn = document.querySelector('.filter-btn');
    const badge = document.getElementById('filterBadge');
    if (badge) { badge.textContent = n; badge.style.display = n > 0 ? 'flex' : 'none'; }
    if (btn) btn.classList.toggle('has-active', n > 0);
}

function clearAllFilters() {
    const panel = document.getElementById('mainFilterPanel');
    if (panel) panel.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = false; });
    _activeFilters = {};
    _myReqsOnly = false;
    _toolbarQuickFilter = '';
    countActiveFilters();
    renderReqList();
}

function applyFilters() {
    const panel = document.getElementById('mainFilterPanel');
    if (!panel) return;
    _activeFilters = {};
    panel.querySelectorAll('input[type=checkbox]:checked').forEach(cb => {
        _activeFilters[cb.value] = true;
    });
    _myReqsOnly = !!_activeFilters['my_accounts'];
    panel.classList.remove('open');
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
                case 'green': return r.reply_count > 0;
                case 'yellow': return diff === 0;
                default: return true;
            }
        });
    }
    return filtered;
}

// ── v7 Main View Switcher ───────────────────────────────────────────────
function setMainView(view, btn) {
    _currentMainView = view;
    document.querySelectorAll('#mainPills .fp').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    _activeFilters = {};
    _myReqsOnly = false;
    _toolbarQuickFilter = '';
    countActiveFilters();
    // Hide status toggle — tabs are now locked to their status
    const stEl = document.getElementById('statusToggle');
    if (stEl) stEl.style.display = 'none';
    // Follow-ups panel: only visible on sourcing tab
    const fuPanel = document.getElementById('followUpsPanel');
    if (fuPanel) fuPanel.style.display = 'none';
    if (view === 'rfq') {
        _reqStatusFilter = 'all';
        _serverSearchActive = false;
        loadRequisitions();
    } else if (view === 'sourcing') {
        _reqStatusFilter = 'all';
        _serverSearchActive = false;
        loadRequisitions();
        loadFollowUpsPanel();
    } else if (view === 'archive') {
        _reqStatusFilter = 'archive';
        _serverSearchActive = false;
        apiFetch('/api/requisitions?status=archive')
            .then(resp => {
                _reqListData = resp.requisitions || resp;
                _reqListData.forEach(r => { if (r.customer_display) _reqCustomerMap[r.id] = r.customer_display; });
                renderReqList();
            })
            .catch(() => showToast('Failed to load archived requisitions', 'error'));
    }
    buildFilterGroups();
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


// ── v7 Main Search ──────────────────────────────────────────────────────
const debouncedMainSearch = debounce(function(val) {
    var ds = document.getElementById('mainSearch');
    var ms = document.getElementById('mobileMainSearch');
    if (typeof val === 'string') {
        if (ds) ds.value = val;
        if (ms) ms.value = val;
    }
    const q = (ds?.value || '').trim();
    if (q.length >= 2) loadRequisitions(q);
    else if (q.length === 0) loadRequisitions();
}, 350);

// ── v7 Sidebar Navigation ───────────────────────────────────────────────
function toggleSidebar() {
    document.body.classList.toggle('sb-open');
}

function sidebarNav(page, el) {
    document.querySelectorAll('.sidebar-nav button').forEach(i => i.classList.remove('active'));
    if (el) el.classList.add('active');
    // Close sidebar on mobile
    const sb = document.getElementById('sidebar');
    if (sb && sb.classList.contains('mobile-open')) toggleMobileSidebar();
    document.body.classList.remove('sb-open');
    const routes = {
        reqs: () => { showList(); setMainPill('rfq'); },
        customers: () => showCustomers(),
        vendors: () => showVendors(),
        materials: () => showMaterials(),
        buyplans: () => showBuyPlans(),
        proactive: () => showProactiveOffers(),
        performance: () => showPerformance(),
        settings: () => showSettings()
    };
    try { if (routes[page]) routes[page](); }
    catch(e) { console.error('sidebarNav error:', page, e); }
}

function navHighlight(btn) {
    document.querySelectorAll('.sidebar-nav button').forEach(i => i.classList.remove('active'));
    if (btn) btn.classList.add('active');
    document.body.classList.remove('sb-open');
}

function setMainPill(view) {
    document.querySelectorAll('#mainPills .fp').forEach(b => {
        b.classList.toggle('on', b.dataset.view === view);
    });
}



const searchRequisitions = debounce(query => loadRequisitions(query), 300);

async function sendFollowUp(contactId, vendorName) {
    if (!confirm(`Send follow-up email to ${vendorName}?`)) return;
    try {
        const data = await apiFetch(`/api/follow-ups/${contactId}/send`, { method: 'POST', body: {} });
        showToast(data.message || `Follow-up sent to ${vendorName}`, 'success');
        if (typeof loadActivity === 'function') loadActivity();
        loadFollowUpsPanel();
    } catch (e) { showToast('Failed to send follow-up', 'error'); }
}

// ── Follow-Ups Dashboard Panel ───────────────────────────────────────────
async function loadFollowUpsPanel() {
    const panel = document.getElementById('followUpsPanel');
    if (!panel) return;
    if (_currentMainView !== 'sourcing') { panel.style.display = 'none'; return; }
    try {
        const data = await apiFetch('/api/follow-ups');
        const followUps = data.follow_ups || [];
        if (!followUps.length) { panel.style.display = 'none'; return; }
        // Group by requisition
        const groups = {};
        for (const fu of followUps) {
            const key = fu.requisition_id || 0;
            if (!groups[key]) groups[key] = { name: fu.requisition_name || 'Unknown RFQ', items: [] };
            groups[key].items.push(fu);
        }
        let html = `<div class="card" style="margin:0 16px 12px;padding:12px;border-left:3px solid var(--amber)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-weight:700;font-size:13px;color:var(--amber)">Awaiting Vendor Replies (${followUps.length})</span>
            </div>`;
        for (const [rfqId, g] of Object.entries(groups)) {
            html += `<div style="margin-bottom:6px"><span style="font-weight:600;font-size:12px">${esc(g.name)}</span></div>`;
            for (const fu of g.items) {
                const dayColor = fu.days_waiting > 5 ? 'var(--red)' : fu.days_waiting > 2 ? 'var(--amber)' : 'var(--green)';
                html += `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px">
                    <span style="color:var(--text2)">${esc(fu.vendor_name)}</span>
                    <span style="color:var(--muted)">${esc(fu.vendor_email || '')}</span>
                    <span style="color:${dayColor};font-weight:600">${fu.days_waiting}d</span>
                    ${fu.parts ? `<span style="color:var(--muted)">${esc(fu.parts)}</span>` : ''}
                    ${fu.contact_id ? `<button class="btn btn-ghost btn-sm" onclick="sendFollowUp(${fu.contact_id},'${escAttr(fu.vendor_name)}')" style="padding:1px 6px;font-size:10px">Follow Up</button>` : ''}
                </div>`;
            }
        }
        html += '</div>';
        panel.innerHTML = html;
        panel.style.display = '';
    } catch { panel.style.display = 'none'; }
}

async function createRequisition() {
    const name = document.getElementById('nrName').value.trim();
    if (!name) { showToast('Please enter a requisition name', 'error'); return; }
    const siteId = document.getElementById('nrSiteId')?.value || null;
    if (!siteId) { showToast('Please select a customer account', 'error'); return; }
    const isAsap = document.getElementById('nrAsap')?.checked;
    const dlVal = document.getElementById('nrDeadline')?.value || '';
    const deadline = isAsap ? 'ASAP' : (dlVal || null);
    try {
        const data = await apiFetch('/api/requisitions', {
            method: 'POST', body: { name, customer_site_id: parseInt(siteId), deadline }
        });
        closeModal('newReqModal');
        document.getElementById('nrName').value = '';
        document.getElementById('nrSiteSearch').value = '';
        document.getElementById('nrSiteId').value = '';
        document.getElementById('nrDeadline').value = '';
        document.getElementById('nrAsap').checked = false;
        document.getElementById('nrSiteSelected').style.display = 'none';
        document.getElementById('nrContactField').style.display = 'none';
        await loadRequisitions();
        toggleDrillDown(data.id);
    } catch (e) { showToast('Failed to create requisition', 'error'); }
}

function clearNrSite() {
    document.getElementById('nrSiteId').value = '';
    document.getElementById('nrSiteSearch').value = '';
    document.getElementById('nrSiteSearch').style.display = '';
    document.getElementById('nrSiteSelected').style.display = 'none';
    document.getElementById('nrContactField').style.display = 'none';
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
    try {
        await apiFetch(`/api/requisitions/${reqId}/archive`, { method: 'PUT' });
        showToast('Archived');
        loadRequisitions();
    } catch (e) { showToast('Failed to archive', 'error'); }
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
    try {
        const resp = await apiFetch(`/api/requisitions/${reqId}/clone`, { method: 'POST' });
        // Rename from "(copy)" to "(re-quote)"
        const reName = resp.name.replace('(copy)', '(re-quote)');
        if (reName !== resp.name) {
            await apiFetch(`/api/requisitions/${resp.id}`, { method: 'PUT', body: { name: reName } });
        }
        showToast(`Re-quoted as "${reName}"`);
        // Switch to sourcing view and open the cloned req
        const srcBtn = document.querySelector('#mainPills .fp:nth-child(2)');
        if (srcBtn) setMainView('sourcing', srcBtn);
        await loadRequisitions();
        expandToSubTab(resp.id, 'sightings');
    } catch (e) { showToast('Failed to re-quote', 'error'); }
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
    if (!confirm('Remove this requirement?')) return;
    try { await apiFetch(`/api/requirements/${id}`, { method: 'DELETE' }); } catch(e) { showToast('Failed to delete requirement', 'error'); return; }
    // Clear cached search results & selections for this requirement
    delete searchResults[id];
    _rebuildSightingIndex();
    for (const key of [...selectedSightings]) {
        if (key.startsWith(id + ':')) selectedSightings.delete(key);
    }
    if (currentReqId) searchResultsCache[currentReqId] = searchResults;
    loadRequirements();
}


function showFileReady(inputId, readyId, nameId) {
    const file = document.getElementById(inputId).files[0];
    const readyEl = document.getElementById(readyId);
    const nameEl = document.getElementById(nameId);
    if (file) {
        nameEl.textContent = file.name;
        readyEl.style.display = '';
    } else {
        readyEl.style.display = 'none';
    }
}

function clearFileInput(inputId, readyId) {
    document.getElementById(inputId).value = '';
    document.getElementById(readyId).style.display = 'none';
}

async function doUpload() {
    const file = document.getElementById('fileInput').files[0];
    if (!file || !currentReqId) return;
    const st = document.getElementById('uploadStatus');
    st.className = 'ustatus load'; st.textContent = 'Uploading…'; st.style.display = 'block';
    document.getElementById('uploadReady').style.display = 'none';
    const fd = new FormData(); fd.append('file', file);
    try {
        const data = await apiFetch(`/api/requisitions/${currentReqId}/upload`, { method: 'POST', body: fd });
        st.className = 'ustatus ok';
        st.textContent = `Added ${data.created} parts from ${data.total_rows} rows`;
        loadRequirements();
    } catch (e) {
        st.className = 'ustatus err'; st.textContent = 'Upload error: ' + e.message;
    }
    document.getElementById('fileInput').value = '';
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
        if (btn) { btn.disabled = false; btn.textContent = '\u25b6 Source'; }
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
            expandedGroups.clear();
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
    const keys = Object.keys(searchResults);
    if (!keys.length) {
        el.innerHTML = '<p class="empty">No results found</p>';
        document.getElementById('srcFilterCount').textContent = '';
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
                        const ea = sa.vendor_card?.engagement_score ?? -1;
                        const eb = sb.vendor_card?.engagement_score ?? -1;
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
                if (vc.engagement_score != null) {
                    const es = Math.round(vc.engagement_score);
                    const esColor = es >= 70 ? 'var(--green)' : es >= 40 ? 'var(--amber)' : 'var(--red)';
                    const esBg = es >= 70 ? 'var(--green-light)' : es >= 40 ? 'var(--amber-light)' : 'var(--red-light)';
                    scoreRing = `<span style="display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;border:2px solid ${esColor};background:${esBg};font-size:8px;font-weight:700;color:${esColor};margin-right:3px;cursor:default" title="Engagement: ${es}/100&#10;Based on response rate, recency, velocity, and win rate">${es}</span>`;
                }
                const starStr = vc.avg_rating != null ? `<span class="stars" style="font-size:11px">★</span><span class="stars-num" style="font-size:10px">${vc.avg_rating}</span><span class="stars-count" style="font-size:9px;color:var(--muted)">(${vc.review_count})</span>` : '';
                const cardPill = `<span class="badge" style="background:var(--bg2);cursor:pointer;font-size:9px;padding:1px 6px;margin-left:3px" onclick="event.stopPropagation();openVendorPopup(${vc.card_id})" title="Open vendor card">View</span>`;
                ratingHtml = `<span class="sc-rating">${scoreRing}${starStr}${cardPill}</span>`;
            } else {
                ratingHtml = '<span class="sc-rating sc-rating-new" title="New vendor">☆</span>';
            }

            const octopartLink = s.octopart_url ? `<a href="${escAttr(s.octopart_url)}" target="_blank" class="btn-link">🔗 Octopart</a>` : '';
            const vendorLink = s.vendor_url ? `<a href="${escAttr(s.vendor_url)}" target="_blank" class="btn-link">🏢 Site</a>` : '';
            const phoneLink = s.vendor_phone ? `<a class="btn-call" href="tel:${ph}" onclick="logCall(event,'${vn}','${ph}','${mpn}')">📞 ${esc(s.vendor_phone)}</a>` : '';
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
                    ${phoneLink}${octopartLink}${vendorLink}${unavailBtn}
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



async function markUnavailable(sightingId, unavail) {
    try {
        await apiFetch(`/api/sightings/${sightingId}/unavailable`, {
            method: 'PUT', body: { unavailable: unavail }
        });
        // Update local state — use index for O(1) lookup
        const ref = _sightingIndex[sightingId];
        if (ref) {
            ref.sighting.is_unavailable = unavail;
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

// ── RFQ Flow ────────────────────────────────────────────────────────────
let rfqAllParts = []; // All MPNs on this requisition
let rfqSubsMap = {}; // { primary_mpn: [sub1, sub2, ...] }

async function openBatchRfqModal(prebuiltGroups) {
    const groups = prebuiltGroups || getSelectedByVendor();
    if (!groups.length) return;

    const modal = document.getElementById('rfqModal');
    document.getElementById('rfqPrepare').style.display = '';
    document.getElementById('rfqReady').style.display = 'none';
    rfqCondition = 'any';
    document.querySelectorAll('.rfq-cond-btn').forEach((b,i) => {
        b.classList.toggle('active', i === 0);
    });
    modal.classList.add('open');

    try {
        const data = await apiFetch(`/api/requisitions/${currentReqId}/rfq-prepare`, {
            method: 'POST', body: { vendors: groups.map(g => ({ vendor_name: g.vendor_name })) }
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
    } catch (e) {
        showToast('Failed to prepare RFQ: ' + e.message, 'error');
        closeModal('rfqModal');
        return;
    }

    // Run lookups for vendors without emails (3-tier: cache → scrape → AI)
    const needsLookup = rfqVendorData.filter(v => v.lookup_status === 'pending');
    if (needsLookup.length) {
        // Prevent backdrop click from closing modal during lookup
        modal.dataset.loading = '1';
        document.getElementById('rfqPrepareStatus').textContent = `Finding contacts for ${needsLookup.length} vendor(s)…`;
        needsLookup.forEach(v => { v.lookup_status = 'loading'; });
        _renderRfqPrepareProgress();
        // Look up all vendors in parallel instead of one-at-a-time
        let done = 0;
        await Promise.all(needsLookup.map(async (v) => {
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
                v.contact_tier = data.tier || 0;
            } catch (e) {
                console.warn(`Vendor lookup failed for ${v.vendor_name}:`, e);
                v.lookup_status = 'no_email';
            }
            done++;
            document.getElementById('rfqPrepareStatus').textContent = `Finding contacts… ${done}/${needsLookup.length} done`;
            _renderRfqPrepareProgress();
        }));
        delete modal.dataset.loading;
    }

    document.getElementById('rfqPrepare').style.display = 'none';
    document.getElementById('rfqReady').style.display = '';
    renderRfqVendors();
    renderRfqMessage();
}

function _renderRfqPrepareProgress() {
    const el = document.getElementById('rfqPrepareVendors');
    if (!el) return;
    el.innerHTML = rfqVendorData.filter(v => v.lookup_status !== 'ready' || v.needs_lookup).map(v => {
        const icon = v.lookup_status === 'loading' ? '⏳' : v.lookup_status === 'ready' ? '✅' : v.lookup_status === 'no_email' ? '❌' : '⏳';
        return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">${icon} <strong>${esc(v.display_name || v.vendor_name)}</strong></div>`;
    }).join('');
}

function renderRfqVendors() {
    const el = document.getElementById('rfqVendorList');
    el.innerHTML = rfqVendorData.map((v, i) => {
        let emailHtml;
        if (v.lookup_status === 'loading') {
            emailHtml = '<span class="email-loading">⏳ Looking up…</span>';
        } else if (v.lookup_status === 'no_email' || (!v.emails.length && v.lookup_status !== 'pending')) {
            emailHtml = `<div class="rfq-email-row">
                <span class="email-none">No email found</span>
                <input class="rfq-email-input" placeholder="Enter email…" onchange="rfqManualEmail(${i},this.value)">
                <button class="btn btn-danger btn-sm" onclick="rfqRemoveVendor(${i})" title="Remove">✕</button>
            </div>`;
        } else if (v.emails.length) {
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
        } else {
            emailHtml = '<span class="email-loading">⏳ Pending…</span>';
        }

        // Source indicator
        const srcLabels = { cached: '💾 Cached', website_scrape: '🌐 Website', ai_lookup: '🤖 AI' };
        const srcBadge = v.contact_source ? `<span class="rfq-src-badge">${srcLabels[v.contact_source] || v.contact_source}</span>` : '';

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

        return `<div class="rfq-vendor-row ${totalRepeats > 0 && (v.new_listing.length + v.new_other.length) === 0 && !v.include_repeats ? 'rfq-vendor-exhausted' : ''} ${!v.included ? 'rfq-vendor-excluded' : ''}">
            <input type="checkbox" ${v.included ? 'checked' : ''} onchange="rfqToggleVendor(${i})" class="rfq-vendor-cb" title="Include in RFQ">
            <div class="rfq-vendor-info">
                <strong>${esc(v.display_name || v.vendor_name)}</strong>
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
    document.getElementById('rfqSummary').textContent = summary;
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

    body += 'We are sourcing the following parts — please send your best offer if available:\n\n';
    body += allSendParts.map(p => {
        const subs = (rfqSubsMap[p] || []).filter(s => s.toUpperCase() !== p.toUpperCase());
        if (subs.length) {
            return `  ${p}  (also acceptable: ${subs.join(', ')})`;
        }
        return `  ${p}`;
    }).join('\n');
    body += '\n';

    if (condLine) body += '\n' + condLine;

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
    // Subject uses all unique parts across all vendors being sent
    const allParts = [...new Set(rfqAllParts)];
    const condTag = rfqCondition !== 'any' ? ` [${rfqCondition.toUpperCase()}]` : '';
    document.getElementById('rfqSubject').value = `RFQ: ${allParts.slice(0, 5).join(', ')}${allParts.length > 5 ? '…' : ''}${condTag} — ${currentReqName}`;

    // Preview body shows a sample for the first vendor with parts to send
    const sample = rfqVendorData.find(v => _vendorHasPartsToSend(v));
    if (sample) {
        document.getElementById('rfqBody').value = buildVendorBody(sample) || '';
    } else {
        document.getElementById('rfqBody').value = '(No vendors with new parts to send)';
    }
}


function rfqSelectEmail(idx, value) {
    if (value === '__custom__') {
        const custom = prompt('Enter email address:');
        if (custom && custom.includes('@')) {
            const email = custom.trim().toLowerCase();
            rfqVendorData[idx].selected_email = email;
            if (!rfqVendorData[idx].emails.includes(email)) {
                rfqVendorData[idx].emails.unshift(email);
            }
            apiFetch('/api/vendor-card/add-email', {
                method: 'POST', body: { vendor_name: rfqVendorData[idx].vendor_name, email }
            }).catch(() => showToast('Failed to save email', 'error'));
        }
        renderRfqVendors();
    } else {
        rfqVendorData[idx].selected_email = value;
    }
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
    if (!rfqVendorData.length) { closeModal('rfqModal'); return; }
    renderRfqVendors();
    renderRfqMessage();
}

async function sendBatchRfq() {
    const btn = document.getElementById('rfqSendBtn');
    await guardBtn(btn, 'Sending…', async () => {
        const subject = document.getElementById('rfqSubject').value;
        // Build per-vendor payloads with personalized body
        const sendable = rfqVendorData.filter(g => g.included && g.selected_email && _vendorHasPartsToSend(g));
        if (!sendable.length) { showToast('No vendors with email and new parts to send', 'error'); return; }
        const payload = sendable.map(g => {
            const body = buildVendorBody(g);
            // All parts being sent (for contact tracking)
            let sentParts = [...g.new_listing, ...g.new_other];
            if (g.include_repeats) sentParts = [...sentParts, ...g.repeat_listing, ...g.repeat_other];
            return {
                vendor_name: g.vendor_name, vendor_email: g.selected_email,
                parts: sentParts, subject, body
            };
        });
        try {
            const data = await apiFetch(`/api/requisitions/${currentReqId}/rfq`, {
                method: 'POST', body: { groups: payload }
            });
            const sent = (data.results || []).filter(r => r.status === 'sent').length;
            showToast(`${sent} of ${payload.length} emails sent successfully`, 'success');
            closeModal('rfqModal');
            selectedSightings.clear();
            // Clear sourcing drill-down state so next expand re-fetches fresh data
            if (_ddSelectedSightings[currentReqId]) delete _ddSelectedSightings[currentReqId];
            if (_ddSightingsCache[currentReqId]) delete _ddSightingsCache[currentReqId];
            renderSources();
            loadActivity();
        } catch (e) {
            showToast('Send error: ' + e.message, 'error');
        }
    });
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
async function openVendorPopup(cardId) {
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

    // Engagement Score (from Email Mining v2)
    if (card.engagement_score != null) {
        const engScore = Math.round(card.engagement_score);
        const engClass = engScore >= 70 ? 'eng-high' : engScore >= 40 ? 'eng-med' : 'eng-low';
        const respRate = card.total_outreach > 0 ? Math.round((card.total_responses / card.total_outreach) * 100) : null;
        html += `<div class="metrics-panel u-items-center">
            <div class="engagement-ring ${engClass}">${engScore}</div>
            <div style="flex:1;font-size:11px">
                <div style="font-weight:700;margin-bottom:2px">Engagement Score</div>
                <div style="color:var(--text2);display:flex;gap:10px;flex-wrap:wrap">
                    ${card.total_outreach != null ? `<span>Outreach: ${card.total_outreach}</span>` : ''}
                    ${card.total_responses != null ? `<span>Replies: ${card.total_responses}</span>` : ''}
                    ${respRate != null ? `<span>Rate: ${respRate}%</span>` : ''}
                    ${card.response_velocity_hours != null ? `<span>Avg: ${Math.round(card.response_velocity_hours)}h</span>` : ''}
                    ${card.ghost_rate != null ? `<span>Ghost: ${Math.round(card.ghost_rate * 100)}%</span>` : ''}
                </div>
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

    document.getElementById('vendorPopupContent').innerHTML = html;
    openModal('vendorPopup');

    // Load contacts, activities, metrics, and intel asynchronously
    loadVendorContacts(card.id);
    loadVendorActivities(card.id);
    loadVendorActivityStatus(card.id);
    loadVendorEmailMetrics(card.id);
    const intelEl = document.getElementById('vpIntelCard');
    if (intelEl && card.display_name) {
        loadCompanyIntel(card.display_name, vendorDomain, intelEl);
    }
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
    if (!confirm(`Delete vendor "${name}"? This cannot be undone.`)) return;
    try {
        await apiFetch(`/api/vendors/${cardId}`, { method: 'DELETE' });
        showToast('Vendor deleted', 'success');
        document.getElementById('vendorPopup').classList.remove('open');
        if (typeof loadVendorList === 'function') loadVendorList();
    } catch (e) { showToast('Failed to delete vendor: ' + e.message, 'error'); }
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
    const comment = document.getElementById('vpComment').value.trim();
    try {
        await apiFetch(`/api/vendors/${cardId}/reviews`, { method: 'POST', body: { rating: vpRating, comment } });
        vpRating = 0; openVendorPopup(cardId);
    } catch (e) { showToast('Failed to submit review', 'error'); }
}

async function vpToggleBlacklist(cardId, blacklisted) {
    const action = blacklisted ? 'blacklist' : 'remove from blacklist';
    if (!confirm(`Are you sure you want to ${action} this vendor?`)) return;
    try {
        await apiFetch(`/api/vendors/${cardId}/blacklist`, { method: 'POST', body: { blacklisted } });
        openVendorPopup(cardId);
        if (currentReqId && Object.keys(searchResults).length) renderSources();
    } catch (e) { showToast('Failed to update blacklist', 'error'); }
}

// ── Vendor Contacts CRUD ──────────────────────────────────────────────

async function loadVendorContacts(cardId) {
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
            return `<div class="si-contact" style="padding:6px 0;border-bottom:1px solid var(--border)">
                <div class="si-contact-info" style="flex:1;min-width:0">
                    <div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center">
                        <span class="si-contact-name">${esc(c.full_name || c.email)}</span>
                        ${srcBadge} ${confBadge} ${verBadge}
                    </div>
                    ${c.title ? '<div style="font-size:11px;color:var(--text2)">' + esc(c.title) + '</div>' : ''}
                    <div class="si-contact-meta">
                        ${c.email ? '<a href="mailto:' + escAttr(c.email) + '" onclick="autoLogEmail(\'' + escAttr(c.email) + '\',\'' + escAttr(c.full_name || '') + '\')">' + esc(c.email) + '</a>' : ''}
                        ${c.email && c.phone ? ' &middot; ' : ''}
                        ${c.phone ? '<a href="tel:' + escAttr(c.phone) + '" onclick="autoLogVendorCall(' + cardId + ',\'' + escAttr(c.phone) + '\')">' + esc(c.phone) + '</a>' : ''}
                    </div>
                    ${c.label ? '<div style="font-size:10px;color:var(--muted)">' + esc(c.label) + '</div>' : ''}
                </div>
                <div class="si-contact-actions" style="display:flex;gap:4px;align-items:center;flex-shrink:0">
                    <button class="btn btn-ghost btn-sm" onclick="openEditVendorContact(${cardId},${c.id})">Edit</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteVendorContact(${cardId},${c.id},'${escAttr(c.full_name || c.email)}')">✕</button>
                </div>
            </div>`;
        }).join('');
    } catch(e) { console.error('loadVendorContacts:', e); el.innerHTML = '<p class="vp-muted" style="font-size:11px">Error loading contacts</p>'; }
}

function openAddVendorContact(cardId) {
    document.getElementById('vcCardId').value = cardId;
    document.getElementById('vcContactId').value = '';
    document.getElementById('vendorContactModalTitle').textContent = 'Add Vendor Contact';
    ['vcFullName','vcTitle','vcEmail','vcPhone','vcLabel'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('vcLabel').value = 'Sales';
    openModal('vendorContactModal', 'vcEmail');
}

async function openEditVendorContact(cardId, contactId) {
    try {
        const contacts = await apiFetch('/api/vendors/' + cardId + '/contacts');
        const c = contacts.find(x => x.id === contactId);
        if (!c) { showToast('Contact not found', 'error'); return; }
        document.getElementById('vcCardId').value = cardId;
        document.getElementById('vcContactId').value = contactId;
        document.getElementById('vendorContactModalTitle').textContent = 'Edit Vendor Contact';
        document.getElementById('vcFullName').value = c.full_name || '';
        document.getElementById('vcTitle').value = c.title || '';
        document.getElementById('vcEmail').value = c.email || '';
        document.getElementById('vcPhone').value = c.phone || '';
        document.getElementById('vcLabel').value = c.label || '';
        openModal('vendorContactModal', 'vcFullName');
    } catch(e) { logCatchError('openEditVendorContact', e); showToast('Error loading contact', 'error'); }
}

async function saveVendorContact() {
    const cardId = document.getElementById('vcCardId').value;
    const contactId = document.getElementById('vcContactId').value;
    const body = {
        full_name: document.getElementById('vcFullName').value.trim() || null,
        title: document.getElementById('vcTitle').value.trim() || null,
        email: document.getElementById('vcEmail').value.trim(),
        phone: document.getElementById('vcPhone').value.trim() || null,
        label: document.getElementById('vcLabel').value.trim() || 'Sales',
    };
    if (!body.email) { showToast('Email is required', 'error'); return; }
    try {
        const url = contactId
            ? `/api/vendors/${cardId}/contacts/${contactId}`
            : `/api/vendors/${cardId}/contacts`;
        await apiFetch(url, { method: contactId ? 'PUT' : 'POST', body });
        closeModal('vendorContactModal');
        showToast(contactId ? 'Contact updated' : 'Contact added', 'success');
        loadVendorContacts(parseInt(cardId));
    } catch(e) { showToast('Failed to save contact', 'error'); }
}

async function deleteVendorContact(cardId, contactId, name) {
    if (!confirm('Remove contact "' + name + '"?')) return;
    try {
        await apiFetch(`/api/vendors/${cardId}/contacts/${contactId}`, { method: 'DELETE' });
        showToast('Contact removed', 'info');
        loadVendorContacts(cardId);
    } catch(e) { showToast('Failed to delete contact', 'error'); }
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


function autoLogEmail(email, contactName) {
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
    const cardId = document.getElementById('vlcCardId').value;
    const dur = parseInt(document.getElementById('vlcDuration').value);
    const data = {
        phone: document.getElementById('vlcPhone').value.trim() || null,
        contact_name: document.getElementById('vlcContactName').value.trim() || null,
        direction: document.getElementById('vlcDirection').value,
        duration_seconds: isNaN(dur) ? null : dur,
        notes: document.getElementById('vlcNotes').value.trim() || null,
    };
    if (window._vlcReqId) data.requisition_id = window._vlcReqId;
    try {
        await apiFetch('/api/vendors/' + cardId + '/activities/call', { method: 'POST', body: data });
        closeModal('vendorLogCallModal');
        showToast('Call logged', 'success');
        if (window._vlcReqId) { loadActivity(); }
        else { loadVendorActivities(parseInt(cardId)); }
        loadVendorActivityStatus(parseInt(cardId));
        window._vlcReqId = null;
    } catch(e) { console.error('saveVendorLogCall:', e); showToast('Error logging call', 'error'); }
}

function openVendorLogNoteModal(cardId, vendorName, reqId) {
    document.getElementById('vlnCardId').value = cardId;
    document.getElementById('vlnVendorName').textContent = vendorName;
    ['vlnContactName','vlnNotes'].forEach(id => document.getElementById(id).value = '');
    window._vlnReqId = reqId || null;
    openModal('vendorLogNoteModal', 'vlnNotes');
}

async function saveVendorLogNote() {
    const cardId = document.getElementById('vlnCardId').value;
    const notes = document.getElementById('vlnNotes').value.trim();
    if (!notes) { showToast('Note text is required', 'error'); return; }
    const data = {
        contact_name: document.getElementById('vlnContactName').value.trim() || null,
        notes: notes,
    };
    if (window._vlnReqId) data.requisition_id = window._vlnReqId;
    try {
        await apiFetch('/api/vendors/' + cardId + '/activities/note', { method: 'POST', body: data });
        closeModal('vendorLogNoteModal');
        showToast('Note added', 'success');
        if (window._vlnReqId) { loadActivity(); }
        else { loadVendorActivities(parseInt(cardId)); }
        loadVendorActivityStatus(parseInt(cardId));
        window._vlnReqId = null;
    } catch(e) { console.error('saveVendorLogNote:', e); showToast('Error adding note', 'error'); }
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
        showToast('Enrichment failed: ' + (e.message || e), 'error');
    }
}

// ── Vendors Tab ────────────────────────────────────────────────────────
let _vendorSortCol = null;
let _vendorSortDir = 'asc';

function _vendorSortArrow(col) {
    if (_vendorSortCol !== col) return '\u21c5';
    return _vendorSortDir === 'asc' ? '\u25b2' : '\u25bc';
}

function sortVendorList(col) {
    if (_vendorSortCol === col) {
        if (_vendorSortDir === 'asc') _vendorSortDir = 'desc';
        else { _vendorSortCol = null; _vendorSortDir = 'asc'; }
    } else {
        _vendorSortCol = col;
        _vendorSortDir = 'asc';
    }
    filterVendorList();
}

async function loadVendorList() {
    const q = (document.getElementById('vendorSearch') || {}).value || '';
    let resp;
    try { resp = await apiFetch(`/api/vendors?q=${encodeURIComponent(q)}`); }
    catch (e) { logCatchError('loadVendorList', e); showToast('Failed to load vendors', 'error'); return; }
    _vendorListData = resp.vendors || resp;
    filterVendorList();
}

function vendorTier(score) {
    if (score == null) return 'new';
    if (score >= 70) return 'proven';
    if (score >= 40) return 'developing';
    return 'caution';
}

function setVendorTier(tier, btn) {
    _vendorTierFilter = tier;
    document.querySelectorAll('#vendorTierPills .fp').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    filterVendorList();
}

function filterVendorList() {
    const hideBL = (document.getElementById('vendorHideBL') || {}).checked;
    const q = (document.getElementById('vendorSearch') || {}).value || '';
    let filtered = [..._vendorListData];
    if (q) {
        const lq = q.toLowerCase();
        filtered = filtered.filter(c => (c.display_name || '').toLowerCase().includes(lq));
    }
    if (_vendorTierFilter !== 'all') {
        filtered = filtered.filter(c => vendorTier(c.engagement_score) === _vendorTierFilter);
    }
    if (hideBL) filtered = filtered.filter(c => !c.is_blacklisted);

    // Sort by column or default (name A-Z)
    if (_vendorSortCol) {
        filtered.sort((a, b) => {
            let va, vb;
            switch (_vendorSortCol) {
                case 'name': va = (a.display_name || ''); vb = (b.display_name || ''); break;
                case 'tier': va = (a.engagement_score ?? -1); vb = (b.engagement_score ?? -1); break;
                case 'score': va = (a.engagement_score ?? -1); vb = (b.engagement_score ?? -1); break;
                case 'rating': va = (a.avg_rating ?? -1); vb = (b.avg_rating ?? -1); break;
                case 'sightings': va = (a.sighting_count || 0); vb = (b.sighting_count || 0); break;
                case 'email': va = ((a.emails || [])[0] || ''); vb = ((b.emails || [])[0] || ''); break;
                case 'last': va = (a.last_sighting_at || ''); vb = (b.last_sighting_at || ''); break;
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _vendorSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _vendorSortDir === 'asc' ? va - vb : vb - va;
        });
    } else {
        filtered.sort((a, b) => (a.display_name || '').localeCompare(b.display_name || ''));
    }

    const countEl = document.getElementById('vendorFilterCount');
    if (countEl) countEl.textContent = filtered.length < _vendorListData.length ? `${filtered.length} of ${_vendorListData.length}` : '';

    const el = document.getElementById('vendorList');
    if (!filtered.length) {
        el.innerHTML = `<p class="empty">${_vendorListData.length ? 'No vendors match filters' : 'No vendors yet \u2014 they\'ll appear here after your first search'}</p>`;
        return;
    }

    const thC = (col) => _vendorSortCol === col ? ' class="sorted"' : '';
    const sa = (col) => `<span class="sort-arrow">${_vendorSortArrow(col)}</span>`;
    const tierBadge = {proven:'b-proven',developing:'b-developing',caution:'b-caution',new:'b-new'};
    const tierLabel = {proven:'Proven',developing:'Developing',caution:'Caution',new:'New'};

    let html = `<div style="padding:0 16px"><table class="tbl"><thead><tr>
        <th onclick="sortVendorList('name')"${thC('name')}>Vendor ${sa('name')}</th>
        <th onclick="sortVendorList('tier')"${thC('tier')}>Tier ${sa('tier')}</th>
        <th onclick="sortVendorList('score')"${thC('score')}>Score ${sa('score')}</th>
        <th onclick="sortVendorList('rating')"${thC('rating')}>Rating ${sa('rating')}</th>
        <th onclick="sortVendorList('sightings')"${thC('sightings')}>Sightings ${sa('sightings')}</th>
        <th onclick="sortVendorList('email')"${thC('email')}>Email ${sa('email')}</th>
        <th onclick="sortVendorList('last')"${thC('last')}>Last Active ${sa('last')}</th>
    </tr></thead><tbody>`;

    for (const c of filtered) {
        const tier = vendorTier(c.engagement_score);
        const bc = c.is_blacklisted ? 'b-bl' : (tierBadge[tier] || 'b-new');
        const tl = c.is_blacklisted ? 'Blacklisted' : (tierLabel[tier] || 'New');
        const primaryEmail = (c.emails || [])[0] || '';
        const scoreText = c.engagement_score != null ? Math.round(c.engagement_score) : '\u2014';
        html += `<tr onclick="openVendorPopup(${c.id})">
            <td><b class="cust-link">${esc(c.display_name)}</b></td>
            <td><span class="badge ${bc}">${tl}</span></td>
            <td class="mono">${scoreText}</td>
            <td>${stars(c.avg_rating, c.review_count)}</td>
            <td class="mono">${c.sighting_count || 0}</td>
            <td style="font-size:11px;color:var(--text2)">${primaryEmail ? esc(primaryEmail) : '\u2014'}</td>
            <td style="font-size:11px;color:var(--muted)">${c.last_sighting_at ? fmtDate(c.last_sighting_at) : '\u2014'}</td>
        </tr>`;
    }

    html += '</tbody></table></div>';
    el.innerHTML = html;
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

async function loadMaterialList() {
    const q = (document.getElementById('materialSearch') || {}).value || '';
    let resp;
    try { resp = await apiFetch(`/api/materials?q=${encodeURIComponent(q)}`); }
    catch (e) { logCatchError('loadMaterialList', e); showToast('Failed to load materials', 'error'); return; }
    _materialListData = resp.materials || resp;
    renderMaterialList();
}

function renderMaterialList() {
    let data = [..._materialListData];
    const q = (document.getElementById('materialSearch') || {}).value || '';
    const el = document.getElementById('materialList');
    if (!data.length) {
        el.innerHTML = `<p class="empty">${q ? 'No materials match your search' : 'No material cards yet \u2014 they\'ll build automatically as you search'}</p>`;
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
        html += `<tr onclick="openMaterialPopup(${c.id})">
            <td><b class="cust-link">${esc(c.display_mpn)}</b></td>
            <td>${esc(c.manufacturer || '\u2014')}</td>
            <td class="mono">${c.vendor_count || 0}</td>
            <td class="mono" style="color:${c.best_price != null ? 'var(--green)' : 'var(--muted)'};font-weight:600">${bestPrice}</td>
            <td class="mono">${c.offer_count || 0}</td>
            <td class="mono">${c.search_count || 0}</td>
            <td style="font-size:11px;color:var(--muted)">${c.last_searched_at ? fmtDate(c.last_searched_at) : '\u2014'}</td>
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

    let html = `<div class="mp-header">
        <h2 onclick="editMaterialField(${card.id},'display_mpn',this)" style="cursor:pointer" title="Click to edit MPN">${esc(card.display_mpn)}</h2>
        <div class="mp-header-meta">
            ${card.manufacturer ? `<span onclick="editMaterialField(${card.id},'manufacturer',this)" style="font-weight:600;cursor:pointer" title="Click to edit">${esc(card.manufacturer)}</span> · ` : `<span onclick="editMaterialField(${card.id},'manufacturer',this)" style="cursor:pointer;color:var(--muted)" title="Click to add">+ Add manufacturer</span> · `}
            ${card.search_count} searches · Last searched ${card.last_searched_at ? fmtDate(card.last_searched_at) : 'never'}
            ${window.__isAdmin ? `<button class="btn btn-danger btn-sm" onclick="deleteMaterial(${card.id},'${escAttr(card.display_mpn)}')" style="margin-left:12px;font-size:10px">Delete</button>` : ''}
        </div>
    </div>`;

    html += `<div class="mp-section"><div class="mp-label">Description</div><div onclick="editMaterialField(${card.id},'description',this)" style="font-size:12px;cursor:pointer" title="Click to edit">${card.description ? esc(card.description) : '<span style="color:var(--muted)">+ Add description</span>'}</div></div>`;

    // ── Offers section ──
    const offers = card.offers || [];
    html += `<div class="mp-section"><div class="mp-label">Offers (${offers.length})</div>`;
    if (offers.length) {
        html += '<div class="mp-table-wrap"><table class="mp-tbl"><thead><tr><th>Vendor</th><th>Qty</th><th>Price</th><th>Lead Time</th><th>Condition</th><th>Status</th><th>Date</th></tr></thead><tbody>';
        for (const o of offers) {
            const statusCls = o.status === 'active' ? 'b-auth' : 'b-src';
            html += `<tr>
                <td class="mp-tbl-vendor">${esc(o.vendor_name)}</td>
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
    const sightings = card.sightings || [];
    html += `<div class="mp-section"><div class="mp-label">Sightings (${sightings.length})</div>`;
    if (sightings.length) {
        html += '<div class="mp-table-wrap"><table class="mp-tbl"><thead><tr><th>Vendor</th><th>Qty</th><th>Price</th><th>Source</th><th>Auth</th><th>Condition</th><th>Date</th></tr></thead><tbody>';
        for (const s of sightings) {
            html += `<tr>
                <td class="mp-tbl-vendor">${esc(s.vendor_name)}</td>
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

    document.getElementById('materialPopupContent').innerHTML = html;
    openModal('materialPopup');
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
    if (!confirm(`Delete material "${mpn}"? This cannot be undone.`)) return;
    try {
        await apiFetch(`/api/materials/${cardId}`, { method: 'DELETE' });
        showToast('Material deleted', 'success');
        document.getElementById('materialPopup').classList.remove('open');
        if (typeof loadMaterialList === 'function') loadMaterialList();
    } catch (e) { showToast('Failed to delete material: ' + e.message, 'error'); }
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
    document.getElementById('actStatSent').textContent = summary.sent || 0;
    document.getElementById('actStatReplied').textContent = summary.replied || 0;
    document.getElementById('actStatOpened').textContent = summary.opened || 0;
    document.getElementById('actStatAwaiting').textContent = summary.awaiting || 0;
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
            <div class="act-card-actions">${followUpBtn}${logBtns}${threadBtn}</div>
        </div>`;
    }).join('');
}

function fmtRelative(iso) {
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
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(26,127,155,.1);line-height:1.5;max-height:300px;overflow-y:auto">${c.body}</div>`
                : '';
            html += `<div data-searchable="${escAttr(searchText)}" style="margin-bottom:12px;padding:10px 14px;background:var(--teal-light);border-radius:8px;border:1px solid rgba(26,127,155,.15)">
                <div style="font-size:11px;color:var(--teal);font-weight:600;margin-bottom:4px">
                    ${c.contact_type === 'email' ? '✉ Sent' : '📞 Called'} · ${esc(c.vendor_contact||'')} · ${fmtDateTime(c.created_at)} · by ${esc(c.user_name||'')}
                </div>
                ${c.subject ? `<div style="font-size:12px;font-weight:600;margin-bottom:2px">${esc(c.subject)}</div>` : ''}
                <div style="font-size:11px;color:var(--text2)">${(c.parts_included||[]).join(', ')}</div>
                ${bodyHtml}
            </div>`;
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
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(16,185,129,.1);line-height:1.5;max-height:300px;overflow-y:auto">${r.body}</div>`
                : '';

            const searchText = searchParts.filter(Boolean).join(' ').toLowerCase();
            html += `<div data-searchable="${escAttr(searchText)}" style="margin-bottom:12px;padding:10px 14px;background:var(--green-light);border-radius:8px;border:1px solid rgba(16,185,129,.15)">
                <div style="font-size:11px;color:var(--green);font-weight:600;margin-bottom:4px">
                    Reply from ${esc(r.vendor_email||'')} · ${fmtDateTime(r.received_at)}
                </div>
                ${r.subject ? `<div style="font-size:12px;font-weight:600;margin-bottom:4px">${esc(r.subject)}</div>` : ''}
                ${parsedHtml}
                ${emailBodyHtml}
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
            const phoneStr = isCall && a.contact_phone ? (' · <a href="tel:' + escAttr(a.contact_phone) + '" style="color:inherit;text-decoration:underline"' + (v.vendor_card_id ? ' onclick="autoLogVendorCall(' + v.vendor_card_id + ',\'' + escAttr(a.contact_phone) + '\')"' : '') + '>' + esc(a.contact_phone) + '</a>') : '';
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
    document.getElementById('threadTitle').textContent = `Thread: ${vendorName}`;
    document.getElementById('threadContent').innerHTML = html;
    document.getElementById('threadSearch').value = '';
    document.getElementById('threadModal').classList.add('open');
    document.getElementById('threadSearch').focus();
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
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(26,127,155,.1);line-height:1.5;max-height:200px;overflow-y:auto">${c.body}</div>`
                : '';
            html += `<div style="margin-bottom:12px;padding:10px 14px;background:var(--teal-light);border-radius:8px;border:1px solid rgba(26,127,155,.15)">
                <div style="font-size:11px;color:var(--teal);font-weight:600;margin-bottom:4px">
                    To: ${esc(c.vendor_contact || '')} (${esc(c.vendor_name || '')}) · ${fmtDateTime(c.created_at)} · by ${esc(c.user_name || '')}
                </div>
                ${c.subject ? `<div style="font-size:12px;font-weight:600;margin-bottom:2px">${esc(c.subject)}</div>` : ''}
                <div style="font-size:11px;color:var(--text2)">${(c.parts_included || []).join(', ')}</div>
                ${bodyHtml}
            </div>`;
        }
    }
    document.getElementById('emailListTitle').textContent = `Sent Emails (${allSent.length})`;
    document.getElementById('emailListContent').innerHTML = html;
    document.getElementById('emailListModal').classList.add('open');
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
                ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px 10px;background:rgba(255,255,255,.6);border-radius:6px;border:1px solid rgba(16,185,129,.1);line-height:1.5;max-height:200px;overflow-y:auto">${r.body}</div>`
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
    document.getElementById('emailListTitle').textContent = `Replies Received (${allReplies.length})`;
    document.getElementById('emailListContent').innerHTML = html;
    document.getElementById('emailListModal').classList.add('open');
}

// ── Stock List Import ────────────────────────────────────────────────────
function toggleStockImport() {
    const el = document.getElementById('stockImportArea');
    el.style.display = el.style.display === 'none' ? '' : 'none';
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
    document.getElementById('stockFileReady').style.display = 'none';

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

    try {
        await apiFetch('/api/emails/reply', {
            method: 'POST',
            body: { conversation_id: conversationId, to: to, subject: subject, body: body }
        });
        showToast('Reply sent', 'success');
        // Refresh thread
        _emailThreadsLoaded = null;
        loadEmailThreads();
    } catch (e) {
        showToast('Failed to send reply: ' + e.message, 'error');
    }
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

// ── Sales Notifications ──────────────────────────────────────────────────
function toggleNotifications() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    const opening = !panel.classList.contains('open');
    panel.classList.toggle('open');
    if (opening) loadNotifications();
}

function _notifBadgeColor(type) {
    switch (type) {
        case 'vendor_reply_review': return '#3b82f6';
        case 'competitive_quote': case 'buyplan_approved': case 'buyplan_completed': return '#22c55e';
        case 'buyplan_rejected': return '#ef4444';
        case 'ownership_warning': case 'buyplan_pending': case 'buyplan_cancelled': return '#f59e0b';
        case 'proactive_match': return '#a855f7';
        default: return '#6b7280';
    }
}
function _notifLabel(type) {
    switch (type) {
        case 'ownership_warning': return 'Ownership';
        case 'vendor_reply_review': return 'Review';
        case 'competitive_quote': return 'Competitive';
        case 'proactive_match': return 'Proactive';
        case 'buyplan_pending': return 'Buy Plan';
        case 'buyplan_approved': return 'Approved';
        case 'buyplan_rejected': return 'Rejected';
        case 'buyplan_completed': return 'Completed';
        case 'buyplan_cancelled': return 'Cancelled';
        default: return type;
    }
}
function _notifClickAction(n) {
    if (n.requisition_id) return `markNotifRead(${n.id});loadRequisition(${n.requisition_id})`;
    if (n.company_id) return `markNotifRead(${n.id});goToCompany(${n.company_id})`;
    return `markNotifRead(${n.id})`;
}

async function loadNotifications() {
    const el = document.getElementById('notifList');
    if (!el) return;
    try {
        const data = await apiFetch('/api/sales/notifications');
        const items = Array.isArray(data) ? data : (data.notifications || []);
        if (!items.length) { el.innerHTML = '<p class="empty" style="font-size:12px">No notifications</p>'; return; }
        const header = `<div style="display:flex;justify-content:flex-end;padding:4px 0;border-bottom:1px solid var(--card2)">
            <button onclick="markAllNotifsRead()" style="font-size:11px;color:var(--teal);background:none;border:none;cursor:pointer;padding:2px 6px">Mark all read</button>
        </div>`;
        el.innerHTML = header + items.map(n => {
            const color = _notifBadgeColor(n.type);
            return `<div style="padding:6px 0;border-bottom:1px solid var(--card2);font-size:12px;cursor:pointer" onclick="${_notifClickAction(n)}">
                <div style="display:flex;align-items:center;gap:6px">
                    <span style="font-size:9px;font-weight:700;text-transform:uppercase;padding:1px 5px;border-radius:3px;color:#fff;background:${color}">${_notifLabel(n.type)}</span>
                    <span style="font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(n.subject || 'Notification')}</span>
                </div>
                <div style="display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin-top:2px">
                    <span>${esc(n.company_name || '')}</span>
                    <span>${n.created_at ? fmtDateTime(n.created_at) : ''}</span>
                </div>
            </div>`;
        }).join('');
    } catch { el.innerHTML = '<p class="empty" style="font-size:12px">Failed to load</p>'; }
}

async function markNotifRead(id) {
    try { await apiFetch(`/api/sales/notifications/${id}/read`, {method:'POST'}); } catch {}
    loadNotificationBadge();
}

async function markAllNotifsRead() {
    try { await apiFetch('/api/sales/notifications/read-all', {method:'POST'}); } catch {}
    loadNotifications();
    loadNotificationBadge();
}

async function loadNotificationBadge() {
    const badge = document.getElementById('notifBadge');
    if (!badge) return;
    try {
        const data = await apiFetch('/api/sales/notifications');
        const items = Array.isArray(data) ? data : (data.notifications || []);
        const count = items.length;
        badge.textContent = count;
        badge.style.display = count > 0 ? 'flex' : 'none';
    } catch { badge.style.display = 'none'; }
}

// Load notification badge on page init
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(loadNotificationBadge, 2000));
} else {
    setTimeout(loadNotificationBadge, 2000);
}

// "/" keyboard shortcut to focus search bar
document.addEventListener('keydown', function(e) {
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        e.preventDefault();
        const sb = document.getElementById('mainSearch');
        if (sb) sb.focus();
    }
});

// Global handler for unhandled promise rejections
window.addEventListener('unhandledrejection', function(event) {
    console.error('Unhandled promise rejection:', event.reason);
    if (typeof showToast === 'function') {
        showToast('Something went wrong — please try again', 'error');
    }
});
