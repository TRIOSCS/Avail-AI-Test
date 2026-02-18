/* AVAIL v1.2.0 — CRM, offers, quotes, target pricing */

// API versioning: all new fetch() calls should use api('/endpoint') instead of '/api/endpoint'.
// Old /api/ paths keep working via middleware rewrite.
const API_BASE = '/api/v1';
function api(path) { return API_BASE + path; }

let currentReqId = null;
let currentReqName = '';
let searchResults = {};
let searchResultsCache = {};  // keyed by reqId
let selectedSightings = new Set();
let rfqVendorData = [];
let activeTabCache = {};  // reqId → tab name
let _vendorListData = [];   // cached vendor list for client-side filtering
let _vendorTierFilter = 'all';  // all|proven|developing|caution|new
let expandedGroups = new Set();  // reqIds that are expanded (default: all collapsed)

// ── Shared Helpers ──────────────────────────────────────────────────────
async function apiFetch(url, opts = {}) {
    if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
        opts.headers = {'Content-Type': 'application/json', ...(opts.headers || {})};
        opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, opts);
    if (!res.ok) {
        const msg = await res.text().catch(() => res.statusText);
        throw Object.assign(new Error(msg), {status: res.status});
    }
    const ct = res.headers.get('content-type') || '';
    return ct.includes('json') ? res.json() : res.text();
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
        } catch (e) { console.error('autocomplete:', e); list.classList.remove('show'); }
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
    await loadRequisitions();
    // Restore last viewed requisition on page reload
    try {
        const lastId = parseInt(localStorage.getItem('lastReqId'));
        const lastName = localStorage.getItem('lastReqName') || '';
        if (lastId) {
            // Try to find it in the loaded list; if not, still open detail (it fetches its own data)
            const found = _reqListData.find(r => r.id === lastId);
            showDetail(lastId, found ? found.name : lastName);
        }
    } catch(e) {}
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
    // Proactive nav visible for sales + admin
    const pNav = document.getElementById('navProactive');
    if (pNav && (['sales','trader'].includes(window.userRole) || window.__isAdmin)) {
        pNav.style.display = '';
        refreshProactiveBadge();
    }
    // Performance nav visible to all
    const perfNav = document.getElementById('navPerformance');
    if (perfNav) perfNav.style.display = '';
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
    // Settings gear visible to admin and dev_assistant
    const settingsMenu = document.getElementById('settingsMenu');
    if (settingsMenu && (window.__isAdmin || window.__isDevAssistant)) settingsMenu.style.display = '';
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
    } catch (e) {}
}

// ── Navigation ──────────────────────────────────────────────────────────
const ALL_VIEWS = ['view-list', 'view-detail', 'view-vendors', 'view-materials', 'view-customers', 'view-buyplans', 'view-proactive', 'view-performance', 'view-settings'];

function showView(viewId) {
    for (const id of ALL_VIEWS) {
        document.getElementById(id).style.display = id === viewId ? '' : 'none';
    }
}

function showList() {
    showView('view-list');
    currentReqId = null;
    try { localStorage.removeItem('lastReqId'); localStorage.removeItem('lastReqName'); } catch(e) {}
    const searchInput = document.getElementById('reqSearchInput');
    if (searchInput) searchInput.value = '';
    loadRequisitions();
}

function openDetailSitePicker() {
    const picker = document.getElementById('detailSitePicker');
    if (picker.style.display === 'none') {
        picker.style.display = '';
        document.getElementById('detailSiteSearch').focus();
        if (typeof loadSiteOptions === 'function' && (!window._siteListCache || !window._siteListCache.length)) loadSiteOptions();
        filterDetailSiteTypeahead('');
    } else {
        picker.style.display = 'none';
    }
}

function filterDetailSiteTypeahead(query) {
    const list = document.getElementById('detailSiteList');
    if (!list) return;
    const cache = window._siteListCache || [];
    const q = query.toLowerCase().trim();
    const matches = q ? cache.filter(s => s.label.toLowerCase().includes(q)).slice(0, 8) : cache.slice(0, 8);
    list.innerHTML = matches.length
        ? matches.map(s => '<div class="site-typeahead-item" onclick="selectDetailSite('+s.id+',\''+s.label.replace(/'/g,"\\'")+'\')">' + (typeof esc === 'function' ? esc(s.label) : s.label) + '</div>').join('')
        : '<div class="site-typeahead-item" style="color:var(--muted)">No sites found</div>';
    list.classList.add('show');
}

async function selectDetailSite(siteId, label) {
    document.getElementById('detailSitePicker').style.display = 'none';
    try {
        await apiFetch(`/api/requisitions/${currentReqId}`, { method: 'PUT', body: { customer_site_id: siteId } });
        document.getElementById('detailCustomer').textContent = label;
        _reqCustomerMap[currentReqId] = label;
        const reqInfo = _reqListData.find(r => r.id === currentReqId);
        if (reqInfo) reqInfo.customer_site_id = siteId;
        showToast('Customer site linked', 'success');
    } catch (e) { showToast('Failed to link site', 'error'); }
}

function showDetail(id, name, tab) {
    currentReqId = id;
    currentReqName = name;
    try { localStorage.setItem('lastReqId', id); localStorage.setItem('lastReqName', name || ''); } catch(e) {}
    showView('view-detail');
    document.getElementById('detailTitle').textContent = name;
    // Show customer display — prompt to link if missing
    const custEl = document.getElementById('detailCustomer');
    if (custEl) {
        const custName = _reqCustomerMap[id];
        custEl.textContent = custName || '+ Link Customer';
        custEl.style.color = custName ? '' : 'var(--teal)';
    }
    // Hide site picker when switching requisitions
    const picker = document.getElementById('detailSitePicker');
    if (picker) picker.style.display = 'none';
    // Show Clone button only for archived/won/lost; set Submit vs Search All label
    const reqInfo = _reqListData.find(r => r.id === id);
    const cloneBtn = document.getElementById('cloneBtn');
    if (cloneBtn) {
        cloneBtn.style.display = (reqInfo && ['archived', 'won', 'lost'].includes(reqInfo.status)) ? '' : 'none';
    }
    const searchBtn = document.getElementById('searchAllBtn');
    if (searchBtn) {
        searchBtn.textContent = 'Search Selected';
    }
    // Set status chip (no pulse on initial load)
    const chip = document.getElementById('detailStatus');
    if (chip && reqInfo) {
        chip.className = 'status-chip status-' + reqInfo.status;
        chip.textContent = _statusLabels[reqInfo.status] || reqInfo.status;
    }
    // Restore cached results or load saved sightings from DB
    if (searchResultsCache[id]) {
        searchResults = searchResultsCache[id];
        renderSources();
    } else {
        searchResults = {};
        selectedSightings.clear();
        // Background fetch: load any previously saved sightings
        apiFetch(`/api/requisitions/${id}/sightings`)
            .then(data => {
                if (data && Object.keys(data).length && currentReqId === id) {
                    searchResults = data;
                    searchResultsCache[id] = data;
                    renderSources();
                }
            })
            .catch(() => {});  // Silent — empty Sources tab is fine
    }
    loadRequirements();
    loadActivity();
    // Set initial new-offers state from list data before loadOffers runs
    if (reqInfo && typeof _hasNewOffers !== 'undefined') {
        _hasNewOffers = reqInfo.has_new_offers || false;
        if (typeof _latestOfferAt !== 'undefined') _latestOfferAt = reqInfo.latest_offer_at || null;
    }
    if (typeof loadOffers === 'function') loadOffers();
    if (typeof loadQuote === 'function') loadQuote();
    // Restore last active tab or default to requirements
    const lastTab = tab || activeTabCache[id] || 'requirements';
    const tabMap = {requirements:0, sources:1, activity:2, offers:3, quote:4, emails:5};
    const tabBtns = document.querySelectorAll('#reqTabs .tab');
    switchTab(lastTab, tabBtns[tabMap[lastTab] || 0]);
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

function switchTab(name, btn) {
    document.querySelectorAll('.tc').forEach(t => t.classList.remove('on'));
    document.querySelectorAll('#reqTabs .tab').forEach(t => t.classList.remove('on'));
    document.getElementById('tab-' + name).classList.add('on');
    btn.classList.add('on');
    if (currentReqId) activeTabCache[currentReqId] = name;
    // Auto-load CRM tabs on first switch
    if (name === 'offers' && typeof loadOffers === 'function') loadOffers();
    if (name === 'quote' && typeof loadQuote === 'function') loadQuote();
    if (name === 'emails' && typeof loadEmailThreads === 'function') loadEmailThreads();
}

// ── Modals ──────────────────────────────────────────────────────────────
function openNewReqModal() {
    document.getElementById('newReqModal').classList.add('open');
    setTimeout(() => document.getElementById('nrName').focus(), 100);
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

const _statusLabels = {draft:'Draft',active:'Sourcing',offers:'Offers',quoting:'Quoting',quoted:'Quoted',won:'Won',lost:'Lost',archived:'Archived'};
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
let _reqStatusFilter = 'draft';
let _reqListSort = 'newest';
let _myReqsOnly = false;   // "My Reqs" toggle for non-sales roles

function setReqListSort(val) {
    _reqListSort = val;
    renderReqList();
}

function toggleMyReqs(btn) {
    _myReqsOnly = !_myReqsOnly;
    btn.classList.toggle('on', _myReqsOnly);
    renderReqList();
}

const debouncedReqListSearch = debounce(() => {
    const q = (document.getElementById('reqListFilter')?.value || '').trim();
    if (q.length >= 2) loadRequisitions(q);
    else if (q.length === 0) loadRequisitions();
    else renderReqList();  // Short input: client-side only
}, 300);

async function loadRequisitions(query = '') {
    try {
        const url = query ? `/api/requisitions?q=${encodeURIComponent(query)}` : '/api/requisitions';
        const resp = await apiFetch(url);
        _reqListData = resp.requisitions || resp;
        _reqListData.forEach(r => { if (r.customer_display) _reqCustomerMap[r.id] = r.customer_display; });
        renderReqList();
    } catch (e) { console.error('loadRequisitions:', e); }
}

function renderReqList() {
    const el = document.getElementById('reqList');
    const statusBar = document.getElementById('reqStatusBar');
    let data = _reqListData;
    // Apply status filter
    if (_reqStatusFilter === 'archive') {
        // Backend already returned only archived/won/lost
    } else if (_reqStatusFilter === 'quoted') {
        data = data.filter(r => r.status === 'quoting' || r.status === 'quoted');
    } else if (_reqStatusFilter === 'active') {
        data = data.filter(r => r.status === 'active');
    } else {
        data = data.filter(r => r.status === _reqStatusFilter);
    }
    // "My Reqs" filter
    if (_myReqsOnly && window.userId) {
        data = data.filter(r => r.created_by === window.userId);
    }
    // Text filter
    const q = (document.getElementById('reqListFilter')?.value || '').trim().toUpperCase();
    if (q) data = data.filter(r =>
        (r.name || '').toUpperCase().includes(q) ||
        (r.customer_display || '').toUpperCase().includes(q)
    );
    // Sort
    const sort = _reqListSort;
    if (sort === 'oldest') data = [...data].sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
    else if (sort === 'name-az') data = [...data].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    else if (sort === 'name-za') data = [...data].sort((a, b) => (b.name || '').localeCompare(a.name || ''));
    else if (sort === 'parts') data = [...data].sort((a, b) => (b.requirement_count || 0) - (a.requirement_count || 0));
    else if (sort === 'replies') data = [...data].sort((a, b) => (b.reply_count || 0) - (a.reply_count || 0));
    else if (sort === 'customer') data = [...data].sort((a, b) => (a.customer_display || '').localeCompare(b.customer_display || ''));
    else if (sort === 'last-searched') data = [...data].sort((a, b) => new Date(b.last_searched_at || 0) - new Date(a.last_searched_at || 0));
    else data = [...data].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

    // Always show status bar
    if (statusBar) statusBar.style.display = 'flex';
    const countEl = document.getElementById('reqStatusCount');
    if (countEl) countEl.textContent = q ? `${data.length} of ${_reqListData.length}` : `${data.length}`;

    if (!data.length) {
        const labels = {draft:'Draft',active:'Sourcing',offers:'Offers',quoted:'Quoted',archive:'Archive'};
        el.innerHTML = '<p class="empty">No ' + (labels[_reqStatusFilter] || _reqStatusFilter) + ' requisitions</p>';
        return;
    }
    el.innerHTML = data.map(r => {
        const isArchived = ['archived', 'won', 'lost'].includes(r.status);
        const archiveBtn = isArchived
            ? `<button class="btn-reactivate" onclick="event.stopPropagation();toggleArchive(${r.id})" title="Reactivate">↩ Activate</button>`
            : `<button class="btn-archive" onclick="event.stopPropagation();toggleArchive(${r.id})" title="Archive">📦 Archive</button>`;
        const createdBy = r.created_by_name ? `<span title="Created by ${esc(r.created_by_name)}">👤 ${esc(r.created_by_name)}</span>` : '';
        const custDisplay = r.customer_display ? `<span class="req-customer">${esc(r.customer_display)}</span>` : '';
        const statusBadge = r.status !== 'active' ? `<span class="status-badge status-${r.status}">${r.status}</span>` : '';
        // Reply count badge
        let replyBadge = '';
        if (r.reply_count > 0) {
            replyBadge = `<span class="badge" style="background:var(--bg3);color:var(--text2);font-size:10px;padding:2px 8px">💬 ${r.reply_count} repl${r.reply_count !== 1 ? 'ies' : 'y'}</span>`;
        }
        let newOffersDot = '';
        if (r.has_new_offers && r.latest_offer_at) {
            const hoursAgo = (Date.now() - new Date(r.latest_offer_at).getTime()) / 3600000;
            if (hoursAgo < 12) {
                newOffersDot = '<span class="new-offers-dot" title="New offers"></span>';
            } else if (hoursAgo < 96) {
                newOffersDot = '<span class="new-offers-dot red" title="New offers — unreviewed"></span>';
            }
            // > 96h: no dot shown (auto-clear)
        }
        const total = r.requirement_count || 0;
        const sourced = r.sourced_count || 0;
        const pct = total > 0 ? Math.round(sourced / total * 100) : 0;
        const progressBar = total > 0 ? `<div class="req-progress"><div class="req-progress-bar"><div class="req-progress-fill" style="width:${pct}%"></div></div><span class="req-progress-text">${sourced}/${total} sourced</span></div>` : '';
        return `
        <div class="card card-clickable ${isArchived ? 'req-archived' : ''}" onclick="showDetail(${r.id}, '${escAttr(r.name)}')">
            <div class="req-card">
                <div style="flex:1;min-width:0">
                    <div class="req-name">${esc(r.name)} ${statusBadge} ${newOffersDot}</div>
                    <div class="req-detail-row">
                        ${custDisplay}
                        ${progressBar}
                        ${replyBadge}
                    </div>
                </div>
                <div class="req-meta">
                    ${createdBy}
                    <span>${r.requirement_count} parts</span>
                    <span>${r.contact_count} contacts</span>
                    ${r.reply_count > 0 ? `<span>${r.reply_count} repl${r.reply_count !== 1 ? 'ies' : 'y'}</span>` : ''}
                    <span>${fmtDate(r.created_at)}</span>
                    ${archiveBtn}
                </div>
            </div>
        </div>`;
    }).join('');
}

function setReqStatusFilter(status, btn) {
    const wasArchive = _reqStatusFilter === 'archive';
    _reqStatusFilter = status;
    document.querySelectorAll('[data-req-status]').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    if (status === 'archive') {
        apiFetch('/api/requisitions?status=archive')
            .then(resp => {
                const data = resp.requisitions || resp;
                _reqListData = data;
                data.forEach(r => { if (r.customer_display) _reqCustomerMap[r.id] = r.customer_display; });
                renderReqList();
            })
            .catch(() => showToast('Failed to load archived requisitions', 'error'));
    } else if (wasArchive) {
        // Re-fetch full list — _reqListData was replaced by archive-only data
        loadRequisitions();
    } else {
        renderReqList();
    }
}

const searchRequisitions = debounce(query => loadRequisitions(query), 300);

async function sendFollowUp(contactId, vendorName) {
    if (!confirm(`Send follow-up email to ${vendorName}?`)) return;
    try {
        const data = await apiFetch(`/api/follow-ups/${contactId}/send`, { method: 'POST', body: {} });
        showToast(data.message || `Follow-up sent to ${vendorName}`, 'success');
        if (typeof loadActivity === 'function') loadActivity();
    } catch (e) { showToast('Failed to send follow-up', 'error'); }
}

async function createRequisition() {
    const name = document.getElementById('nrName').value.trim();
    if (!name) { showToast('Please enter a requisition name', 'error'); return; }
    const siteId = document.getElementById('nrSiteId')?.value || null;
    if (!siteId) { showToast('Please select a customer account', 'error'); return; }
    try {
        const data = await apiFetch('/api/requisitions', {
            method: 'POST', body: { name, customer_site_id: parseInt(siteId) }
        });
        closeModal('newReqModal');
        document.getElementById('nrName').value = '';
        document.getElementById('nrSiteSearch').value = '';
        document.getElementById('nrSiteId').value = '';
        document.getElementById('nrSiteSelected').style.display = 'none';
        document.getElementById('nrContactField').style.display = 'none';
        showDetail(data.id, data.name);
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
            const q = document.getElementById('reqSearchInput').value.trim();
            loadRequisitions(q);
        }
    } catch (e) { showToast('Failed to toggle archive', 'error'); }
}

// ── Requirements ────────────────────────────────────────────────────────
let reqData = []; // Cache for editing
let selectedRequirements = new Set(); // Track selected requirements for partial search

async function loadRequirements() {
    if (!currentReqId) return;
    try { reqData = await apiFetch(`/api/requisitions/${currentReqId}/requirements`); }
    catch(e) { console.error('loadRequirements:', e); return; }
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

function toggleAllReqSelection(checked) {
    if (checked) reqData.forEach(r => selectedRequirements.add(r.id));
    else selectedRequirements.clear();
    renderRequirementsTable();
    updateSearchAllBar();
}

let reqFilterType = 'all';
function setReqFilter(type, btn) {
    reqFilterType = type;
    document.querySelectorAll('[data-req-filter]').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    renderRequirementsTable();
}

function editReqCell(td, reqId, field) {
    if (td.querySelector('input')) return; // Already editing
    const r = reqData.find(x => x.id === reqId);
    if (!r) return;

    let currentVal;
    if (field === 'substitutes') currentVal = (r.substitutes || []).join(', ');
    else if (field === 'target_qty') currentVal = String(r.target_qty || 1);
    else if (field === 'target_price') currentVal = r.target_price != null ? String(r.target_price) : '';
    else currentVal = r[field] || '';

    const input = document.createElement('input');
    input.className = 'req-edit-input';
    input.value = currentVal;
    if (field === 'target_qty') { input.type = 'number'; input.min = '1'; input.style.width = '50px'; }
    if (field === 'target_price') { input.type = 'number'; input.step = '0.01'; input.min = '0'; input.style.width = '60px'; input.placeholder = '0.00'; }

    td.textContent = '';
    td.appendChild(input);
    input.focus();
    input.select();

    const save = async () => {
        const val = input.value.trim();
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
        await apiFetch(`/api/requirements/${reqId}`, { method: 'PUT', body });
        loadRequirements();
    };

    input.addEventListener('blur', save);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { loadRequirements(); }
    });
}

function editReqName(h2) {
    if (h2.querySelector('input')) return;
    const current = currentReqName || h2.textContent;
    const input = document.createElement('input');
    input.className = 'req-edit-input';
    input.value = current;
    input.style.cssText = 'font-size:18px;font-weight:700;width:300px';
    h2.textContent = '';
    h2.appendChild(input);
    input.focus();
    input.select();
    const save = async () => {
        const val = input.value.trim();
        if (!val || val === current) { h2.textContent = current; return; }
        try {
            await apiFetch(`/api/requisitions/${currentReqId}`, { method: 'PUT', body: { name: val } });
            currentReqName = val;
            h2.textContent = val;
            loadList();
        } catch(e) { h2.textContent = current; showToast('Failed to rename', 'error'); }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { h2.textContent = current; }
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
    for (const key of [...selectedSightings]) {
        if (key.startsWith(id + ':')) selectedSightings.delete(key);
    }
    if (currentReqId) searchResultsCache[currentReqId] = searchResults;
    loadRequirements();
}

function toggleUpload() {
    const el = document.getElementById('uploadArea');
    el.style.display = el.style.display === 'none' ? '' : 'none';
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

function submitOrSearch() {
    searchAll();
}

async function searchAll() {
    if (!currentReqId) return;
    if (!selectedRequirements.size) { alert('No parts selected'); return; }
    const btn = document.getElementById('searchAllBtn');
    btn.disabled = true; btn.textContent = 'Searching…';
    try {
        const body = { requirement_ids: [...selectedRequirements] };
        searchResults = await apiFetch(`/api/requisitions/${currentReqId}/search`, { method: 'POST', body });
        searchResultsCache[currentReqId] = searchResults;
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
        alert('Search error: ' + e.message);
    }
    btn.disabled = false; btn.textContent = 'Search Selected';
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

function setSrcFilter(type, btn) {
    srcFilterType = type;
    document.querySelectorAll('[data-src-filter]').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    renderSources();
}

function setSrcSort(val) {
    _srcSort = val;
    renderSources();
}

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
            const condBadge = cond ? `<span class="badge b-cond-${cond === 'NEW' ? 'new' : 'ref'}">${esc(cond)}</span>` : '';
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

            // Row 2 badges: collect all, show max 3 + overflow
            const excessBadge = (s.source_type || '').toLowerCase() === 'excess_list' ? '<span class="badge" style="background:#fef3c7;color:#92400e" title="Excess list from customer">EXCESS</span>' : '';
            const allBadges = [matchBadge, unavailBadge, excessBadge, s.is_authorized ? '<span class="badge b-auth">Auth</span>' : '', `<span class="badge b-src">${srcLabel}</span>`, condBadge, emailIndicator, histBadge, matHistBadge].filter(b => b);
            const visibleBadges = allBadges.slice(0, 3).join('');
            const overflowBadge = allBadges.length > 3 ? `<span class="sc-more-badge">+${allBadges.length - 3}</span>` : '';

            html += `<div class="card sc ${s.is_historical ? 'sc-hist' : ''} ${s.is_material_history ? 'sc-mathistory' : ''} ${isSub ? 'sc-sub' : ''} ${unavailClass}">
                ${isBuyer() ? `<input type="checkbox" ${checked} onchange="toggleSighting('${key}')">` : ''}
                <div class="sc-body">
                    <div class="sc-top">
                        <span class="sc-vendor" title="${escAttr(s.vendor_name)}">${esc(s.vendor_name)}</span>
                        ${ratingHtml}
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

function expandAllGroups() {
    for (const reqId of Object.keys(searchResults)) {
        expandedGroups.add(reqId);
    }
    renderSources();
}

function collapseAllGroups() {
    expandedGroups.clear();
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

function selectAllSightings() {
    for (const reqId of Object.keys(searchResults)) {
        const sightings = searchResults[reqId].sightings || [];
        for (let i = 0; i < sightings.length; i++) {
            const vn = (sightings[i].vendor_name || '').trim().toLowerCase();
            if (!sightings[i].is_unavailable && vn && vn !== 'no seller listed') {
                selectedSightings.add(`${reqId}:${i}`);
            }
        }
    }
    renderSources();
    updateBatchCount();
}

function clearSelection() {
    selectedSightings.clear();
    renderSources();
    updateBatchCount();
}

async function markUnavailable(sightingId, unavail) {
    try {
        await apiFetch(`/api/sightings/${sightingId}/unavailable`, {
            method: 'PUT', body: { unavailable: unavail }
        });
        // Update local state
        for (const reqId of Object.keys(searchResults)) {
            const sightings = searchResults[reqId].sightings || [];
            for (const s of sightings) {
                if (s.id === sightingId) {
                    s.is_unavailable = unavail;
                    break;
                }
            }
        }
        renderSources();
    } catch(e) { alert('Error: ' + e.message); }
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

async function openBatchRfqModal() {
    const groups = getSelectedByVendor();
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
        alert('Failed to prepare RFQ: ' + e.message);
        closeModal('rfqModal');
        return;
    }

    // Run lookups for vendors without emails (3-tier: cache → scrape → AI)
    const needsLookup = rfqVendorData.filter(v => v.lookup_status === 'pending');
    if (needsLookup.length) {
        document.getElementById('rfqPrepareStatus').textContent = `Finding contacts for ${needsLookup.length} vendor(s)…`;
        for (const v of needsLookup) {
            v.lookup_status = 'loading';
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
                v.contact_tier = data.tier || 0;
            } catch {
                v.lookup_status = 'no_email';
            }
            renderRfqVendors();
        }
    }

    document.getElementById('rfqPrepare').style.display = 'none';
    document.getElementById('rfqReady').style.display = '';
    renderRfqVendors();
    renderRfqMessage();
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
    btn.disabled = true; btn.textContent = 'Sending…';
    const subject = document.getElementById('rfqSubject').value;
    // Build per-vendor payloads with personalized body
    const sendable = rfqVendorData.filter(g => g.included && g.selected_email && _vendorHasPartsToSend(g));
    if (!sendable.length) { alert('No vendors with email and new parts to send'); btn.disabled = false; btn.textContent = 'Send'; return; }
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
        alert(`${sent} of ${payload.length} emails sent successfully`);
        closeModal('rfqModal');
        selectedSightings.clear();
        renderSources();
        loadActivity();
    } catch (e) {
        alert('Send error: ' + e.message);
    }
    btn.disabled = false; btn.textContent = 'Send';
}

// ── Click-to-Call Logging ───────────────────────────────────────────────
async function logCall(event, vendorName, vendorPhone, mpn) {
    try {
        await apiFetch('/api/contacts/phone', {
            method: 'POST', body: { requisition_id: currentReqId, vendor_name: vendorName,
                                   vendor_phone: vendorPhone, parts: mpn ? [mpn] : [] }
        });
        loadActivity();
    } catch (e) { console.error('Failed to log call:', e); }
}

// ── Vendor Card Popup ──────────────────────────────────────────────────
async function openVendorPopup(cardId) {
    let card;
    try { card = await apiFetch(`/api/vendors/${cardId}`); }
    catch (e) { console.error('Failed to load vendor:', e); return; }

    let html = `<div class="vp-header">
        <h2>${esc(card.display_name)}</h2>
        <div class="vp-rating">${stars(card.avg_rating, card.review_count)}</div>
    </div>`;

    // Blacklist toggle
    const blOn = card.is_blacklisted;
    html += `<div class="vp-section" style="padding-bottom:8px;margin-bottom:10px">
        <button class="btn-blacklist ${blOn ? 'vp-bl-on' : 'vp-bl-off'}" onclick="vpToggleBlacklist(${card.id}, ${!blOn})">
            ${blOn ? '🚫 Blacklisted' : 'Blacklist'}
        </button>
        ${blOn ? '<span style="font-size:10px;color:var(--red);margin-left:8px">This vendor is hidden from all search results</span>' : ''}
    </div>`;

    // Info
    html += '<div class="vp-section">';
    if (card.website) html += `<div class="vp-field"><span class="vp-label">Website</span> <a href="${escAttr(card.website)}" target="_blank">${esc(card.website)}</a></div>`;
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
        html += `<div style="display:flex;gap:12px;align-items:center;margin-top:10px;padding:8px 12px;background:var(--surface);border-radius:6px;border:1px solid var(--border)">
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

    // Intel Card container (loaded async)
    html += `<div id="vpIntelCard"></div>`;

    // Contacts (structured — loaded async)
    html += `<div class="vp-section">
        <div class="vp-label" style="display:flex;justify-content:space-between;align-items:center">
            Contacts
            <button class="btn btn-ghost btn-sm" onclick="openAddVendorContact(${card.id})">+ Add</button>
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
    document.getElementById('vendorPopup').classList.add('open');

    // Load contacts, activities, and intel asynchronously
    loadVendorContacts(card.id);
    loadVendorActivities(card.id);
    loadVendorActivityStatus(card.id);
    const intelEl = document.getElementById('vpIntelCard');
    if (intelEl && card.display_name) {
        loadCompanyIntel(card.display_name, vendorDomain, intelEl);
    }
}

let vpRating = 0;
function vpSetRating(n) {
    vpRating = n;
    document.querySelectorAll('#vpStarPicker .vp-star').forEach(el => {
        el.textContent = parseInt(el.dataset.n) <= n ? '★' : '☆';
    });
}

async function vpSubmitReview(cardId) {
    if (vpRating === 0) { alert('Please select a rating'); return; }
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
    document.getElementById('vendorContactModal').classList.add('open');
    setTimeout(() => document.getElementById('vcEmail').focus(), 100);
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
        document.getElementById('vendorContactModal').classList.add('open');
        setTimeout(() => document.getElementById('vcFullName').focus(), 100);
    } catch(e) { console.error('openEditVendorContact:', e); showToast('Error loading contact', 'error'); }
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
        el.innerHTML = `<span class="badge" style="background:color-mix(in srgb,${colors[d.status]} 15%,transparent);color:${colors[d.status]};font-size:9px;padding:1px 6px;border-radius:8px">${labels[d.status]}${daysText}</span>`;
    } catch(e) { console.error('loadVendorActivityStatus:', e); }
}

function openVendorLogCallModal(cardId, vendorName, reqId) {
    document.getElementById('vlcCardId').value = cardId;
    document.getElementById('vlcVendorName').textContent = vendorName;
    ['vlcPhone','vlcContactName','vlcDuration','vlcNotes'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('vlcDirection').value = 'outbound';
    window._vlcReqId = reqId || null;
    document.getElementById('vendorLogCallModal').classList.add('open');
    setTimeout(() => document.getElementById('vlcPhone').focus(), 100);
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
    document.getElementById('vendorLogNoteModal').classList.add('open');
    setTimeout(() => document.getElementById('vlnNotes').focus(), 100);
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
    } catch(e) {}
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
let _vendorSort = 'name-az';

function setVendorSort(val) {
    _vendorSort = val;
    filterVendorList();
}

async function loadVendorList() {
    const q = (document.getElementById('vendorSearch') || {}).value || '';
    let resp;
    try { resp = await apiFetch(`/api/vendors?q=${encodeURIComponent(q)}`); }
    catch (e) { console.error('Failed to load vendors:', e); return; }
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
    document.querySelectorAll('.vendor-filters .pill-filter').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    filterVendorList();
}

function filterVendorList() {
    const hideBL = (document.getElementById('vendorHideBL') || {}).checked;
    const q = (document.getElementById('vendorSearch') || {}).value || '';
    let filtered = _vendorListData;
    if (q) {
        const lq = q.toLowerCase();
        filtered = filtered.filter(c => (c.display_name || '').toLowerCase().includes(lq));
    }
    if (_vendorTierFilter !== 'all') {
        filtered = filtered.filter(c => vendorTier(c.engagement_score) === _vendorTierFilter);
    }
    if (hideBL) filtered = filtered.filter(c => !c.is_blacklisted);

    // Sort
    const vs = _vendorSort;
    if (vs === 'name-za') filtered.sort((a, b) => (b.display_name || '').localeCompare(a.display_name || ''));
    else if (vs === 'eng-desc') filtered.sort((a, b) => (b.engagement_score ?? -1) - (a.engagement_score ?? -1));
    else if (vs === 'eng-asc') filtered.sort((a, b) => (a.engagement_score ?? -1) - (b.engagement_score ?? -1));
    else if (vs === 'sightings') filtered.sort((a, b) => (b.sighting_count || 0) - (a.sighting_count || 0));
    else if (vs === 'rating') filtered.sort((a, b) => (b.avg_rating ?? -1) - (a.avg_rating ?? -1));
    else filtered.sort((a, b) => (a.display_name || '').localeCompare(b.display_name || ''));

    const countEl = document.getElementById('vendorFilterCount');
    if (countEl) countEl.textContent = filtered.length < _vendorListData.length ? `${filtered.length} of ${_vendorListData.length}` : '';

    const el = document.getElementById('vendorList');
    if (!filtered.length) {
        el.innerHTML = `<p class="empty">${_vendorListData.length ? 'No vendors match filters' : 'No vendors yet — they\'ll appear here after your first search'}</p>`;
        return;
    }
    el.innerHTML = filtered.map(c => {
        const tier = vendorTier(c.engagement_score);
        const tierLabel = {proven:'Proven',developing:'Developing',caution:'Caution',new:'New'}[tier];
        const tierCls = {proven:'eng-proven',developing:'eng-developing',caution:'eng-caution',new:'eng-new'}[tier];
        const scoreText = c.engagement_score != null ? `${Math.round(c.engagement_score)} · ${tierLabel}` : tierLabel;
        return `<div class="card card-clickable ${c.is_blacklisted ? 'vendor-card-bl' : ''}" onclick="openVendorPopup(${c.id})">
            <div class="vendor-card-row">
                <div class="vendor-card-name">${esc(c.display_name)} ${c.is_blacklisted ? '<span style="color:var(--red);font-size:10px">🚫 Blacklisted</span>' : ''}</div>
                <div class="vendor-card-meta">
                    <span class="eng-badge ${tierCls}">${scoreText}</span>
                    <span>${stars(c.avg_rating, c.review_count)}</span>
                    <span class="badge b-email" title="Emails on file">✉ ${(c.emails||[]).length}</span>
                    <span style="font-size:10px;color:var(--muted)">${c.sighting_count} sightings</span>
                </div>
            </div>
        </div>`;
    }).join('');
}

// ── Materials Tab ──────────────────────────────────────────────────────
let _materialListData = [];
let _materialSort = 'last-searched';

function setMaterialSort(val) {
    _materialSort = val;
    renderMaterialList();
}

async function loadMaterialList() {
    const q = (document.getElementById('materialSearch') || {}).value || '';
    let resp;
    try { resp = await apiFetch(`/api/materials?q=${encodeURIComponent(q)}`); }
    catch (e) { console.error('Failed to load materials:', e); return; }
    _materialListData = resp.materials || resp;
    renderMaterialList();
}

function renderMaterialList() {
    let data = [..._materialListData];
    const ms = _materialSort;
    if (ms === 'mpn-az') data.sort((a, b) => (a.display_mpn || '').localeCompare(b.display_mpn || ''));
    else if (ms === 'mpn-za') data.sort((a, b) => (b.display_mpn || '').localeCompare(a.display_mpn || ''));
    else if (ms === 'most-searched') data.sort((a, b) => (b.search_count || 0) - (a.search_count || 0));
    else if (ms === 'most-vendors') data.sort((a, b) => (b.vendor_count || 0) - (a.vendor_count || 0));
    else if (ms === 'oldest-search') data.sort((a, b) => new Date(a.last_searched_at || 0) - new Date(b.last_searched_at || 0));
    else data.sort((a, b) => new Date(b.last_searched_at || 0) - new Date(a.last_searched_at || 0));

    const q = (document.getElementById('materialSearch') || {}).value || '';
    const el = document.getElementById('materialList');
    if (!data.length) {
        el.innerHTML = `<p class="empty">${q ? 'No materials match your search' : 'No material cards yet — they\'ll build automatically as you search'}</p>`;
        return;
    }
    el.innerHTML = data.map(c => `
        <div class="card card-clickable" onclick="openMaterialPopup(${c.id})">
            <div class="mat-card-row">
                <div>
                    <div class="mat-card-mpn">${esc(c.display_mpn)}</div>
                    ${c.manufacturer ? `<div style="font-size:11px;color:var(--text2)">${esc(c.manufacturer)}</div>` : ''}
                </div>
                <div class="mat-card-meta">
                    <span class="badge b-mathistory">${c.vendor_count} vendor${c.vendor_count !== 1 ? 's' : ''}</span>
                    <span>${c.search_count} searches</span>
                    <span>${c.last_searched_at ? fmtDate(c.last_searched_at) : '—'}</span>
                </div>
            </div>
        </div>
    `).join('');
}

async function openMaterialPopup(cardId) {
    let card, pricingHistory;
    try { card = await apiFetch(`/api/materials/${cardId}`); }
    catch (e) { console.error('Failed to load material:', e); return; }

    // Fetch customer quote history for this MPN
    const mpn = card.display_mpn || card.normalized_mpn;
    try { pricingHistory = await apiFetch(`/api/pricing-history/${encodeURIComponent(mpn)}`); }
    catch { pricingHistory = { history: [] }; }

    let html = `<div class="mp-header">
        <h2>${esc(card.display_mpn)}</h2>
        <div class="mp-header-meta">
            ${card.manufacturer ? `<span style="font-weight:600">${esc(card.manufacturer)}</span> · ` : ''}
            ${card.search_count} searches · Last searched ${card.last_searched_at ? fmtDate(card.last_searched_at) : 'never'}
        </div>
    </div>`;

    if (card.description) {
        html += `<div class="mp-section"><div class="mp-label">Description</div><div style="font-size:12px">${esc(card.description)}</div></div>`;
    }

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
    document.getElementById('materialPopup').classList.add('open');
}

async function openVendorPopupByName(vendorName) {
    let resp;
    try { resp = await apiFetch(`/api/vendors?q=${encodeURIComponent(vendorName)}`); }
    catch (e) { console.error('Failed to load vendor:', e); return; }
    const data = resp.vendors || resp;
    if (data.length) {
        const exact = data.find(c => c.display_name.toLowerCase() === vendorName.toLowerCase());
        openVendorPopup(exact ? exact.id : data[0].id);
    }
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

function setActFilter(type, btn) {
    actFilterType = type;
    document.querySelectorAll('[data-act-filter]').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    renderActivityCards();
}

function setActStat(type, el) {
    // Sent and Replied tiles open detail modals
    if (type === 'all') { openSentEmailsModal(); return; }
    if (type === 'replied') { openRepliedEmailsModal(); return; }
    // Toggle — click same stat again to clear
    if (actStatFilter === type) {
        actStatFilter = null;
        document.querySelectorAll('.act-stat').forEach(s => s.classList.remove('on'));
    } else {
        actStatFilter = type;
        document.querySelectorAll('.act-stat').forEach(s => s.classList.remove('on'));
        el.classList.add('on');
    }
    renderActivityCards();
}

async function loadActivity() {
    if (!currentReqId) return;
    try {
        activityData = await apiFetch(`/api/requisitions/${currentReqId}/activity`);
    } catch {
        // Fallback to old endpoint
        let contacts;
        try { contacts = await apiFetch(`/api/requisitions/${currentReqId}/contacts`); }
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
    // Get the first requirement ID for this requisition
    const el = document.getElementById('emailsContent');
    if (!el) return;

    // Avoid reloading if already loaded for this req
    if (_emailThreadsLoaded === currentReqId && _emailThreadsData.length > 0) return;

    el.innerHTML = '<div class="spinner-row"><div class="spinner"></div> Loading email threads...</div>';

    try {
        // Get requirements for this requisition to find requirement IDs
        const reqs = await apiFetch(`/api/requisitions/${currentReqId}/requirements`);
        if (!reqs || !reqs.length) {
            el.innerHTML = '<p class="empty">No requirements — add parts first to see related emails</p>';
            return;
        }

        // Fetch threads for each requirement and merge
        const allThreads = new Map(); // conversation_id → thread
        for (const req of reqs) {
            try {
                const data = await apiFetch(`/api/requirements/${req.id}/emails`);
                if (data.error) {
                    el.innerHTML = `<p class="empty" style="color:var(--red)">${esc(data.error)}</p>`;
                    return;
                }
                for (const t of (data.threads || [])) {
                    if (!allThreads.has(t.conversation_id)) {
                        allThreads.set(t.conversation_id, t);
                    }
                }
            } catch (e) {
                if (e.status === 401) {
                    el.innerHTML = '<p class="empty" style="color:var(--red)">Could not load emails — M365 connection may need refresh</p>';
                    return;
                }
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
        showToast('Reply sent', 'ok');
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
