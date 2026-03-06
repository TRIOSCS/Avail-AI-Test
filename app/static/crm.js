/* AVAIL v1.2.0 — CRM Extension: Customers, Offers, Quotes */

import {
    apiFetch, debounce, esc, escAttr, logCatchError, showToast,
    fmtDate, fmtDateTime, fmtRelative, openModal, closeModal,
    showView, sidebarNav, navHighlight, autoLogEmail,
    initNameAutocomplete, notifyStatusChange, loadRequisitions,
    toggleDrillDown, guardBtn, openVendorPopup,
    loadVendorContacts, refreshProactiveBadge,
    currentReqId, setCurrentReqId,
    _renderAvailScoreTable,
} from 'app';

// ── Currency Formatting ─────────────────────────────────────────────────
function fmtCurrency(n) {
    const v = Math.abs(Number(n || 0));
    const sign = Number(n || 0) < 0 ? '-' : '';
    if (v >= 1e9) return sign + '$' + (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return sign + '$' + (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return sign + '$' + (v / 1e3).toFixed(1) + 'K';
    return sign + '$' + v.toFixed(2);
}

// ── Debounced CRM Handlers ─────────────────────────────────────────────
const _debouncedFilterSiteContacts = debounce((input, siteId) => filterSiteContacts(input, siteId), 150);
const _debouncedUpdateBpTotals = debounce(() => updateBpTotals(), 150);
const _debouncedUpdateProactivePreview = debounce(() => updateProactivePreview(), 150);
const _debouncedLoadVendorScorecards = debounce(() => loadVendorScorecards(), 300);

// ── CRM State ──────────────────────────────────────────────────────────
// Client-side company detail cache — avoids re-fetching when toggling drawers
const _companyDetailCache = {};  // {companyId: {data, ts}}
const _COMPANY_CACHE_TTL = 60_000; // 60 seconds

function _getCachedCompanyDetail(companyId) {
    const c = _companyDetailCache[companyId];
    if (c && (Date.now() - c.ts) < _COMPANY_CACHE_TTL) return c.data;
    return null;
}
function _setCachedCompanyDetail(companyId, data) {
    _companyDetailCache[companyId] = { data, ts: Date.now() };
}
function invalidateCompanyCache(companyId) {
    if (companyId) delete _companyDetailCache[companyId];
    else Object.keys(_companyDetailCache).forEach(k => delete _companyDetailCache[k]);
}

let crmCustomers = [];
let _custTotal = 0;
let _custOffset = 0;
const _CUST_PAGE_SIZE = 100;
let crmOffers = [];
let crmQuote = null;
let selectedOffers = new Set();
let _custUnassigned = false;
let _custSortCol = null;
let _custSortDir = 'asc';

function _custSortArrow(col) {
    if (_custSortCol !== col) return '\u21c5';
    return _custSortDir === 'asc' ? '\u25b2' : '\u25bc';
}

function sortCustList(col) {
    if (_custSortCol === col) {
        if (_custSortDir === 'asc') _custSortDir = 'desc';
        else { _custSortCol = null; _custSortDir = 'asc'; }
    } else {
        _custSortCol = col;
        _custSortDir = 'asc';
    }
    renderCustomers();
}

function autoLogCrmCall(phone, companyId) {
    const cid = companyId || _selectedCustId;
    const url = cid
        ? '/api/companies/' + cid + '/activities/call'
        : '/api/activities/call';
    apiFetch(url, {
        method: 'POST', body: { phone: phone, direction: 'outbound' }
    }).catch(function(e) { logCatchError('autoLogCrmCall', e); });
}

// ── Top bar view label ────────────────────────────────────────────────

function _setTopViewLabel(label) {
    const bc = document.getElementById('topBreadcrumb');
    const bcText = document.getElementById('topBreadcrumbText');
    const bcBack = bc?.querySelector('.breadcrumb-back');
    const bcSep = bc?.querySelector('.breadcrumb-sep');
    if (bc) bc.style.display = label ? 'flex' : 'none';
    if (bcText) bcText.textContent = label || '';
    // Hide back button + separator when just showing view name (not drilled down)
    if (bcBack) bcBack.style.display = 'none';
    if (bcSep) bcSep.style.display = 'none';
}

function _setTopDrillLabel(label, backLabel) {
    const bc = document.getElementById('topBreadcrumb');
    const bcText = document.getElementById('topBreadcrumbText');
    const bcBack = bc?.querySelector('.breadcrumb-back');
    const bcSep = bc?.querySelector('.breadcrumb-sep');
    const bcBackLabel = document.getElementById('breadcrumbBackLabel');
    if (bc) bc.style.display = 'flex';
    if (bcText) bcText.textContent = label || '';
    if (bcBackLabel) bcBackLabel.textContent = backLabel || 'Accounts';
    // Show back button + separator when drilled into a record
    if (bcBack) bcBack.style.display = '';
    if (bcSep) bcSep.style.display = '';
}

// ── Abort all CRM fetches (called from showView on tab switch) ────────
function _abortAllCrmFetches() {
    if (_custAbort) { try { _custAbort.abort(); } catch(e){} _custAbort = null; }
}

// ── Customer Filter / Sort / Drawer Helpers ───────────────────────────

let _custFilterMode = 'all';
let _custOwnerFilterId = null;  // null = all, number = specific user
let _custSelectedIds = new Set();

function setCustFilter(mode, btn) {
    _custFilterMode = (_custFilterMode === mode) ? 'all' : mode;
    document.querySelectorAll('#view-customers .chip-row .chip').forEach(c => c.classList.toggle('on', c.dataset.value === _custFilterMode));
    renderCustomers();
}

function setCustOwnerFilter(val) {
    _custOwnerFilterId = val ? parseInt(val) : null;
    renderCustomers();
}

async function _populateCustOwnerDropdown() {
    const sel = document.getElementById('custOwnerFilter');
    if (!sel) return;
    try {
        if (!_userListCache) {
            try { _userListCache = await apiFetch('/api/users/list'); } catch(e) { _userListCache = []; }
        }
        const roles = ['sales', 'trader', 'manager', 'admin'];
        const users = _userListCache.filter(u => roles.includes(u.role));
        sel.innerHTML = '<option value="">All Accounts</option>' +
            users.map(u => '<option value="' + u.id + '"' +
                (u.id === window.userId ? ' selected' : '') +
                '>' + esc(u.name) + '</option>').join('');
        // Default to current user
        if (window.userId) {
            _custOwnerFilterId = window.userId;
        }
    } catch(e) { /* ignore */ }
}

function toggleCustUnassigned(btn) {
    setCustFilter('unassigned', btn);
}

// ── Customers View ─────────────────────────────────────────────────────

async function showCustomers() {
    showView('view-customers');
    // Ensure flex display for the full-width layout
    const viewEl = document.getElementById('view-customers');
    if (viewEl) { viewEl.classList.remove('u-hidden'); viewEl.style.display = 'flex'; }
    setCurrentReqId(null);
    // Reset stale state from previous session
    _selectedCustId = null;
    _custFilterMode = 'all';
    document.querySelectorAll('#view-customers .chip-row .chip').forEach(c => c.classList.toggle('on', c.dataset.value === 'all'));
    // Populate owner filter dropdown
    _populateCustOwnerDropdown();
    // Show view indicator in top bar
    _setTopViewLabel('Accounts');
    // Role-based account filtering
    const isManagerOrAdmin = window.__isAdmin || ['manager','trader'].includes(window.userRole);
    const isSalesOnly = window.userRole === 'sales';
    const toggleLabel = document.getElementById('custMyOnlyLabel');
    const toggleInput = document.getElementById('custMyOnly');
    if (toggleLabel && toggleInput) {
        if (isManagerOrAdmin) {
            toggleLabel.classList.remove('u-hidden');
        } else {
            toggleLabel.classList.add('u-hidden');
            toggleInput.checked = true;
        }
    }
    await loadCustomers();
}

let _custAbort = null;
async function loadCustomers(append) {
    if (_custAbort) { try { _custAbort.abort(); } catch(e){} }
    _custAbort = new AbortController();
    var cl = document.getElementById('custList');
    if (!append) {
        _custOffset = 0;
        // skeleton rows use safe static HTML (no user input)
        if (cl && (!crmCustomers || !crmCustomers.length)) cl.innerHTML = window.skeletonRows ? window.skeletonRows(5) : '<div class="spinner-row"><div class="spinner"></div>Loading companies\u2026</div>';
    }
    try {
        const filter = document.getElementById('custFilter')?.value || '';
        const isSalesOnly = window.userRole === 'sales';
        const myOnly = document.getElementById('custMyOnly')?.checked;
        let url = '/api/companies?search=' + encodeURIComponent(filter) + '&limit=' + _CUST_PAGE_SIZE + '&offset=' + _custOffset;
        if ((isSalesOnly || myOnly) && window.userId) url += '&owner_id=' + window.userId;
        if (_custFilterMode === 'unassigned') url += '&unassigned=1';
        const [result, reqs] = await Promise.all([
            apiFetch(url, {signal: _custAbort.signal}).catch(e => { if (e.name !== 'AbortError') showToast('Failed to load accounts', 'error'); return {items:[],total:0}; }),
            apiFetch('/api/requisitions', {signal: _custAbort.signal}).catch(e => { if (e.name !== 'AbortError') showToast('Failed to load requisitions','warn'); return []; }),
        ]);
        const items = (result.items || result || []).filter(c =>
            !c.account_type || c.account_type.toLowerCase() !== 'vendor'
        );
        _custTotal = result.total || items.length;
        if (append) {
            crmCustomers = crmCustomers.concat(items);
        } else {
            crmCustomers = items;
        }
        _custOffset = crmCustomers.length;
        _enrichCustomersWithReqStats(crmCustomers, Array.isArray(reqs) ? reqs : []);
        renderCustomers();
    } catch (e) { if (e.name === 'AbortError') return; showToast('Failed to load customers', 'error'); console.error(e); }
}

async function loadMoreCustomers() {
    await loadCustomers(true);
}

function _enrichCustomersWithReqStats(companies, reqs) {
    // Build company id set for fast lookup
    const companyIds = new Set(companies.map(c => c.id));
    // Compute per-company: win rate and 90-day revenue
    const now = Date.now();
    const d90 = 90 * 86400000;
    const compStats = {};
    for (const r of reqs) {
        const cid = r.company_id || null;
        if (!cid || !companyIds.has(cid)) continue;
        if (!compStats[cid]) compStats[cid] = { won: 0, lost: 0, total: 0, revenue: 0, lastReqDate: null };
        const st = (r.status || '').toLowerCase();
        if (st === 'won' || st === 'lost') compStats[cid].total++;
        if (st === 'won') {
            compStats[cid].won++;
            if (r.created_at && (now - new Date(r.created_at).getTime()) < d90) {
                compStats[cid].revenue += parseFloat(r.won_revenue || r.total_value || 0);
            }
        }
        if (st === 'lost') compStats[cid].lost++;
        // Track last RFQ date
        if (r.created_at) {
            if (!compStats[cid].lastReqDate || r.created_at > compStats[cid].lastReqDate) {
                compStats[cid].lastReqDate = r.created_at;
            }
        }
    }
    for (const c of companies) {
        const s = compStats[c.id];
        if (s) {
            c.win_rate = s.total > 0 ? (s.won / s.total) * 100 : null;
            c.revenue_90d = s.revenue || null;
            c.last_req_date = s.lastReqDate || null;
        }
    }
}

async function goToCompany(companyId) {
    if (!companyId) return;
    showView('view-customers');
    const viewEl = document.getElementById('view-customers');
    if (viewEl) { viewEl.classList.remove('u-hidden'); viewEl.style.display = 'flex'; }
    setCurrentReqId(null);
    try {
        const result = await apiFetch('/api/companies');
        const items = result.items || result || [];
        crmCustomers = (Array.isArray(items) ? items : []).filter(c =>
            !c.account_type || c.account_type.toLowerCase() !== 'vendor'
        );
        _custTotal = result.total || crmCustomers.length;
        _custOffset = crmCustomers.length;
        renderCustomers();
    } catch (e) { showToast('Failed to load customers', 'error'); return; }
    setTimeout(() => openCustDrawer(companyId), 150);
    navHighlight(document.getElementById('navCustomers'));
}

let _selectedCustId = null;
let _currentCustTab = 'overview'; // persist tab across account switches
const _autoAnalyzedTags = new Set(); // companies already auto-analyzed this session

function _custHealthColor(c) {
    const enrichDays = window.daysSince ? window.daysSince(c.last_enriched_at) : 999;
    if (enrichDays <= 30) return 'green';
    if (enrichDays <= 90) return 'amber';
    return 'red';
}

function _custHealthLabel(c) {
    const enrichDays = window.daysSince ? window.daysSince(c.last_enriched_at) : 999;
    if (enrichDays <= 30) return 'Healthy';
    if (enrichDays <= 90) return 'Aging';
    if (enrichDays < 900) return 'At Risk';
    return 'New';
}

function renderCustomers() {
    const el = document.getElementById('custList');
    if (!el) return;
    const countEl = document.getElementById('custListCount');
    if (!crmCustomers.length) {
        // Safe static HTML - no user input
        el.innerHTML = '<p class="crm-empty">No accounts yet — click <b>+ New Account</b> to get started</p>';
        if (countEl) countEl.textContent = '';
        return;
    }
    if (window.__isMobile) { renderMobileAccountList(crmCustomers); return; }

    // Apply view filters
    let filtered = [...crmCustomers];
    // Owner filtering is now server-side via owner_id param; skip client filter
    if (_custFilterMode === 'strategic') filtered = filtered.filter(c => c.is_strategic);
    if (_custFilterMode === 'at-risk') filtered = filtered.filter(c => _custHealthColor(c) === 'red');
    if (_custFilterMode === 'stale') {
        const daysSince = window.daysSince || (() => 999);
        filtered = filtered.filter(c => daysSince(c.last_enriched_at) > 30);
    }

    // Sort
    if (_custSortCol) {
        filtered.sort((a, b) => {
            let va, vb;
            switch (_custSortCol) {
                case 'name': va = (a.name || ''); vb = (b.name || ''); break;
                case 'health': va = _custHealthColor(a) === 'green' ? 0 : _custHealthColor(a) === 'amber' ? 1 : 2; vb = _custHealthColor(b) === 'green' ? 0 : _custHealthColor(b) === 'amber' ? 1 : 2; break;
                case 'owner': va = (a.account_owner_name || 'zzz'); vb = (b.account_owner_name || 'zzz'); break;
                case 'sites': va = (a.site_count || 0); vb = (b.site_count || 0); break;
                case 'reqs': va = (a.open_req_count || 0); vb = (b.open_req_count || 0); break;
                case 'type': va = (a.account_type || 'zzz'); vb = (b.account_type || 'zzz'); break;
                case 'revenue': va = (a.revenue_90d || 0); vb = (b.revenue_90d || 0); break;
                case 'winrate': va = (a.win_rate || 0); vb = (b.win_rate || 0); break;
                case 'lastreq': va = (a.last_req_date || ''); vb = (b.last_req_date || ''); break;
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _custSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _custSortDir === 'asc' ? va - vb : vb - va;
        });
    } else {
        filtered.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    }

    if (countEl) countEl.textContent = filtered.length + (_custTotal > crmCustomers.length ? ' of ' + _custTotal : '') + ' accounts';

    // Build table
    const thSort = (col, label) => {
        const active = _custSortCol === col;
        const arrow = active ? (_custSortDir === 'asc' ? ' ▲' : ' ▼') : '';
        return `<th class="${active ? 'sorted' : ''}" onclick="sortCustList('${col}')">${label}<span class="sort-arrow">${arrow}</span></th>`;
    };

    let html = `<table class="crm-table">
        <thead><tr>
            <th class="td-check"><input type="checkbox" onchange="toggleAllCustCheckboxes(this)"></th>
            ${thSort('name', 'Account')}
            ${thSort('type', 'Type')}
            ${thSort('health', 'Health')}
            ${thSort('owner', 'Owner')}
            ${thSort('sites', 'Sites')}
            ${thSort('reqs', 'Open Reqs')}
            ${thSort('revenue', '90-day Revenue')}
            ${thSort('winrate', 'Win Rate')}
            ${thSort('lastreq', 'Last RFQ')}
        </tr></thead><tbody>`;

    for (const c of filtered) {
        const displayName = c.name.replace(/\s*(bucket|pass)\s*$/i, '').trim();
        const healthColor = _custHealthColor(c);
        const healthLabel = _custHealthLabel(c);
        const openReqs = c.open_req_count || 0;
        const checked = _custSelectedIds.has(c.id) ? ' checked' : '';
        const activeRow = _selectedCustId === c.id ? ' active-row' : '';
        const acctType = c.account_type || 'Standard';
        const typeColors = { Customer: '--blue', Prospect: '--amber', Partner: '--green', Competitor: '--red' };
        const typeColor = typeColors[acctType] || '--muted';
        const rev90 = c.revenue_90d != null ? '$' + Number(c.revenue_90d).toLocaleString(undefined, {minimumFractionDigits:0, maximumFractionDigits:0}) : '—';
        const winRate = c.win_rate != null ? Math.round(c.win_rate) + '%' : '—';

        html += `<tr class="${activeRow}" onclick="openCustDrawer(${c.id})" data-company-id="${c.id}">
            <td class="td-check"><input type="checkbox" onclick="event.stopPropagation();toggleCustCheckbox(${c.id},this)"${checked}></td>
            <td>
                <div style="display:flex;align-items:center;gap:8px">
                    <span style="font-weight:600;color:var(--text)">${esc(displayName)}</span>
                    ${c.is_strategic ? '<span title="Strategic" style="color:var(--amber);font-size:12px">★</span>' : ''}
                </div>
                ${c.domain ? '<div style="font-size:11px;color:var(--muted)">' + esc(c.domain) + '</div>' : ''}
            </td>
            <td><span style="background:var(${typeColor}-bg,#f1f5f9);color:var(${typeColor},#64748b);padding:1px 8px;border-radius:4px;font-size:10px;font-weight:600">${esc(acctType)}</span></td>
            <td><div class="health-indicator"><span class="health-dot health-dot-${healthColor}"></span><span class="health-indicator-label">${healthLabel}</span></div></td>
            <td>${c.account_owner_name ? '<span style="font-size:12px">' + esc(c.account_owner_name) + '</span>' : '<span style="font-size:11px;color:var(--muted)">—</span>'}</td>
            <td>${c.site_count || 0}</td>
            <td>${openReqs || '<span style="color:var(--muted)">0</span>'}</td>
            <td class="mono" style="color:var(--teal)">${rev90}</td>
            <td>${winRate !== '—' ? '<span style="font-weight:600;color:' + (c.win_rate >= 50 ? 'var(--green)' : c.win_rate >= 25 ? 'var(--amber)' : 'var(--red)') + '">' + winRate + '</span>' : '<span class="muted-cell">—</span>'}</td>
            <td class="muted-cell">${c.last_req_date ? getRelativeTime(c.last_req_date) : '—'}</td>
        </tr>`;
    }
    html += '</tbody></table>';

    // Load More button if there are more pages
    if (_custOffset < _custTotal) {
        html += '<div style="text-align:center;padding:12px"><button class="btn btn-ghost" onclick="loadMoreCustomers()">Load More (' + crmCustomers.length + ' of ' + _custTotal + ')</button></div>';
    }

    el.innerHTML = html;
}

function _renderCustCardMobile(c) {
    const displayName = c.name.replace(/\s*(bucket|pass)\s*$/i, '').trim();
    const healthColor = _custHealthColor(c);
    const openReqs = c.open_req_count || 0;
    const rev90 = c.revenue_90d != null ? '$' + Number(c.revenue_90d).toLocaleString(undefined, {minimumFractionDigits:0, maximumFractionDigits:0}) : '';
    const owner = c.account_owner_name || '';

    return `<div class="m-card m-health-${healthColor}" onclick="openCustDrawer(${c.id})">
        <div class="m-card-header">
            <span class="m-card-title">${esc(displayName)}${c.is_strategic ? ' <span style="color:var(--amber)">★</span>' : ''} ${_custEnrichBadge(c)}</span>
            <span class="m-card-chevron">›</span>
        </div>
        ${c.domain ? `<div class="m-card-subtitle">${esc(c.domain)}</div>` : ''}
        <div class="m-card-body">
            <span style="font-size:12px"><b>${c.site_count || 0}</b> sites</span>
            <span style="font-size:12px"><b>${openReqs}</b> open reqs</span>
            ${rev90 ? `<span style="font-size:12px;color:var(--green)">${rev90}</span>` : ''}
        </div>
        <div class="m-card-footer">
            <span class="m-card-meta">${owner ? esc(owner) : '<span style="color:var(--muted)">Unassigned</span>'}</span>
        </div>
    </div>`;
}

// ── Mobile Account List ───────────────────────────────────────────────
// Renders a card-based account list for mobile viewports.
// Called from renderCustomers() when window.__isMobile is true.
// Uses m-card CSS classes defined in mobile.css.

function renderMobileAccountList(companies) {
    const el = document.getElementById('custList');
    if (!el) return;
    const countEl = document.getElementById('custListCount');

    // Apply same view filters as desktop
    let filtered = [...companies];
    if (_custFilterMode === 'strategic') filtered = filtered.filter(c => c.is_strategic);
    if (_custFilterMode === 'at-risk') filtered = filtered.filter(c => _custHealthColor(c) === 'red');
    if (_custFilterMode === 'stale') {
        const daysSince = window.daysSince || (() => 999);
        filtered = filtered.filter(c => daysSince(c.last_enriched_at) > 30);
    }

    // Sort alphabetically by name (mobile default)
    if (_custSortCol) {
        filtered.sort((a, b) => {
            let va, vb;
            switch (_custSortCol) {
                case 'name': va = (a.name || ''); vb = (b.name || ''); break;
                case 'owner': va = (a.account_owner_name || 'zzz'); vb = (b.account_owner_name || 'zzz'); break;
                case 'reqs': va = (a.open_req_count || 0); vb = (b.open_req_count || 0); break;
                default: va = (a.name || ''); vb = (b.name || ''); break;
            }
            if (typeof va === 'string') return _custSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _custSortDir === 'asc' ? va - vb : vb - va;
        });
    } else {
        filtered.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    }

    if (countEl) countEl.textContent = filtered.length + (_custTotal > crmCustomers.length ? ' of ' + _custTotal : '') + ' accounts';

    // Build card list - all user content escaped via esc()
    let html = '';
    for (const c of filtered) {
        html += _renderCustCardMobile(c);
    }

    // Load More button if there are more pages
    if (_custOffset < _custTotal) {
        html += '<div style="text-align:center;padding:12px"><button class="m-card" style="text-align:center;font-weight:600;color:var(--blue);cursor:pointer" onclick="loadMoreCustomers()">Load More (' + crmCustomers.length + ' of ' + _custTotal + ')</button></div>';
    }

    // Safe: all user content in cards is escaped via esc() in _renderCustCardMobile
    el.innerHTML = html || '<p class="m-empty" style="text-align:center;padding:24px;color:var(--muted)">No accounts match filters</p>';
}

// ── Mobile Contact Card ───────────────────────────────────────────────
// Renders a single contact as a mobile-friendly card with tap-to-call
// and tap-to-email links. Used in the company drawer contacts tab on mobile.

function _renderMobileContact(ct, companyId) {
    const initials = (ct.full_name || '?').split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2);
    const location = [ct.site_city, ct.site_state].filter(Boolean).join(', ');

    // Build action links for phone and email (tap-friendly)
    let actionsHtml = '';
    if (ct.phone) {
        actionsHtml += phoneLink(ct.phone, {company_id: companyId || null, origin: 'crm_mobile_contact'});
    }
    if (ct.email) {
        actionsHtml += `<a href="mailto:${escAttr(ct.email)}" onclick="event.stopPropagation();autoLogEmail('${escAttr(ct.email)}','${escAttr(ct.full_name || '')}')" style="display:flex;align-items:center;gap:6px;padding:10px 14px;background:var(--blue-bg,#eff6ff);border-radius:8px;color:var(--blue,#2563eb);text-decoration:none;font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis">${esc(ct.email)}</a>`;
    }

    return `<div class="m-card" style="margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
            <div style="width:40px;height:40px;border-radius:50%;background:var(--blue-bg,#eff6ff);color:var(--blue,#2563eb);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;flex-shrink:0">${initials}</div>
            <div style="flex:1;min-width:0">
                <div class="m-card-title" style="margin:0">${esc(ct.full_name)}${ct.is_primary ? ' <span style="background:var(--blue);color:#fff;font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600;vertical-align:middle">Primary</span>' : ''}</div>
                ${ct.title ? `<div class="m-card-subtitle" style="margin:0">${esc(ct.title)}</div>` : ''}
                <div class="m-card-meta" style="margin:0">${esc(ct.site_name || '')}${location ? ' · ' + esc(location) : ''}</div>
            </div>
        </div>
        ${actionsHtml ? `<div style="display:flex;flex-direction:column;gap:6px">${actionsHtml}</div>` : ''}
    </div>`;
}

// ── Checkbox / Bulk Actions ───────────────────────────────────────────

function toggleCustCheckbox(companyId, cb) {
    if (cb.checked) _custSelectedIds.add(companyId);
    else _custSelectedIds.delete(companyId);
    _updateCustBulkBar();
}

function toggleAllCustCheckboxes(masterCb) {
    const checkboxes = document.querySelectorAll('#custList tbody .td-check input[type="checkbox"]');
    checkboxes.forEach(cb => {
        cb.checked = masterCb.checked;
        const row = cb.closest('tr');
        const cid = Number(row?.dataset?.companyId);
        if (cid) { if (masterCb.checked) _custSelectedIds.add(cid); else _custSelectedIds.delete(cid); }
    });
    _updateCustBulkBar();
}

function clearCustSelection() {
    _custSelectedIds.clear();
    document.querySelectorAll('#custList .td-check input[type="checkbox"]').forEach(cb => cb.checked = false);
    _updateCustBulkBar();
}

function _updateCustBulkBar() {
    const bar = document.getElementById('custBulkBar');
    const countEl = document.getElementById('custBulkCount');
    if (!bar) return;
    const n = _custSelectedIds.size;
    if (countEl) countEl.textContent = n;
    bar.classList.toggle('visible', n > 0);
}

async function bulkAssignOwner() {
    // Simple prompt for now
    const name = prompt('Assign owner (enter user ID):');
    if (!name) return;
    const ownerId = parseInt(name);
    if (isNaN(ownerId)) { showToast('Invalid user ID', 'error'); return; }
    for (const cid of _custSelectedIds) {
        try { await apiFetch('/api/companies/' + cid, { method: 'PUT', body: { account_owner_id: ownerId } }); }
        catch (e) { console.error('bulk assign error', cid, e); }
    }
    showToast(_custSelectedIds.size + ' accounts updated', 'success');
    clearCustSelection();
    loadCustomers();
}

function bulkExportAccounts() {
    const ids = [..._custSelectedIds];
    const data = crmCustomers.filter(c => ids.includes(c.id));
    const csv = ['Name,Industry,Owner,Sites,Domain'].concat(
        data.map(c => [c.name, c.industry || '', c.account_owner_name || '', c.site_count || 0, c.domain || ''].map(v => '"' + String(v).replace(/"/g, '""') + '"').join(','))
    ).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'accounts_export.csv'; a.click();
    URL.revokeObjectURL(url);
    showToast('Exported ' + data.length + ' accounts', 'success');
}

// ── Customer Drawer ───────────────────────────────────────────────────

async function openCustDrawer(companyId, tab) {
    _selectedCustId = companyId;
    // Highlight active row in table
    document.querySelectorAll('#custList tbody tr').forEach(r => {
        r.classList.toggle('active-row', Number(r.dataset.companyId) === companyId);
    });
    const backdrop = document.getElementById('custDrawerBackdrop');
    const drawer = document.getElementById('custDrawer');
    const viewEl = document.getElementById('view-customers');
    const miniList = document.getElementById('custMiniList');
    const tableWrap = document.getElementById('custTableWrap');
    const isDesktop = window.innerWidth > 768;

    // Split-pane: show mini-list on desktop
    if (isDesktop && miniList) {
        _renderMiniList(companyId);
        miniList.classList.add('open');
        if (viewEl) viewEl.classList.add('cust-split-active');
        if (tableWrap) tableWrap.style.display = 'none';
        if (backdrop) backdrop.classList.remove('open');
    } else {
        if (backdrop) backdrop.classList.add('open');
    }
    if (drawer) drawer.classList.add('open');
    // Show drill-down breadcrumb in top bar
    const comp = crmCustomers.find(x => x.id === companyId);
    _setTopDrillLabel(comp ? comp.name.replace(/\s*(bucket|pass)\s*$/i, '').trim() : 'Account');
    // Ensure company data is loaded (may be called from Contacts view before CRM tab)
    if (!crmCustomers.find(x => x.id === companyId)) {
        const cached = _getCachedCompanyDetail('_all');
        if (cached) {
            crmCustomers = cached;
        } else {
            try {
                const result = await apiFetch('/api/companies?search=');
                const items = result.items || result || [];
                crmCustomers = (Array.isArray(items) ? items : []).filter(c =>
                    c && typeof c === 'object' && c.id && c.name
                );
                _setCachedCompanyDetail('_all', crmCustomers);
            } catch (_e) { /* will show "Account not found" in drawer */ }
        }
    }
    const targetTab = tab || _currentCustTab || 'overview';
    switchCustDrawerTab(targetTab);
    // Highlight correct tab button
    const tabNames = ['overview', 'contacts', 'sites', 'activity', 'pipeline'];
    const tabIdx = tabNames.indexOf(targetTab);
    document.querySelectorAll('#custDrawerTabs .drawer-tab').forEach((t, i) => t.classList.toggle('active', i === tabIdx));
}

let _miniListIds = []; // ordered company IDs for keyboard nav

function _renderMiniListFromSearch() { _renderMiniList(_selectedCustId); }

function _renderMiniList(activeId) {
    const miniList = document.getElementById('custMiniList');
    if (!miniList) return;
    let filtered = [...crmCustomers];
    if (_custFilterMode === 'strategic') filtered = filtered.filter(c => c.is_strategic);
    if (_custFilterMode === 'healthy') filtered = filtered.filter(c => _custHealthColor(c) === 'green');
    if (_custFilterMode === 'at-risk') filtered = filtered.filter(c => _custHealthColor(c) === 'red');
    if (_custFilterMode === 'unassigned') filtered = filtered.filter(c => !c.account_owner_id);
    filtered.sort((a, b) => (a.name || '').localeCompare(b.name || ''));

    // Apply mini-list search filter
    const searchInput = miniList.querySelector('.cust-mini-search');
    const searchQ = (searchInput?.value || '').toLowerCase().trim();
    if (searchQ) {
        filtered = filtered.filter(c =>
            (c.name || '').toLowerCase().includes(searchQ) ||
            (c.account_owner_name || '').toLowerCase().includes(searchQ) ||
            (c.domain || '').toLowerCase().includes(searchQ)
        );
    }

    _miniListIds = filtered.map(c => c.id);

    // Build header (only on first render or if not present)
    let headerHtml = '';
    if (!miniList.querySelector('.cust-mini-list-header')) {
        headerHtml = `<div class="cust-mini-list-header">
            <input class="cust-mini-search" placeholder="Filter accounts..." oninput="_renderMiniListFromSearch()"
                onkeydown="_miniListKeyNav(event)">
            <div class="cust-mini-count">${filtered.length} account${filtered.length !== 1 ? 's' : ''}</div>
        </div>`;
    }

    let itemsHtml = '';
    for (const c of filtered) {
        const displayName = c.name.replace(/\s*(bucket|pass)\s*$/i, '').trim();
        const hc = _custHealthColor(c);
        const hl = _custHealthLabel(c);
        const isActive = c.id === activeId;
        const owner = c.account_owner_name || '';
        const strategic = c.is_strategic ? '<span style="color:var(--amber);font-size:10px" title="Strategic">★</span> ' : '';
        itemsHtml += `<div class="cust-mini-list-item${isActive ? ' active' : ''}" data-cid="${c.id}" onclick="openCustDrawer(${c.id})">
            <div class="cust-mini-list-info">
                <div class="cust-mini-list-name">${strategic}${esc(displayName)}</div>
                <div class="cust-mini-list-meta">
                    <span class="cust-mini-list-health ${hc}">${hl}</span>
                    ${owner ? '<span class="cust-mini-list-sub">' + esc(owner) + '</span>' : ''}
                </div>
            </div>
        </div>`;
    }

    if (headerHtml) {
        miniList.innerHTML = headerHtml + '<div class="cust-mini-list-scroll">' + itemsHtml + '</div>';
        // Preserve search value
    } else {
        const scrollEl = miniList.querySelector('.cust-mini-list-scroll');
        if (scrollEl) scrollEl.innerHTML = itemsHtml;
        const countEl = miniList.querySelector('.cust-mini-count');
        if (countEl) countEl.textContent = `${filtered.length} account${filtered.length !== 1 ? 's' : ''}`;
    }

    // Scroll active item into view
    const activeEl = miniList.querySelector('.cust-mini-list-item.active');
    if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

function _miniListKeyNav(event) {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    if (!_miniListIds.length || !_selectedCustId) return;
    const idx = _miniListIds.indexOf(_selectedCustId);
    let next;
    if (event.key === 'ArrowDown') next = idx < _miniListIds.length - 1 ? idx + 1 : 0;
    else next = idx > 0 ? idx - 1 : _miniListIds.length - 1;
    openCustDrawer(_miniListIds[next]);
}

function closeCustDrawer() {
    _selectedCustId = null;
    _currentCustTab = 'overview'; // reset tab on full close
    const backdrop = document.getElementById('custDrawerBackdrop');
    const drawer = document.getElementById('custDrawer');
    const viewEl = document.getElementById('view-customers');
    const miniList = document.getElementById('custMiniList');
    const tableWrap = document.getElementById('custTableWrap');
    if (backdrop) backdrop.classList.remove('open');
    if (drawer) drawer.classList.remove('open');
    if (miniList) miniList.classList.remove('open');
    if (viewEl) viewEl.classList.remove('cust-split-active');
    if (tableWrap) tableWrap.style.display = '';
    document.querySelectorAll('#custList tbody tr').forEach(r => r.classList.remove('active-row'));
    // Restore view label
    _setTopViewLabel('Accounts');
}

async function analyzeCustomerTags(companyId) {
    const btn = document.getElementById('analyzeTags-' + companyId);
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }
    try {
        const result = await apiFetch('/api/companies/' + companyId + '/analyze-tags', { method: 'POST' });
        _applyTagResult(companyId, result);
        showToast('Tags analyzed', 'success');
    } catch (e) {
        showToast('Tag analysis failed', 'error');
        logCatchError('analyzeCustomerTags', e);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Analyze Tags'; }
    }
}

async function _autoAnalyzeTags(companyId) {
    try {
        const result = await apiFetch('/api/companies/' + companyId + '/analyze-tags', { method: 'POST' });
        _applyTagResult(companyId, result);
    } catch (_e) {
        const container = document.getElementById('custTags-' + companyId);
        if (container) container.innerHTML = '<span style="font-size:11px;color:var(--muted)">No focus areas detected</span>';
    }
}

function _applyTagResult(companyId, result) {
    const c = crmCustomers.find(x => x.id === companyId);
    if (c) { c.brand_tags = result.brand_tags || []; c.commodity_tags = result.commodity_tags || []; }
    const container = document.getElementById('custTags-' + companyId);
    if (container) {
        const tags = (result.brand_tags || []).map(t => '<span class="tag tag-brand">' + esc(t) + '</span>').join('') +
            (result.commodity_tags || []).map(t => '<span class="tag tag-commodity">' + esc(t) + '</span>').join('');
        container.innerHTML = tags || '<span style="font-size:11px;color:var(--muted)">No focus areas detected</span>';
    }
}

async function _autoLoadAccountSummary(companyId) {
    const container = document.getElementById('custSummary-' + companyId);
    if (!container) return;
    try {
        const result = await apiFetch('/api/companies/' + companyId + '/summarize', { method: 'POST' });
        // Check if user navigated away
        if (_selectedCustId !== companyId) return;
        const headerEl = container.closest('.ai-summary-card')?.querySelector('h4');
        if (headerEl) headerEl.innerHTML = '✦ AI Account Intelligence';
        if (!result.situation && !result.development && (!result.next_steps || !result.next_steps.length)) {
            container.innerHTML = '<span style="color:var(--muted)">Not enough data to generate a summary yet. Add contacts, send RFQs, and log activity to build account intelligence.</span>';
            return;
        }
        let html = '';
        if (result.situation) {
            html += '<div class="ai-summary-section"><div class="ai-summary-label">Situation</div><div class="ai-summary-text">' + esc(result.situation) + '</div></div>';
        }
        if (result.development) {
            html += '<div class="ai-summary-section"><div class="ai-summary-label">Account Development</div><div class="ai-summary-text">' + esc(result.development) + '</div></div>';
        }
        if (result.next_steps && result.next_steps.length) {
            html += '<div class="ai-summary-section"><div class="ai-summary-label">Recommended Actions</div><ul style="margin:4px 0 0;padding-left:18px">';
            for (const step of result.next_steps) {
                html += '<li class="ai-summary-text">' + esc(step) + '</li>';
            }
            html += '</ul></div>';
        }
        container.innerHTML = html;
    } catch (_e) {
        if (_selectedCustId !== companyId) return;
        container.innerHTML = '<span style="color:var(--muted)">Could not generate summary</span>';
        const headerEl = container.closest('.ai-summary-card')?.querySelector('h4');
        if (headerEl) headerEl.innerHTML = '✦ AI Account Intelligence';
    }
}

async function switchCustDrawerTab(tab, btn) {
    _currentCustTab = tab; // persist for account switches
    document.querySelectorAll('#custDrawerTabs .drawer-tab').forEach(t => t.classList.remove('active'));
    if (btn) btn.classList.add('active');
    if (!_selectedCustId) return;
    // Lazy-load full detail (sites, contacts) when needed
    const needsDetail = (tab === 'sites' || tab === 'contacts' || tab === 'overview');
    if (needsDetail) await _ensureCompanyDetail(_selectedCustId);
    if (tab === 'overview') _renderCustDrawerOverview(_selectedCustId);
    else if (tab === 'contacts') _renderCustDrawerContacts(_selectedCustId);
    else if (tab === 'sites') _renderCustDrawerSites(_selectedCustId);
    else if (tab === 'activity') _renderCustDrawerActivity(_selectedCustId);
    else if (tab === 'pipeline') _renderCustDrawerPipeline(_selectedCustId);
    else if (tab === 'apollo') _renderCustDrawerApollo(_selectedCustId);
}

async function _renderCustDrawerApollo(companyId) {
    const body = document.getElementById('custDrawerBody');
    if (!body) return;
    body.innerHTML = '<div class="drawer-section"><div class="spinner-row"><div class="spinner"></div>Loading Apollo data…</div></div>';
    try {
        const data = await apiFetch('/api/apollo/credits');
        if (data && data.error) {
            console.error('Apollo credits fetch error:', data.error);
            const creditsEl = body.querySelector ? null : null;
            body.innerHTML = `<div class="drawer-section"><div class="drawer-section-title">Apollo</div><div class="drawer-field"><span class="drawer-field-label">Credits</span><span class="drawer-field-value" style="color:var(--muted)" title="Error: ${escAttr(String(data.error))}">unavailable</span></div></div>`;
        } else {
            const credits = (data && data.credits !== null && data.credits !== undefined) ? data.credits : null;
            body.innerHTML = `<div class="drawer-section"><div class="drawer-section-title">Apollo</div><div class="drawer-field"><span class="drawer-field-label">Credits</span><span class="drawer-field-value">${credits !== null ? credits : 'unavailable'}</span></div></div>`;
        }
    } catch (e) {
        logCatchError('_renderCustDrawerApollo', e);
        body.innerHTML = '<div class="drawer-section"><div class="drawer-field"><span class="drawer-field-label">Credits</span><span class="drawer-field-value" style="color:var(--muted)">unavailable</span></div></div>';
    }
}

async function _ensureCompanyDetail(companyId) {
    const c = crmCustomers.find(x => x.id === companyId);
    if (!c) return;
    if (c.sites) return; // already has detail data
    // Check client cache first
    const cached = _getCachedCompanyDetail(companyId);
    if (cached && cached.sites) {
        c.sites = cached.sites;
        c._detail = cached;
        return;
    }
    try {
        const detail = await apiFetch('/api/companies/' + companyId);
        c.sites = detail.sites || [];
        c._detail = detail;
        _setCachedCompanyDetail(companyId, detail);
    } catch (e) {
        logCatchError('_ensureCompanyDetail', e);
        c.sites = [];
    }
}

function _renderCustDrawerOverview(companyId) {
    const body = document.getElementById('custDrawerBody');
    const title = document.getElementById('custDrawerTitle');
    if (!body) return;
    const c = crmCustomers.find(x => x.id === companyId);
    if (!c) { body.innerHTML = '<p class="crm-empty">Account not found</p>'; return; }

    const displayName = c.name.replace(/\s*(bucket|pass)\s*$/i, '').trim();
    if (title) title.innerHTML = esc(displayName) + (c.is_strategic ? ' <span style="color:var(--amber)">★</span>' : '');
    const mTitle = document.getElementById('custDrawerMobileTitle');
    if (mTitle) mTitle.textContent = displayName;

    const healthColor = _custHealthColor(c);
    const healthLabel = _custHealthLabel(c);

    // Use denormalized counts (or fetch from detail if available)
    const totalContacts = c._detail ? (c._detail.sites || []).reduce((n, s) => n + (s.contacts || []).length, 0) : 0;
    const openReqs = c.open_req_count || 0;

    let html = `<div class="drawer-section" style="border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
            <div class="health-indicator"><span class="health-dot health-dot-${healthColor}"></span><span class="health-indicator-label" style="font-weight:600">${healthLabel}</span></div>
            <div style="display:flex;align-items:center;gap:6px">
                <label style="display:flex;align-items:center;gap:4px;font-size:11px;color:${c.is_strategic ? 'var(--amber)' : 'var(--muted)'};cursor:pointer;user-select:none" title="Strategic accounts have a 90-day inactivity window (vs 30-day)">
                    <input type="checkbox" ${c.is_strategic ? 'checked' : ''} onchange="toggleStrategic(${c.id},this.checked)" style="accent-color:var(--amber)"> ★ Strategic
                </label>
                <button class="btn btn-ghost btn-sm" onclick="openEditCompany(${c.id})">Edit</button>
                <button class="btn-enrich" onclick="unifiedEnrichCompany(${c.id})">Enrich</button>
            </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;text-align:center;margin-bottom:4px">
            <div><div style="font-size:22px;font-weight:700;color:var(--text)">${c.site_count || 0}</div><div style="font-size:10px;color:var(--muted)">Sites</div></div>
            <div><div style="font-size:22px;font-weight:700;color:var(--text)">${totalContacts}</div><div style="font-size:10px;color:var(--muted)">Contacts</div></div>
            <div><div style="font-size:22px;font-weight:700;color:var(--blue)">${openReqs}</div><div style="font-size:10px;color:var(--muted)">Open Reqs</div></div>
            <div><div style="font-size:22px;font-weight:700;color:var(--text)">${c.account_type || 'Standard'}</div><div style="font-size:10px;color:var(--muted)">Type</div></div>
        </div>
    </div>`;

    // AI Account Summary (auto-loaded)
    html += `<div class="drawer-section" id="custSummarySection-${c.id}" style="border-bottom:1px solid var(--border)">
        <div class="ai-summary-card">
            <h4><span class="spinner-dot"></span> AI Account Intelligence</h4>
            <div id="custSummary-${c.id}" style="color:var(--muted);font-size:11px">Analyzing account…</div>
        </div>
    </div>`;

    // Two-column grid for details + notes
    html += '<div class="drawer-overview-grid">';

    // Left column: Company details
    html += `<div class="drawer-section">
        <div class="drawer-section-title">Account Details</div>
        <div class="drawer-field"><span class="drawer-field-label">Owner</span><span class="drawer-field-value">${c.account_owner_name ? esc(c.account_owner_name) : '<span style="color:var(--red)">Unassigned</span>'}</span></div>
        <div class="drawer-field"><span class="drawer-field-label">Industry</span><span class="drawer-field-value">${esc(c.industry || '—')}</span></div>
        ${c.domain ? '<div class="drawer-field"><span class="drawer-field-label">Domain</span><span class="drawer-field-value"><a href="https://'+escAttr(c.domain)+'" target="_blank">'+esc(c.domain)+'</a></span></div>' : ''}
        ${c.website ? '<div class="drawer-field"><span class="drawer-field-label">Website</span><span class="drawer-field-value"><a href="'+escAttr(c.website)+'" target="_blank">'+esc(c.website)+'</a></span></div>' : ''}
        ${c.phone ? '<div class="drawer-field"><span class="drawer-field-label">Phone</span><span class="drawer-field-value">'+phoneLink(c.phone, {company_id: c.id, origin: 'company_drawer'})+'</span></div>' : ''}
        ${c.employee_size ? '<div class="drawer-field"><span class="drawer-field-label">Size</span><span class="drawer-field-value">'+esc(c.employee_size)+'</span></div>' : ''}
        ${c.hq_city ? '<div class="drawer-field"><span class="drawer-field-label">HQ</span><span class="drawer-field-value">'+esc(c.hq_city)+(c.hq_state ? ', '+esc(c.hq_state) : '')+'</span></div>' : ''}
        ${c.credit_terms ? '<div class="drawer-field"><span class="drawer-field-label">Credit Terms</span><span class="drawer-field-value">'+esc(c.credit_terms)+'</span></div>' : ''}
        ${c.linkedin_url ? '<div class="drawer-field"><span class="drawer-field-label">LinkedIn</span><span class="drawer-field-value"><a href="'+escAttr(c.linkedin_url)+'" target="_blank">View Profile</a></span></div>' : ''}
        <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px">
            <div class="drawer-section-title" style="margin-bottom:6px">Focus Areas</div>
            <div id="custTags-${c.id}">
                ${(c.brand_tags && c.brand_tags.length) || (c.commodity_tags && c.commodity_tags.length) ?
                    (c.brand_tags || []).map(t => '<span class="tag tag-brand">' + esc(t) + '</span>').join('') +
                    (c.commodity_tags || []).map(t => '<span class="tag tag-commodity">' + esc(t) + '</span>').join('')
                    : '<span class="ai-auto-loading" style="font-size:11px;color:var(--muted)"><span class="spinner-dot"></span> Analyzing focus areas…</span>'}
            </div>
        </div>
    </div>`;

    // Right column: Notes & recent activity
    html += `<div class="drawer-section">
        <div class="drawer-section-title">Account Notes</div>
        <div class="notes-panel">
            <textarea id="custNotes-${c.id}" rows="4" style="width:100%;resize:vertical;border:1px solid var(--border);border-radius:6px;padding:8px;font-size:12px;background:var(--white);font-family:inherit"
                placeholder="Add account notes..." onblur="saveCustNotes(${c.id})">${esc(c.notes || '')}</textarea>
            <div class="note-compose">
                <textarea id="custNewNote-${c.id}" rows="1" placeholder="Log a note..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();logCustNote(${c.id})}"></textarea>
                <button class="btn btn-sm btn-ghost" onclick="logCustNote(${c.id})">Log</button>
            </div>
            <div id="custRecentNotes-${c.id}" class="notes-log"></div>
        </div>
    </div>`;

    html += '</div>'; // close drawer-overview-grid

    body.innerHTML = html;
    // Load recent notes asynchronously
    _loadCustRecentNotes(c.id);
    // Auto-analyze tags if empty and not yet attempted this session
    const hasTags = (c.brand_tags && c.brand_tags.length) || (c.commodity_tags && c.commodity_tags.length);
    if (!hasTags && !_autoAnalyzedTags.has(c.id)) {
        _autoAnalyzedTags.add(c.id);
        _autoAnalyzeTags(c.id);
    }
    // Auto-load AI summary
    _autoLoadAccountSummary(c.id);
}

async function _renderCustDrawerSites(companyId) {
    const body = document.getElementById('custDrawerBody');
    if (!body) return;
    const c = crmCustomers.find(x => x.id === companyId);
    if (!c) return;

    const sites = c.sites || [];
    if (!sites.length) {
        body.innerHTML = `<div class="drawer-section"><p class="crm-empty">No sites — <a href="#" onclick="event.preventDefault();openAddSiteModal(${c.id},'${escAttr(c.name)}')">add one</a></p></div>`;
        return;
    }

    let html = `<div class="drawer-section" style="padding-bottom:8px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
            <div class="drawer-section-title" style="margin:0">${sites.length} Sites</div>
            <button class="btn btn-ghost btn-sm" onclick="openAddSiteModal(${c.id},'${escAttr(c.name)}')">+ Add Site</button>
        </div>`;

    for (const s of sites) {
        const contactCount = s.contact_count || 0;
        html += `<div class="site-accordion" id="siteAccordion-${s.id}">
            <div class="site-accordion-header" onclick="toggleSiteAccordion(${s.id})">
                <div class="site-accordion-title">
                    <span style="font-size:10px;transition:transform 0.2s" id="siteArrow-${s.id}">▶</span>
                    ${esc(s.site_name)}
                    ${s.owner_name ? '<span style="font-size:11px;color:var(--muted);font-weight:400">' + esc(s.owner_name) + '</span>' : ''}
                </div>
                <div class="site-accordion-meta">
                    ${s.open_reqs ? s.open_reqs + ' reqs' : ''}
                    ${s.city ? (s.open_reqs ? ' · ' : '') + esc(s.city) : ''}
                </div>
            </div>
            <div class="site-accordion-body" id="siteAccBody-${s.id}"></div>
        </div>`;
    }
    html += '</div>';
    body.innerHTML = html;
}

async function toggleSiteAccordion(siteId) {
    const bodyEl = document.getElementById('siteAccBody-' + siteId);
    const arrow = document.getElementById('siteArrow-' + siteId);
    if (!bodyEl) return;

    if (bodyEl.classList.contains('open')) {
        bodyEl.classList.remove('open');
        if (arrow) arrow.style.transform = '';
        return;
    }

    bodyEl.classList.add('open');
    if (arrow) arrow.style.transform = 'rotate(90deg)';

    // Load site details
    bodyEl.innerHTML = '<p class="empty" style="padding:8px;font-size:11px">Loading...</p>';
    try {
        const s = await apiFetch('/api/sites/' + siteId);
        const contacts = s.contacts || [];
        const sorted = [...contacts].sort((a, b) => {
            if (a.is_primary !== b.is_primary) return b.is_primary ? 1 : -1;
            return (a.full_name || '').localeCompare(b.full_name || '');
        });

        let html = '';

        // Contacts
        if (sorted.length) {
            html += '<div style="margin-bottom:10px">';
            for (const c of sorted) {
                const initials = (c.full_name || '?').split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2);
                const cStatus = c.contact_status || 'new';
                const CSTATUS = window.CONTACT_STATUS || {};
                const csCfg = CSTATUS[cStatus] || { label: cStatus, color: '#64748b', bg: '#e2e8f0' };
                const lastContact = c.last_contacted_at ? getRelativeTime(c.last_contacted_at) : '';
                const emailBadge = c.email_verified ? '<span style="color:#22c55e;font-size:9px;margin-left:2px" title="Verified">&#10003;</span>' : '';
                const phoneBadge = c.phone_verified ? '<span style="color:#22c55e;font-size:9px;margin-left:2px" title="Direct dial">&#9742;</span>' : '';
                html += `<div class="site-contact-row">
                    <div class="site-contact-avatar">${initials}</div>
                    <div class="site-contact-info">
                        <div class="site-contact-name">
                            ${esc(c.full_name)}${c.is_primary ? ' <span style="font-size:9px;color:var(--blue);font-weight:700">PRIMARY</span>' : ''}
                            <span style="background:${csCfg.bg};color:${csCfg.color};padding:1px 5px;border-radius:3px;font-size:9px;font-weight:600;margin-left:4px">${csCfg.label}</span>
                            ${_contactRoleBadge(c.contact_role)}
                            ${_enrichSourceBadge(c.enrichment_source)}
                        </div>
                        ${c.title ? '<div class="site-contact-title">' + esc(c.title) + '</div>' : ''}
                        ${c.email ? '<div style="font-size:10px;color:var(--muted)">' + esc(c.email) + emailBadge + '</div>' : ''}
                        ${c.phone ? '<div style="font-size:10px;color:var(--muted)">' + esc(c.phone) + phoneBadge + '</div>' : ''}
                        ${lastContact ? '<div style="font-size:10px;color:var(--muted)">Last contact: ' + lastContact + '</div>' : ''}
                    </div>
                    <div class="site-contact-actions">
                        ${c.email ? '<a href="mailto:'+escAttr(c.email)+'" title="Email" onclick="event.stopPropagation();autoLogEmail(\''+escAttr(c.email)+'\',\''+escAttr(c.full_name || '')+'\')">✉</a>' : ''}
                        ${c.phone ? '<a href="tel:'+escAttr(toE164(c.phone) || c.phone)+'" class="phone-link" onclick="logCallInitiated(this)" data-phone="'+escAttr(c.phone)+'" data-ctx="'+escAttr(JSON.stringify({company_id: _selectedCustId, customer_site_id: s.id, origin: 'site_contacts'}))+'" title="Call">📞</a>' : ''}
                        ${c.email && !c.email_verified ? '<a href="#" onclick="event.preventDefault();event.stopPropagation();verifyContactEmail('+c.id+',\''+escAttr(c.email)+'\')" title="Verify email" style="font-size:10px">Verify</a>' : ''}
                        <a href="#" onclick="event.preventDefault();event.stopPropagation();openEditSiteContact(${s.id},${c.id})">Edit</a>
                    </div>
                </div>`;
            }
            html += '</div>';
        } else {
            html += '<p style="font-size:11px;color:var(--muted);padding:4px 0">No contacts yet</p>';
        }

        // Site details
        html += `<div style="font-size:11px;color:var(--muted);display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px">
            ${s.payment_terms ? '<span>Terms: ' + esc(s.payment_terms) + '</span>' : ''}
            ${s.shipping_terms ? '<span>Ship: ' + esc(s.shipping_terms) + '</span>' : ''}
            ${s.city ? '<span>' + esc(s.city) + (s.state ? ', ' + esc(s.state) : '') + '</span>' : ''}
        </div>`;

        // Recent reqs
        if ((s.recent_reqs || []).length) {
            html += '<div style="margin-bottom:8px">';
            for (const r of s.recent_reqs.slice(0, 3)) {
                html += `<div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:11px;cursor:pointer" onclick="event.stopPropagation();sidebarNav('reqs');setTimeout(()=>toggleDrillDown(${r.id}),300)">
                    <span class="status-badge status-${r.status}" style="font-size:10px">${r.status}</span>
                    <span style="color:var(--text)">REQ-${String(r.id).padStart(3,'0')}</span>
                    <span style="color:var(--muted)">${r.requirement_count} MPNs</span>
                </div>`;
            }
            html += '</div>';
        }

        // Action buttons
        html += `<div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openAddSiteContact(${s.id})">+ Contact</button>
            <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openEditSiteModal(${s.id})">Edit Site</button>
        </div>`;

        bodyEl.innerHTML = html;
    } catch (e) {
        bodyEl.innerHTML = '<p class="empty" style="color:var(--red);font-size:11px">Failed to load site</p>';
    }
}

function _toggleActivityDetail(id) {
    const el = document.getElementById('actDetail-' + id);
    if (el) el.classList.toggle('open');
}

async function _renderCustDrawerActivity(companyId) {
    const body = document.getElementById('custDrawerBody');
    if (!body) return;
    body.innerHTML = '<div class="drawer-section"><p class="empty">Loading activity...</p></div>';
    try {
        const activities = await apiFetch('/api/companies/' + companyId + '/activities');
        const c = crmCustomers.find(x => x.id === companyId);
        if (!activities.length) {
            body.innerHTML = `<div class="drawer-section"><p class="crm-empty">No activity recorded — send an RFQ or log a note to get started</p>
                ${c ? '<button class="btn btn-ghost btn-sm" onclick="openLogNoteModal('+c.id+',\''+escAttr(c.name)+'\')">+ Add Note</button>' : ''}
            </div>`;
            return;
        }
        const actIcon = window.activityIcon || (() => '');
        const relTime = window.getRelativeTime || (() => '');

        let html = '<div style="padding:12px 20px">';
        html += `<div class="activity-feed">`;
        for (const a of activities.slice(0, 30)) {
            const label = (a.activity_type || '').replace(/_/g, ' ');
            const dur = a.duration_seconds ? ' (' + Math.round(a.duration_seconds / 60) + 'm)' : '';
            const typeClass = 'activity-icon-' + (a.activity_type === 'email' ? 'email' : a.activity_type === 'call' ? 'call' : a.activity_type === 'note' ? 'note' : 'system');
            const isEmail = (a.activity_type || '').includes('email');
            html += `<div class="activity-item">
                <div class="activity-icon ${typeClass}">${actIcon(a.activity_type)}</div>
                <div class="activity-content">
                    <div class="activity-title">${esc(a.summary || label + dur)}</div>
                    ${isEmail && a.subject ? '<div class="activity-subject">' + esc(a.subject) + '</div>' : ''}
                    <div class="activity-detail">${esc(a.user_name || '')}</div>
                    ${isEmail && (a.subject || a.contact_email || a.notes) ? '<button class="activity-view-pill" onclick="_toggleActivityDetail('+a.id+')">View</button><div class="activity-email-detail" id="actDetail-'+a.id+'">'
                        + (a.subject ? '<div><span class="activity-email-label">Subject:</span>' + esc(a.subject) + '</div>' : '')
                        + (a.contact_email ? '<div><span class="activity-email-label">Contact:</span>' + esc(a.contact_email) + '</div>' : '')
                        + (a.notes ? '<div><span class="activity-email-label">Notes:</span>' + esc(a.notes) + '</div>' : '')
                        + '</div>' : ''}
                </div>
                <span class="activity-time">${relTime(a.created_at)}</span>
            </div>`;
        }
        html += '</div></div>';
        body.innerHTML = html;
    } catch (e) {
        body.innerHTML = '<div class="drawer-section"><p class="crm-empty" style="color:var(--red)">Failed to load activity</p></div>';
    }
}

async function _renderCustDrawerPipeline(companyId) {
    const body = document.getElementById('custDrawerBody');
    if (!body) return;
    const c = crmCustomers.find(x => x.id === companyId);
    if (!c) return;

    // Gather all reqs from all sites
    const siteIds = (c.sites || []).map(s => s.id);
    body.innerHTML = '<div class="drawer-section"><p class="empty">Loading pipeline...</p></div>';

    try {
        // Fetch site details for recent reqs
        const sitePromises = siteIds.slice(0, 20).map(sid => apiFetch('/api/sites/' + sid).catch(() => null));
        const siteResults = await Promise.all(sitePromises);
        const allReqs = [];
        for (const s of siteResults) {
            if (s && s.recent_reqs) {
                for (const r of s.recent_reqs) {
                    allReqs.push({ ...r, site_name: s.site_name });
                }
            }
        }

        if (!allReqs.length) {
            body.innerHTML = '<div class="drawer-section"><p class="crm-empty">No requisitions for this account — create one from the main RFQ list</p></div>';
            return;
        }

        // Group by status
        const groups = { open: [], quoted: [], won: [], lost: [], archived: [] };
        for (const r of allReqs) {
            const st = (r.status || 'open').toLowerCase();
            if (groups[st]) groups[st].push(r);
            else groups.open.push(r);
        }

        // Win/loss summary
        const wonCount = groups.won.length;
        const lostCount = groups.lost.length;
        const totalDecided = wonCount + lostCount;
        let html = '<div style="padding:12px 20px">';
        if (totalDecided > 0) {
            const winPct = Math.round((wonCount / totalDecided) * 100);
            const winColor = winPct >= 50 ? 'var(--green)' : winPct >= 25 ? 'var(--amber)' : 'var(--red)';
            html += `<div style="display:flex;gap:16px;align-items:center;padding:8px 12px;background:var(--bg);border-radius:8px;margin-bottom:12px;font-size:12px">
                <span style="font-weight:700;color:${winColor}">${winPct}% win rate</span>
                <span style="color:var(--green)">${wonCount} won</span>
                <span style="color:var(--red)">${lostCount} lost</span>
                <span style="color:var(--muted)">${groups.open.length + groups.quoted.length} in progress</span>
            </div>`;
        }
        for (const [status, reqs] of Object.entries(groups)) {
            if (!reqs.length) continue;
            const color = status === 'won' ? 'var(--green)' : status === 'lost' ? 'var(--red)' : status === 'quoted' ? 'var(--amber)' : 'var(--blue)';
            html += `<div style="margin-bottom:16px">
                <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:var(--muted);margin-bottom:6px;display:flex;align-items:center;gap:6px">
                    <span style="width:8px;height:8px;border-radius:50%;background:${color}"></span>
                    ${status} (${reqs.length})
                </div>`;
            for (const r of reqs) {
                html += `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);cursor:pointer" onclick="goToReq(${r.id},{view:'customers',companyId:${companyId},label:'${esc(c.name || 'Account').replace(/'/g, "\\'")} Pipeline'})">
                    <span style="font-size:12px;font-weight:600;color:var(--blue)">REQ-${String(r.id).padStart(3,'0')}</span>
                    <span style="font-size:12px;color:var(--text);flex:1">${esc(r.name || '')}</span>
                    <span style="font-size:11px;color:var(--muted)">${r.requirement_count || 0} MPNs</span>
                </div>`;
            }
            html += '</div>';
        }
        html += '</div>';
        body.innerHTML = html;
    } catch (e) {
        body.innerHTML = '<div class="drawer-section"><p class="crm-empty" style="color:var(--red)">Failed to load pipeline</p></div>';
    }
}

// Legacy compat
function selectCustomer(companyId) { openCustDrawer(companyId); }
function renderCustomerDetail(companyId) { openCustDrawer(companyId); }

async function toggleStrategic(companyId, checked) {
    try {
        await apiFetch('/api/companies/' + companyId, {
            method: 'PUT', body: { is_strategic: checked }
        });
        // Update local cache
        const c = crmCustomers.find(x => x.id === companyId);
        if (c) c.is_strategic = checked;
        showToast(checked ? 'Marked as strategic (90-day window)' : 'Removed strategic flag (30-day window)');
    } catch (e) {
        logCatchError('toggleStrategic', e);
        showToast('Failed to update', 'error');
    }
}

async function saveCustNotes(companyId) {
    const el = document.getElementById('custNotes-' + companyId);
    if (!el) return;
    try {
        await apiFetch('/api/companies/' + companyId, {
            method: 'PUT', body: { notes: el.value }
        });
    } catch (e) { logCatchError('saveCustNotes', e); }
}

async function logCustNote(companyId) {
    const el = document.getElementById('custNewNote-' + companyId);
    if (!el || !el.value.trim()) return;
    try {
        await apiFetch('/api/companies/' + companyId + '/activities/note', {
            method: 'POST', body: { notes: el.value.trim() }
        });
        el.value = '';
        showToast('Note logged');
        _loadCustRecentNotes(companyId);
    } catch (e) { logCatchError('logCustNote', e); }
}

async function _loadCustRecentNotes(companyId) {
    const container = document.getElementById('custRecentNotes-' + companyId);
    if (!container) return;
    try {
        const activities = await apiFetch('/api/companies/' + companyId + '/activities');
        const notes = activities.filter(a => a.activity_type === 'note').slice(0, 10);
        if (!notes.length) { container.innerHTML = ''; return; }
        const relTime = window.getRelativeTime || ((d) => d ? new Date(d).toLocaleDateString() : '');
        container.innerHTML = notes.map(n => `<div class="note-entry">
            <div>${esc(n.notes || n.summary || '')}</div>
            <div class="note-entry-meta"><span>${esc(n.user_name || n.contact_name || '')}</span><span>${relTime(n.created_at)}</span></div>
        </div>`).join('');
    } catch (e) { /* silently fail */ }
}

async function saveContactNotes(siteId, contactId) {
    const el = document.getElementById('contactNotes-' + contactId);
    if (!el) return;
    const statusEl = document.getElementById('noteStatus-' + contactId);
    try {
        await apiFetch('/api/sites/' + siteId + '/contacts/' + contactId, {
            method: 'PUT', body: { notes: el.value }
        });
        if (statusEl) { statusEl.textContent = 'Saved'; statusEl.classList.add('visible'); setTimeout(() => statusEl.classList.remove('visible'), 2000); }
    } catch (e) {
        logCatchError('saveContactNotes', e);
        if (statusEl) { statusEl.textContent = 'Error'; statusEl.style.color = 'var(--red)'; statusEl.classList.add('visible'); setTimeout(() => { statusEl.classList.remove('visible'); statusEl.style.color = ''; }, 2000); }
    }
}

let _drawerContacts = [];
const _debouncedFilterDrawerContacts = debounce((q) => filterDrawerContacts(q), 150);

function _buildContactCardHtml(ct, companyId) {
    const initials = (ct.full_name || '?').split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2);
    const relTime = window.getRelativeTime || (() => '');
    const location = [ct.site_city, ct.site_state].filter(Boolean).join(', ');
    const isArchived = ct.is_active === false;
    const archiveBtn = isArchived
        ? `<button onclick="event.stopPropagation();toggleContactArchive(${ct.site_id},${ct.id},false)" title="Restore" style="font-size:10px;color:var(--green)">Restore</button>`
        : `<button onclick="event.stopPropagation();toggleContactArchive(${ct.site_id},${ct.id},true)" title="Archive" style="font-size:10px;color:var(--muted)">Archive</button>`;
    return `<div class="contact-card" ${isArchived ? 'style="opacity:0.5"' : ''}>
        <div class="contact-card-header">
            <div class="contact-card-avatar">${initials}</div>
            <div class="contact-card-info">
                <div class="contact-card-name">${esc(ct.full_name)}${ct.is_primary ? ' <span class="contact-badge-primary">Primary</span>' : ''}${isArchived ? ' <span style="font-size:9px;color:var(--muted)">(Archived)</span>' : ''}</div>
                ${ct.title ? '<div class="contact-card-title">' + esc(ct.title) + '</div>' : ''}
                <div class="contact-card-site">${esc(ct.site_name)}</div>
                ${location ? '<div class="contact-card-location">' + esc(location) + '</div>' : ''}
            </div>
            <div class="contact-card-actions">
                ${ct.email ? '<a href="mailto:'+escAttr(ct.email)+'" title="'+escAttr(ct.email)+'" onclick="event.stopPropagation();autoLogEmail(\''+escAttr(ct.email)+'\',\''+escAttr(ct.full_name || '')+'\')">✉</a>' : ''}
                ${ct.phone ? '<a href="tel:'+escAttr(toE164(ct.phone) || ct.phone)+'" class="phone-link" onclick="logCallInitiated(this)" data-phone="'+escAttr(ct.phone)+'" data-ctx="'+escAttr(JSON.stringify({company_id: companyId, customer_site_id: ct.site_id, origin: 'contact_card'}))+'" title="'+escAttr(ct.phone)+'">📞</a>' : ''}
                <button onclick="openEditSiteContact(${ct.site_id},${ct.id})" title="Edit">✎</button>
                ${archiveBtn}
            </div>
        </div>
        <div class="contact-card-meta">
            ${ct.email ? '<a href="mailto:'+escAttr(ct.email)+'" onclick="event.stopPropagation()">' + esc(ct.email) + '</a>' : ''}
            ${ct.phone ? phoneLink(ct.phone, {company_id: companyId, customer_site_id: ct.site_id, origin: 'contact_card_meta'}) : ''}
            ${ct.created_at ? '<span class="contact-card-added">' + relTime(ct.created_at) + '</span>' : ''}
        </div>
        <div class="contact-card-notes" style="display:flex;align-items:center;gap:0">
            <textarea id="contactNotes-${ct.id}" rows="1" placeholder="Contact notes..."
                onblur="saveContactNotes(${ct.site_id},${ct.id})"
                onkeydown="if(event.ctrlKey&&event.key==='Enter'){event.preventDefault();saveContactNotes(${ct.site_id},${ct.id})}">${esc(ct.notes || '')}</textarea>
            <span class="note-save-status" id="noteStatus-${ct.id}">Saved</span>
        </div>
        <div style="margin-top:4px;display:flex;align-items:start;gap:4px">
            <textarea id="contactNewNote-${ct.id}" rows="1" placeholder="Log a note..." style="flex:1;font-size:11px;padding:4px;border:1px solid var(--border);border-radius:4px;resize:none"
                onkeydown="if(event.ctrlKey&&event.key==='Enter'){event.preventDefault();logContactNote(${ct.site_id},${ct.id})}"></textarea>
            <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:4px 8px" onclick="logContactNote(${ct.site_id},${ct.id})">Log</button>
        </div>
        <div id="contactRecentNotes-${ct.id}" style="margin-top:4px"></div>
    </div>`;
}

async function logContactNote(siteId, contactId) {
    const el = document.getElementById('contactNewNote-' + contactId);
    if (!el || !el.value.trim()) return;
    try {
        await apiFetch('/api/sites/' + siteId + '/contacts/' + contactId + '/notes', {
            method: 'POST', body: { notes: el.value.trim() }
        });
        el.value = '';
        showToast('Note logged');
        _loadContactRecentNotes(siteId, contactId);
    } catch (e) { logCatchError('logContactNote', e); }
}

async function _loadContactRecentNotes(siteId, contactId) {
    const container = document.getElementById('contactRecentNotes-' + contactId);
    if (!container) return;
    try {
        const notes = await apiFetch('/api/sites/' + siteId + '/contacts/' + contactId + '/notes');
        if (!notes.length) { container.innerHTML = ''; return; }
        const relTime = window.getRelativeTime || ((d) => d ? new Date(d).toLocaleDateString() : '');
        container.innerHTML = notes.slice(0, 10).map(n => `<div class="note-entry">
            <div>${esc(n.notes || '')}</div>
            <div class="note-entry-meta"><span>${esc(n.user_name || '')}</span><span>${relTime(n.created_at)}</span></div>
        </div>`).join('');
    } catch (e) { /* silently fail */ }
}

async function toggleContactArchive(siteId, contactId, currentlyActive) {
    const action = currentlyActive ? 'archive' : 'restore';
    if (!confirm('Are you sure you want to ' + action + ' this contact?')) return;
    try {
        await apiFetch('/api/sites/' + siteId + '/contacts/' + contactId, {
            method: 'PUT', body: { is_active: !currentlyActive }
        });
        showToast('Contact ' + (currentlyActive ? 'archived' : 'restored'));
        // Reload the current view
        if (typeof renderCustomerDetail === 'function' && window._lastCustDetailId) {
            renderCustomerDetail(window._lastCustDetailId);
        }
    } catch (e) { logCatchError('toggleContactArchive', e); }
}

function filterDrawerContacts(query) {
    const grid = document.getElementById('drawerContactsGrid');
    if (!grid) return;
    const q = (query || '').toLowerCase().trim();
    const filtered = q ? _drawerContacts.filter(ct => {
        return (ct.full_name || '').toLowerCase().includes(q)
            || (ct.title || '').toLowerCase().includes(q)
            || (ct.site_name || '').toLowerCase().includes(q)
            || (ct.site_city || '').toLowerCase().includes(q)
            || (ct.site_state || '').toLowerCase().includes(q)
            || (ct.email || '').toLowerCase().includes(q);
    }) : _drawerContacts;
    const cid = _selectedCustId || 0;
    const cardBuilder = window.__isMobile ? _renderMobileContact : _buildContactCardHtml;
    // Safe: all user content escaped via esc() in card builders
    grid.innerHTML = filtered.length
        ? filtered.map(ct => cardBuilder(ct, cid)).join('')
        : '<p class="crm-empty" style="padding:20px;grid-column:1/-1">No contacts match your search</p>';
}

async function _renderCustDrawerContacts(companyId) {
    const body = document.getElementById('custDrawerBody');
    const title = document.getElementById('custDrawerTitle');
    if (!body) return;
    const c = crmCustomers.find(x => x.id === companyId);
    if (!c) { body.innerHTML = '<p class="crm-empty">Account not found</p>'; return; }

    const displayName = c.name.replace(/\s*(bucket|pass)\s*$/i, '').trim();
    if (title) title.innerHTML = esc(displayName) + (c.is_strategic ? ' <span style="color:var(--amber)">★</span>' : '');
    const mTitle2 = document.getElementById('custDrawerMobileTitle');
    if (mTitle2) mTitle2.textContent = displayName;

    const sites = c.sites || [];
    if (!sites.length) {
        body.innerHTML = `<div class="drawer-section"><p class="crm-empty">No sites yet — <a href="#" onclick="event.preventDefault();openAddSiteModal(${c.id},'${escAttr(c.name)}')">add a site</a> to start adding contacts</p></div>`;
        return;
    }

    body.innerHTML = '<div class="drawer-section"><p class="empty">Loading contacts...</p></div>';

    try {
        // Fetch contacts from all sites in parallel
        const sitePromises = sites.slice(0, 30).map(s => apiFetch('/api/sites/' + s.id).catch(() => null));
        const siteResults = await Promise.all(sitePromises);
        const allContacts = [];
        for (const s of siteResults) {
            if (s && s.contacts) {
                for (const ct of s.contacts) {
                    allContacts.push({ ...ct, site_id: s.id, site_name: s.site_name,
                        site_city: s.city, site_state: s.state });
                }
            }
        }

        // Sort: primary first, then alphabetical
        allContacts.sort((a, b) => {
            if (a.is_primary !== b.is_primary) return b.is_primary ? 1 : -1;
            return (a.full_name || '').localeCompare(b.full_name || '');
        });

        _drawerContacts = allContacts;

        let html = `<div class="drawer-section" style="padding-bottom:8px;border-bottom:1px solid var(--border)">
            <div style="display:flex;align-items:center;justify-content:space-between">
                <div class="drawer-section-title" style="margin:0">${allContacts.length} Contact${allContacts.length !== 1 ? 's' : ''} across ${sites.length} site${sites.length !== 1 ? 's' : ''}</div>
                <button class="btn btn-ghost btn-sm" onclick="openAddSiteContact(${sites[0].id})">+ Add Contact</button>
            </div>
            ${allContacts.length > 3 ? '<input class="drawer-contact-search" placeholder="Search contacts..." oninput="_debouncedFilterDrawerContacts(this.value)">' : ''}
        </div>`;

        if (!allContacts.length) {
            html += `<div class="drawer-section"><p class="crm-empty">No contacts yet — add contacts to your sites to build your stakeholder map</p></div>`;
        } else if (window.__isMobile) {
            // Mobile: render tap-friendly contact cards with call/email links
            html += '<div id="drawerContactsGrid" style="padding:8px 0">';
            for (const ct of allContacts) {
                html += _renderMobileContact(ct, companyId);
            }
            html += '</div>';
        } else {
            html += '<div class="contacts-grid" id="drawerContactsGrid">';
            for (const ct of allContacts) {
                html += _buildContactCardHtml(ct, companyId);
            }
            html += '</div>';
        }

        // Safe: all user content escaped via esc() in card builders
        body.innerHTML = html;
    } catch (e) {
        body.innerHTML = '<div class="drawer-section"><p class="crm-empty" style="color:var(--red)">Failed to load contacts</p></div>';
    }
}

// Escape key to close drawer
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        if (document.getElementById('custDrawer')?.classList.contains('open')) { closeCustDrawer(); e.preventDefault(); return; }
        if (document.getElementById('vendorDrawer')?.classList.contains('open') && window.closeVendorDrawer) { window.closeVendorDrawer(); e.preventDefault(); return; }
        if (document.getElementById('contactDrawer')?.classList.contains('open') && window.closeContactDrawer) { window.closeContactDrawer(); e.preventDefault(); return; }
    }
});

async function toggleSiteDetail(siteId) {
    const panel = document.getElementById('siteDetail-' + siteId);
    if (!panel) return;
    if (panel.style.display === 'none') {
        panel.style.display = 'block';
        panel.innerHTML = '<p class="empty" style="padding:8px">Loading...</p>';
        try {
            const s = await apiFetch('/api/sites/' + siteId);
            const siteDomain = s.company_domain || (s.company_website ? s.company_website.replace(/https?:\/\/(www\.)?/, '').split('/')[0] : '');
            const contacts = s.contacts || [];
            // Sort: primary first, then alphabetical
            const sorted = [...contacts].sort((a, b) => {
                if (a.is_primary !== b.is_primary) return b.is_primary ? 1 : -1;
                return (a.full_name || '').localeCompare(b.full_name || '');
            });
            const renderContact = c => {
                const isArchived = c.is_active === false;
                const archiveBtn = isArchived
                    ? `<button class="btn btn-ghost btn-sm" style="color:var(--green)" onclick="event.stopPropagation();toggleContactArchive(${s.id},${c.id},false)">Restore</button>`
                    : `<button class="btn btn-ghost btn-sm" style="color:var(--muted)" onclick="event.stopPropagation();toggleContactArchive(${s.id},${c.id},true)">Archive</button>`;
                return `
                <div class="si-contact-card" ${isArchived ? 'style="opacity:0.5"' : ''} data-contact-search="${escAttr((c.full_name + ' ' + (c.title || '') + ' ' + (c.email || '')).toLowerCase())}">
                    <div class="si-contact-left">
                        <div class="si-contact-avatar">${esc((c.full_name || '?')[0].toUpperCase())}</div>
                    </div>
                    <div class="si-contact-info">
                        <div class="si-contact-row1">
                            <span class="si-contact-name">${esc(c.full_name)}</span>
                            ${c.is_primary ? '<span class="si-contact-badge">Primary</span>' : ''}
                            ${isArchived ? '<span style="font-size:9px;color:var(--muted)">(Archived)</span>' : ''}
                        </div>
                        ${c.title ? '<div class="si-contact-title">' + esc(c.title) + '</div>' : ''}
                        <div class="si-contact-meta">
                            ${c.email ? '<a href="mailto:'+esc(c.email)+'" title="'+escAttr(c.email)+'" onclick="autoLogEmail(\''+escAttr(c.email)+'\',\''+escAttr(c.full_name || '')+'\')">'+esc(c.email)+'</a>' : ''}
                            ${c.phone ? phoneLink(c.phone, {company_id: _selectedCustId, customer_site_id: s.id, origin: 'site_contact_detail'}) : ''}
                        </div>
                        ${c.notes ? '<div class="si-contact-notes">'+esc(c.notes)+'</div>' : ''}
                        <div style="margin-top:4px;display:flex;align-items:start;gap:4px">
                            <textarea id="siContactNewNote-${c.id}" rows="1" placeholder="Log a note..." style="flex:1;font-size:11px;padding:4px;border:1px solid var(--border);border-radius:4px;resize:none"
                                onkeydown="if(event.ctrlKey&&event.key==='Enter'){event.preventDefault();logContactNote(${s.id},${c.id})}"></textarea>
                            <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:4px 8px" onclick="logContactNote(${s.id},${c.id})">Log</button>
                        </div>
                        <div id="contactRecentNotes-${c.id}" style="margin-top:4px"></div>
                    </div>
                    <div class="si-contact-actions">
                        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openEditSiteContact(${s.id},${c.id})">Edit</button>
                        ${archiveBtn}
                        <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteSiteContact(${s.id},${c.id},'${escAttr(c.full_name)}')">✕</button>
                    </div>
                </div>`;
            };
            const activeContacts = contacts.filter(c => c.is_active !== false);
            const archivedContacts = contacts.filter(c => c.is_active === false);
            const searchBar = contacts.length > 5
                ? `<input class="si-contact-search" placeholder="Filter contacts…" oninput="_debouncedFilterSiteContacts(this,${s.id})">`
                : '';
            const archiveToggle = archivedContacts.length
                ? `<label style="font-size:10px;color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer"><input type="checkbox" onchange="document.getElementById('archivedContacts-${s.id}').style.display=this.checked?'block':'none'"> Show archived (${archivedContacts.length})</label>`
                : '';
            const activeHtml = activeContacts.length
                ? activeContacts.map(renderContact).join('')
                : '<p class="empty" style="padding:4px;font-size:11px">No active contacts — add one below</p>';
            const archivedHtml = archivedContacts.length
                ? `<div id="archivedContacts-${s.id}" style="display:none">${archivedContacts.map(renderContact).join('')}</div>`
                : '';
            const contactsHtml = `${searchBar}<div class="si-contact-grid" id="contactGrid-${s.id}">${activeHtml}${archivedHtml}</div>`;
            panel.innerHTML = `
            <div class="site-info">
                <div class="si-row"><span class="si-label">Owner</span><span>${esc(s.owner_name || '—')}</span></div>
                <div class="si-contacts">
                    <div style="display:flex;align-items:center;justify-content:space-between">
                        <div class="si-contacts-title">Contacts <span style="font-weight:400;color:var(--muted)">(${activeContacts.length})</span></div>
                        <div style="display:flex;align-items:center;gap:8px">
                            ${archiveToggle}
                            <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openAddSiteContact(${s.id})">+ Add</button>
                        </div>
                    </div>
                    ${contactsHtml}
                </div>
                <div class="si-row"><span class="si-label">Terms</span><span>${esc(s.payment_terms || '—')} · ${esc(s.shipping_terms || '—')}</span></div>
                <div class="si-row"><span class="si-label">Address</span><span>${esc(s.address_line1 || '')} ${s.city ? esc(s.city)+', ' : ''}${esc(s.state || '')} ${esc(s.zip || '')}</span></div>
                ${s.notes ? '<div class="si-row"><span class="si-label">Notes</span><span>'+esc(s.notes)+'</span></div>' : ''}
                <div class="si-reqs">
                    <strong style="font-size:11px;color:var(--muted)">Recent Requisitions</strong>
                    ${(s.recent_reqs || []).length ? s.recent_reqs.map(r => `
                        <div class="si-req" onclick="sidebarNav('reqs');setTimeout(()=>toggleDrillDown(${r.id}),300)">
                            <span>REQ-${String(r.id).padStart(3,'0')}</span>
                            <span>${r.requirement_count} MPNs</span>
                            <span class="status-badge status-${r.status}">${r.status}</span>
                            <span>${fmtDate(r.created_at)}</span>
                        </div>
                    `).join('') : '<p class="empty" style="padding:4px;font-size:11px">No requisitions</p>'}
                </div>
                <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
                    <button class="btn btn-ghost btn-sm" onclick="openEditSiteModal(${s.id})">Edit Site</button>
                    <button class="btn-enrich" onclick="unifiedEnrichCompany(${s.company_id})">Enrich</button>
                </div>
                <div id="siteIntel-${s.id}"></div>
            </div>`;
            // Load company intel asynchronously
            const intelEl = document.getElementById('siteIntel-' + s.id);
            if (intelEl && s.company_name) {
                loadCompanyIntel(s.company_name, siteDomain, intelEl);
            }
        } catch (e) { logCatchError('loadSiteDetail', e); panel.innerHTML = '<p class="empty" style="padding:8px">Error loading site</p>'; }
    } else {
        panel.style.display = 'none';
    }
}

function openNewCompanyModal() {
    ['ncName','ncWebsite','ncLinkedin','ncIndustry'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    const warn = document.getElementById('ncDupWarning');
    if (warn) warn.classList.add('u-hidden');
    openModal('newCompanyModal', 'ncName');
}

function openNewVendorModal() {
    openModal('vendorContactModal');
    const titleEl = document.getElementById('vendorContactModalTitle');
    if (titleEl) titleEl.textContent = 'Add New Vendor';
}

const debouncedCheckDupCompany = debounce(async (val) => {
    const warn = document.getElementById('ncDupWarning');
    if (!warn) return;
    const q = (val || '').trim();
    if (q.length < 3) { warn.classList.add('u-hidden'); return; }
    try {
        const resp = await apiFetch('/api/companies/check-duplicate?name=' + encodeURIComponent(q));
        if (resp.matches && resp.matches.length > 0) {
            const names = resp.matches.map(m =>
                '<b>' + esc(m.name) + '</b>' + (m.match === 'exact' ? ' (exact match)' : '')
            ).join(', ');
            warn.innerHTML = 'Possible duplicate: ' + names;
            warn.classList.remove('u-hidden');
        } else {
            warn.classList.add('u-hidden');
        }
    } catch (e) { warn.classList.add('u-hidden'); }
}, 400);

async function createCompany(forceCreate) {
    const _v = id => document.getElementById(id)?.value || '';
    const name = _v('ncName').trim();
    if (!name) return;
    const btn = document.querySelector('#newCompanyModal .btn-primary');
    const body = {
        name, website: _v('ncWebsite').trim(),
        linkedin_url: _v('ncLinkedin').trim() || null,
        industry: _v('ncIndustry').trim(),
    };
    const qs = forceCreate ? '?force=true' : '';
    await guardBtn(btn, 'Creating…', async () => {
        try {
            const data = await apiFetch('/api/companies' + qs, {
                method: 'POST', body
            });
            closeModal('newCompanyModal');
            ['ncName','ncWebsite','ncLinkedin','ncIndustry'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
            showToast('Company "' + data.name + '" created', 'success');
            await loadSiteOptions();
            loadCustomers();
            if (window._quickCreateFromReq && data.default_site_id) {
                window._quickCreateFromReq = false;
                selectSite(data.default_site_id, data.name);
                return;
            }
            openAddSiteModal(data.id, data.name);
        } catch (e) {
            // 409 = duplicate found — offer to use existing company
            if (e.status === 409) {
                try {
                    const errData = JSON.parse(e.message);
                    if (errData.duplicates && errData.duplicates.length) {
                        const dup = errData.duplicates[0];
                        const msg = `Similar company found: "${dup.name}" (${dup.match} match).\n\nAdd a site to the existing company instead?\n\n(Click Cancel to create a separate account — this is fine if different salespeople own different sites.)`;
                        if (confirm(msg)) {
                            closeModal('newCompanyModal');
                            openAddSiteModal(dup.id, dup.name);
                        } else {
                            await createCompany(true);
                        }
                        return;
                    }
                } catch (_) { /* parse failed, fall through */ }
            }
            showToast('Failed to create company', 'error');
        }
    });
}

async function openEditCompany(companyId) {
    var c = crmCustomers.find(x => x.id === companyId);
    if (!c) return;
    const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    _s('ecId', companyId);
    _s('ecName', c.name || '');
    _s('ecAccountType', c.account_type || '');
    _s('ecPhone', c.phone || '');
    _s('ecWebsite', c.website || '');
    _s('ecDomain', c.domain || '');
    _s('ecLinkedin', c.linkedin_url || '');
    _s('ecIndustry', c.industry || '');
    _s('ecLegalName', c.legal_name || '');
    _s('ecEmployeeSize', c.employee_size || '');
    _s('ecHqCity', c.hq_city || '');
    _s('ecHqState', c.hq_state || '');
    _s('ecHqCountry', c.hq_country || '');
    _s('ecCreditTerms', c.credit_terms || '');
    _s('ecTaxId', c.tax_id || '');
    _s('ecCurrency', c.currency || 'USD');
    _s('ecCarrier', c.preferred_carrier || '');
    _s('ecNotes', c.notes || '');
    const ecStrategic = document.getElementById('ecStrategic'); if (ecStrategic) ecStrategic.checked = !!c.is_strategic;
    await loadUserOptions('ecOwner');
    if (c.account_owner_id) _s('ecOwner', c.account_owner_id);
    openModal('editCompanyModal', 'ecName');
}

async function saveEditCompany() {
    const _v = id => document.getElementById(id)?.value ?? '';
    var id = _v('ecId');
    var name = _v('ecName').trim();
    if (!name) { showToast('Company name is required', 'error'); return; }
    var ownerVal = _v('ecOwner');
    try {
        await apiFetch('/api/companies/' + id, {
            method: 'PUT',
            body: {
                name: name,
                account_type: _v('ecAccountType') || null,
                phone: _v('ecPhone').trim() || null,
                website: _v('ecWebsite').trim() || null,
                domain: _v('ecDomain').trim() || null,
                linkedin_url: _v('ecLinkedin').trim() || null,
                industry: _v('ecIndustry').trim() || null,
                legal_name: _v('ecLegalName').trim() || null,
                employee_size: _v('ecEmployeeSize').trim() || null,
                hq_city: _v('ecHqCity').trim() || null,
                hq_state: _v('ecHqState').trim() || null,
                hq_country: _v('ecHqCountry').trim() || null,
                credit_terms: _v('ecCreditTerms').trim() || null,
                tax_id: _v('ecTaxId').trim() || null,
                currency: _v('ecCurrency').trim() || null,
                preferred_carrier: _v('ecCarrier').trim() || null,
                notes: _v('ecNotes'),
                is_strategic: document.getElementById('ecStrategic')?.checked ?? false,
                account_owner_id: ownerVal ? parseInt(ownerVal) : null,
            }
        });
        closeModal('editCompanyModal');
        showToast('Company updated', 'success');
        invalidateCompanyCache();
        loadCustomers();
    } catch (e) { showToast('Failed to update company: ' + (e.message || ''), 'error'); }
}

function openAddSiteModal(companyId, companyName) {
    const cidEl = document.getElementById('asSiteCompanyId');
    if (cidEl) { cidEl.value = companyId; delete cidEl.dataset.editSiteId; }
    const cnEl = document.getElementById('asSiteCompanyName'); if (cnEl) cnEl.textContent = companyName;
    document.querySelector('#addSiteModal h2').innerHTML = 'Add Site to <span id="asSiteCompanyName">' + esc(companyName) + '</span>';
    ['asSiteName','asSiteAddr1','asSiteAddr2','asSiteCity','asSiteState','asSiteZip','asSitePayTerms','asSiteShipTerms','asSiteTimezone','asSiteRecvHours','asSiteCarrierAcct'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    const asSiteCountry = document.getElementById('asSiteCountry'); if (asSiteCountry) asSiteCountry.value = 'US';
    const asSiteType = document.getElementById('asSiteType'); if (asSiteType) asSiteType.value = '';
    const asSiteNotes = document.getElementById('asSiteNotes'); if (asSiteNotes) asSiteNotes.value = '';
    openModal('addSiteModal', 'asSiteName');
}

async function addSite() {
    const _v = id => document.getElementById(id)?.value ?? '';
    const companyId = _v('asSiteCompanyId');
    const data = {
        site_name: _v('asSiteName').trim(),
        owner_id: _v('asSiteOwner') || null,
        address_line1: _v('asSiteAddr1').trim() || null,
        address_line2: _v('asSiteAddr2').trim() || null,
        city: _v('asSiteCity').trim() || null,
        state: _v('asSiteState').trim() || null,
        zip: _v('asSiteZip').trim() || null,
        country: _v('asSiteCountry').trim() || 'US',
        payment_terms: _v('asSitePayTerms').trim(),
        shipping_terms: _v('asSiteShipTerms').trim(),
        site_type: _v('asSiteType') || null,
        timezone: _v('asSiteTimezone').trim() || null,
        receiving_hours: _v('asSiteRecvHours').trim() || null,
        carrier_account: _v('asSiteCarrierAcct').trim() || null,
        notes: _v('asSiteNotes').trim() || null,
    };
    if (!data.site_name) return;
    try {
        const editId = document.getElementById('asSiteCompanyId')?.dataset?.editSiteId;
        if (editId) {
            await apiFetch('/api/sites/' + editId, { method: 'PUT', body: data });
            const cidEl = document.getElementById('asSiteCompanyId'); if (cidEl) delete cidEl.dataset.editSiteId;
            showToast('Site updated', 'success');
        } else {
            await apiFetch('/api/companies/' + companyId + '/sites', { method: 'POST', body: data });
            showToast('Site created', 'success');
        }
        closeModal('addSiteModal');
        ['asSiteName','asSiteAddr1','asSiteAddr2','asSiteCity','asSiteState','asSiteZip','asSitePayTerms','asSiteShipTerms'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
        const asSC = document.getElementById('asSiteCountry'); if (asSC) asSC.value = 'US';
        const asSN = document.getElementById('asSiteNotes'); if (asSN) asSN.value = '';
        loadCustomers();
        loadSiteOptions();
    } catch (e) { showToast('Failed to save site', 'error'); }
}

async function openEditSiteModal(siteId) {
    try {
        const s = await apiFetch('/api/sites/' + siteId);
        const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
        const cidEl = document.getElementById('asSiteCompanyId');
        if (cidEl) { cidEl.value = s.company_id; cidEl.dataset.editSiteId = siteId; }
        _s('asSiteCompanyName', 'textContent', s.company_name || 'Unknown');
        _s('asSiteName', 'value', s.site_name || ''); _s('asSiteOwner', 'value', s.owner_id || '');
        _s('asSiteAddr1', 'value', s.address_line1 || ''); _s('asSiteAddr2', 'value', s.address_line2 || '');
        _s('asSiteCity', 'value', s.city || ''); _s('asSiteState', 'value', s.state || '');
        _s('asSiteZip', 'value', s.zip || ''); _s('asSiteCountry', 'value', s.country || 'US');
        _s('asSitePayTerms', 'value', s.payment_terms || ''); _s('asSiteShipTerms', 'value', s.shipping_terms || '');
        _s('asSiteType', 'value', s.site_type || ''); _s('asSiteTimezone', 'value', s.timezone || '');
        _s('asSiteRecvHours', 'value', s.receiving_hours || ''); _s('asSiteCarrierAcct', 'value', s.carrier_account || '');
        _s('asSiteNotes', 'value', s.notes || '');
        openModal('addSiteModal');
        document.querySelector('#addSiteModal h2').innerHTML = 'Edit Site — <span>' + esc(s.site_name || '') + '</span>';
    } catch (e) { console.error('openEditSiteModal:', e); showToast('Error loading site', 'error'); }
}

// ── Offers Tab ─────────────────────────────────────────────────────────

let _hasNewOffers = false;
let _latestOfferAt = null;
let _pendingOfferFiles = [];  // Files queued for upload after offer save
let _offerStatusFilter = 'all';
let _offerSort = 'newest';

async function loadOffers() {
    if (!currentReqId) return;
    const reqId = currentReqId;
    try {
        const data = await apiFetch('/api/requisitions/' + reqId + '/offers');
        if (currentReqId !== reqId) return;
        _hasNewOffers = data.has_new_offers || false;
        _latestOfferAt = data.latest_offer_at || null;
        crmOffers = data.groups || [];
        selectedOffers.clear();
        renderOffers();
        updateOfferTabBadge();
    } catch (e) { logCatchError('loadOffers', e); showToast('Failed to load offers', 'error'); }
}

function updateOfferTabBadge() {
    const totalOffers = crmOffers.reduce((sum, g) => sum + (g.offers?.length || 0), 0);
    document.querySelectorAll('#reqTabs .tab').forEach(t => {
        if (t.textContent.match(/^Offers/)) {
            t.textContent = totalOffers ? 'Offers (' + totalOffers + ')' : 'Offers';
            t.classList.remove('tab-new', 'tab-urgent');
            if (_hasNewOffers && totalOffers && _latestOfferAt) {
                const hoursAgo = (Date.now() - new Date(_latestOfferAt).getTime()) / 3600000;
                if (hoursAgo < 12) {
                    t.classList.add('tab-new');
                } else if (hoursAgo < 96) {
                    t.classList.add('tab-urgent');
                }
                // > 96h: no highlight (auto-clear)
            }
        }
    });
}

function setOfferFilter(status, btn) {
    _offerStatusFilter = status;
    document.querySelectorAll('#offerFilterBar .filter-pill').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    renderOffers();
}

function setOfferSort(val) {
    _offerSort = val;
    renderOffers();
}

function _sortOffers(offers) {
    const sorted = [...offers];
    switch (_offerSort) {
        case 'price_asc':  return sorted.sort((a, b) => (a.unit_price ?? Infinity) - (b.unit_price ?? Infinity));
        case 'price_desc': return sorted.sort((a, b) => (b.unit_price ?? -1) - (a.unit_price ?? -1));
        case 'vendor':     return sorted.sort((a, b) => (a.vendor_name || '').localeCompare(b.vendor_name || ''));
        default:           return sorted;  // newest = server order
    }
}

function renderOffers() {
    const el = document.getElementById('offersContent');
    if (!crmOffers.length) {
        el.innerHTML = stateEmpty('No offers yet', 'Log vendor offers as they come in');
        return;
    }
    const filterBar = `<div id="offerFilterBar" class="offer-filter-bar">
        <div class="filter-pills">
            <button class="filter-pill ${_offerStatusFilter==='all'?'on':''}" onclick="setOfferFilter('all',this)">All</button>
            <button class="filter-pill ${_offerStatusFilter==='active'?'on':''}" onclick="setOfferFilter('active',this)">Active</button>
            <button class="filter-pill ${_offerStatusFilter==='expired'?'on':''}" onclick="setOfferFilter('expired',this)">Expired</button>
        </div>
        <select class="offer-sort" onchange="setOfferSort(this.value)">
            <option value="newest" ${_offerSort==='newest'?'selected':''}>Newest</option>
            <option value="price_asc" ${_offerSort==='price_asc'?'selected':''}>Price ↑</option>
            <option value="price_desc" ${_offerSort==='price_desc'?'selected':''}>Price ↓</option>
            <option value="vendor" ${_offerSort==='vendor'?'selected':''}>Vendor A→Z</option>
        </select>
    </div>`;
    const groupsHtml = crmOffers.map(group => {
        const targetStr = group.target_price ? '$' + Number(group.target_price).toFixed(4) : 'no target';
        const lastQ = group.last_quoted?.sell_price != null ? 'last: $' + Number(group.last_quoted.sell_price).toFixed(4) : '';
        let visibleOffers = group.offers;
        if (_offerStatusFilter !== 'all') {
            visibleOffers = visibleOffers.filter(o => (o.status || 'active') === _offerStatusFilter);
        }
        visibleOffers = _sortOffers(visibleOffers);
        const offersHtml = visibleOffers.length ? visibleOffers.map(o => {
            const checked = selectedOffers.has(o.id) ? 'checked' : '';
            const isRef = o.status === 'reference';
            const isExpired = o.status === 'expired';
            const isSub = o.mpn && group.mpn && o.mpn.trim().toUpperCase() !== group.mpn.trim().toUpperCase();
            const rowCls = isRef ? 'offer-ref' : (isExpired ? 'offer-expired' : (isSub ? 'offer-sub' : ''));
            const subDetails = [o.firmware && 'FW: '+esc(o.firmware), o.hardware_code && 'HW: '+esc(o.hardware_code), o.packaging && 'Pkg: '+esc(o.packaging)].filter(Boolean).join(' · ');

            // Notes pill — shows date/time, click to expand
            let noteStr = '';
            if (o.notes) {
                const noteDate = o.created_at ? new Date(o.created_at).toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
                noteStr = `<span class="offer-note-pill" onclick="this.nextElementSibling.classList.toggle('hidden');event.stopPropagation()" style="display:inline-flex;align-items:center;gap:3px;margin-top:3px;padding:1px 8px;border-radius:10px;background:var(--amber-light,#fff3cd);color:var(--amber,#856404);font-size:10px;font-weight:600;cursor:pointer;border:1px solid var(--amber,#856404)">📝 Notes${noteDate ? ' · '+noteDate : ''}</span><div class="hidden" style="margin-top:4px;padding:6px 8px;border-radius:6px;background:var(--bg2,#f8f9fa);border:1px solid var(--border);font-size:11px;color:var(--text1);white-space:pre-wrap;max-width:350px">${esc(o.notes)}</div>`;
            }

            // Photo indicator — prominent badge with count and click to open gallery
            const images = (o.attachments||[]).filter(a => (a.content_type||'').startsWith('image/'));
            const nonImages = (o.attachments||[]).filter(a => !(a.content_type||'').startsWith('image/'));
            let photoHtml = '';
            if (images.length) {
                photoHtml = `<span onclick="openOfferGallery(${o.id});event.stopPropagation()" style="display:inline-flex;align-items:center;gap:3px;margin-top:3px;padding:2px 8px;border-radius:10px;background:var(--teal-light,#d1ecf1);color:var(--teal,#0c7c84);font-size:10px;font-weight:600;cursor:pointer;border:1px solid var(--teal,#0c7c84)">📷 ${images.length} Photo${images.length>1?'s':''} — View</span>`;
            }
            const fileHtml = nonImages.map(a => `<a href="${esc(a.onedrive_url||'#')}" target="_blank" style="font-size:10px;color:var(--teal);text-decoration:underline">${esc(a.file_name)}</a><button onclick="event.stopPropagation();deleteOfferAttachment(${a.id})" style="border:none;background:none;color:var(--red);cursor:pointer;font-size:10px;padding:0 2px" title="Remove attachment">&times;</button>`).join(' ');

            const enteredStr = o.entered_by && o.entered_by !== '?' ? '<span style="font-size:10px;color:var(--muted)">by '+esc(o.entered_by)+'</span>' : '';
            return `
            <tr class="${rowCls}">
                <td><input type="checkbox" ${checked} ${isRef ? 'disabled' : ''} onchange="toggleOfferSelect(${o.id},this.checked)"></td>
                <td>${esc(o.vendor_name)}${isSub ? ' <span class="badge b-sub">SUB</span>' : ''}${o.mpn && isSub ? '<div style="font-size:10px;color:#0e7490;font-weight:600">'+esc(o.mpn)+'</div>' : ''}${subDetails ? '<div class="sc-detail" style="font-size:10px;color:var(--muted)">'+subDetails+'</div>' : ''}${noteStr ? '<div>'+noteStr+'</div>' : ''}${photoHtml || fileHtml ? '<div style="margin-top:2px">'+photoHtml+(fileHtml?' '+fileHtml:'')+'</div>' : ''}</td>
                <td>${o.unit_price != null ? '$'+Number(o.unit_price).toFixed(4) : '—'}</td>
                <td>${o.qty_available != null ? o.qty_available.toLocaleString() : '—'}</td>
                <td>${esc(o.lead_time || '—')}</td>
                <td>${esc(o.condition || '—')}</td>
                <td>${esc(o.date_code || '—')}</td>
                <td>${o.moq ? o.moq.toLocaleString() : '—'}</td>
                <td style="font-size:10px">${esc(o.warranty || '—')}</td>
                <td style="font-size:10px">${esc(o.country_of_origin || '—')}</td>
                <td style="font-size:10px;white-space:nowrap">${o.avg_rating != null ? '<span style="color:var(--amber)">★</span> ' + o.avg_rating + ' <span style="color:var(--muted)">(' + o.review_count + ')</span>' : '—'}</td>
                <td>${enteredStr}</td>
                <td>${isRef ? '<span class="offer-ref-badge">ref</span>' : '<button class="btn btn-ghost btn-sm" onclick="openEditOffer('+o.id+')" title="Edit offer" style="padding:2px 6px;font-size:10px">✎</button><button class="btn btn-danger btn-sm" onclick="deleteOffer('+o.id+')" title="Remove offer" style="padding:2px 6px;font-size:10px">✕</button>'}</td>
            </tr>`;
        }).join('') : '<tr><td colspan="13" class="empty" style="padding:8px">No offers for this part</td></tr>';
        return `
        <div class="offer-group">
            <div class="offer-group-header">
                <strong>${esc(group.mpn)}</strong>
                <span>need ${(group.target_qty||0).toLocaleString()}</span>
                <span>${targetStr}</span>
                <span>${lastQ}</span>
                <button class="btn btn-ghost btn-sm" onclick="openPricingHistory('${escAttr(group.mpn)}')">📊</button>
            </div>
            <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
            <table class="tbl offer-table">
                <thead><tr><th style="width:30px"></th><th>Vendor</th><th>Price</th><th>Avail</th><th>Lead</th><th>Cond</th><th>DC</th><th>MOQ</th><th>Warranty</th><th>COO</th><th>Rating</th><th>By</th><th style="width:40px"></th></tr></thead>
                <tbody>${offersHtml}</tbody>
            </table>
            </div>
        </div>`;
    }).join('');
    el.innerHTML = filterBar + groupsHtml;
    updateBuildQuoteBtn();
}

function toggleOfferSelect(offerId, checked) {
    if (checked) selectedOffers.add(offerId);
    else selectedOffers.delete(offerId);
    updateBuildQuoteBtn();
}

// ── Offer Photo Gallery / Lightbox ─────────────────────────────────────
function openOfferGallery(offerId) {
    // Find offer across all groups
    let images = [];
    for (const g of crmOffers) {
        const o = g.offers.find(x => x.id === offerId);
        if (o) {
            images = (o.attachments||[]).filter(a => (a.content_type||'').startsWith('image/'));
            break;
        }
    }
    if (!images.length) return;

    let idx = 0;
    // Remove existing gallery if any
    let gal = document.getElementById('offerGalleryOverlay');
    if (gal) gal.remove();

    gal = document.createElement('div');
    gal.id = 'offerGalleryOverlay';
    gal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:center;';
    gal.innerHTML = `
        <button id="galClose" style="position:absolute;top:16px;right:20px;background:none;border:none;color:#fff;font-size:28px;cursor:pointer;z-index:10001">&times;</button>
        <button id="galPrev" style="position:absolute;left:16px;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);border:none;color:#fff;font-size:32px;cursor:pointer;padding:8px 14px;border-radius:8px;z-index:10001">&#8249;</button>
        <div style="display:flex;flex-direction:column;align-items:center;max-width:90vw;max-height:90vh">
            <img id="galImg" style="max-width:85vw;max-height:80vh;object-fit:contain;border-radius:8px;box-shadow:0 4px 24px rgba(0,0,0,.5)">
            <div id="galCaption" style="color:#fff;font-size:13px;margin-top:8px;text-align:center"></div>
        </div>
        <button id="galNext" style="position:absolute;right:16px;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);border:none;color:#fff;font-size:32px;cursor:pointer;padding:8px 14px;border-radius:8px;z-index:10001">&#8250;</button>
    `;
    document.body.appendChild(gal);

    function show(i) {
        idx = i;
        const img = images[idx];
        const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
        _s('galImg', 'src', img.onedrive_url || '');
        _s('galCaption', 'textContent', img.file_name + ' (' + (idx+1) + '/' + images.length + ')');
        const vis = images.length > 1 ? 'visible' : 'hidden';
        const gp = document.getElementById('galPrev'); if (gp) gp.style.visibility = vis;
        const gn = document.getElementById('galNext'); if (gn) gn.style.visibility = vis;
    }
    show(0);

    function closeGallery() {
        document.removeEventListener('keydown', galKey);
        gal.remove();
    }
    const gc = document.getElementById('galClose'); if (gc) gc.onclick = closeGallery;
    const gp = document.getElementById('galPrev'); if (gp) gp.onclick = (e) => { e.stopPropagation(); show((idx - 1 + images.length) % images.length); };
    const gn = document.getElementById('galNext'); if (gn) gn.onclick = (e) => { e.stopPropagation(); show((idx + 1) % images.length); };
    gal.onclick = (e) => { if (e.target === gal) closeGallery(); };
    // Keyboard nav
    function galKey(e) {
        if (e.key === 'Escape') closeGallery();
        if (e.key === 'ArrowLeft') show((idx - 1 + images.length) % images.length);
        if (e.key === 'ArrowRight') show((idx + 1) % images.length);
    }
    document.addEventListener('keydown', galKey);
}

function updateBuildQuoteBtn() {
    const btn = document.getElementById('buildQuoteBtn');
    if (btn) {
        btn.disabled = selectedOffers.size === 0;
        btn.textContent = 'Build Quote from Selected (' + selectedOffers.size + ')';
    }
}

// ── OneDrive Browser ────────────────────────────────────────────────────

let _odCurrentPath = '';
let _odTargetOfferId = null;

async function browseOneDrive(path) {
    _odCurrentPath = path;
    const el = document.getElementById('odFileList');
    el.innerHTML = '<p class="empty">Loading…</p>';
    // Update breadcrumb
    const bc = document.getElementById('odBreadcrumb');
    const parts = path ? path.split('/').filter(Boolean) : [];
    let bcHtml = '<a onclick="browseOneDrive(\'\')" style="cursor:pointer;color:var(--teal)">Root</a>';
    let cumPath = '';
    for (const p of parts) {
        cumPath += (cumPath ? '/' : '') + p;
        const cp = cumPath;
        bcHtml += ' / <a onclick="browseOneDrive(\'' + escAttr(cp) + '\')" style="cursor:pointer;color:var(--teal)">' + esc(p) + '</a>';
    }
    bc.innerHTML = bcHtml;
    try {
        const url = '/api/onedrive/browse' + (path ? '?path=' + encodeURIComponent(path) : '');
        const items = await apiFetch(url);
        if (!items.length) {
            el.innerHTML = '<p class="empty">Empty folder</p>';
            return;
        }
        el.innerHTML = items.map(i => {
            if (i.is_folder) {
                const folderPath = path ? path + '/' + i.name : i.name;
                return `<div class="card card-clickable" style="padding:8px 12px;margin-bottom:4px" onclick="browseOneDrive('${escAttr(folderPath)}')">
                    <span style="font-size:13px">📁 ${esc(i.name)}</span>
                </div>`;
            }
            return `<div class="card" style="padding:8px 12px;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center">
                <span style="font-size:13px">📄 ${esc(i.name)} <span style="color:var(--muted);font-size:10px">${i.size ? (i.size/1024).toFixed(0)+'KB' : ''}</span></span>
                <button class="btn btn-primary btn-sm" onclick="selectOneDriveFile('${escAttr(i.id)}')">Attach</button>
            </div>`;
        }).join('');
    } catch (e) {
        el.innerHTML = '<p class="empty">Failed to load — check Microsoft connection</p>';
        console.error('browseOneDrive:', e);
    }
}

async function selectOneDriveFile(itemId) {
    if (_odTargetOfferId) {
        // Attach directly to an existing offer
        try {
            await apiFetch('/api/offers/' + _odTargetOfferId + '/attachments/onedrive', {
                method: 'POST', body: { item_id: itemId }
            });
            showToast('File attached', 'success');
            closeModal('oneDriveModal');
            loadOffers();
        } catch (e) { showToast('Failed to attach', 'error'); }
    } else {
        // Fetch file info and add to pending list (pre-save flow)
        try {
            const items = await apiFetch('/api/onedrive/browse' + (_odCurrentPath ? '?path=' + encodeURIComponent(_odCurrentPath) : ''));
            const item = items.find(i => i.id === itemId);
            if (item) {
                // OneDrive attachment selection (kept for edit-offer flow)
                showToast('File selected: ' + item.name, 'info');
            }
            closeModal('oneDriveModal');
        } catch (e) { showToast('Failed to select file', 'error'); }
    }
}

async function deleteOfferAttachment(attId) {
    if (!confirm('Remove this attachment?')) return;
    try {
        await apiFetch('/api/offer-attachments/' + attId, { method: 'DELETE' });
        showToast('Attachment removed', 'info');
        loadOffers();
    } catch (e) { showToast('Failed to remove attachment', 'error'); }
}

async function deleteOffer(offerId) {
    if (!confirm('Remove this offer?')) return;
    try {
        await apiFetch('/api/offers/' + offerId, { method: 'DELETE' });
        showToast('Offer removed', 'info');
        selectedOffers.delete(offerId);
        loadOffers();
    } catch (e) { console.error('deleteOffer:', e); showToast('Error deleting offer', 'error'); }
}

function openEditOffer(offerId) {
    // Find the offer across all groups
    let offer = null;
    for (const g of crmOffers) {
        offer = g.offers.find(o => o.id === offerId);
        if (offer) break;
    }
    if (!offer) return;
    const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    _s('eoOfferId', offerId); _s('eoVendor', offer.vendor_name || '');
    _s('eoQty', offer.qty_available || ''); _s('eoPrice', offer.unit_price || '');
    _s('eoLead', offer.lead_time || ''); _s('eoCond', offer.condition || 'New');
    _s('eoDC', offer.date_code || ''); _s('eoFirmware', offer.firmware || '');
    _s('eoHardware', offer.hardware_code || ''); _s('eoPackaging', offer.packaging || '');
    _s('eoMoq', offer.moq || ''); _s('eoWarranty', offer.warranty || '');
    _s('eoCOO', offer.country_of_origin || ''); _s('eoNotes', offer.notes || '');
    _s('eoStatus', offer.status || 'active');
    openModal('editOfferModal');
}

async function updateOffer() {
    const _v = id => document.getElementById(id)?.value || '';
    const offerId = _v('eoOfferId');
    const data = {
        vendor_name: _v('eoVendor').trim(),
        qty_available: parseInt(_v('eoQty')) || null,
        unit_price: parseFloat(_v('eoPrice')) || null,
        lead_time: _v('eoLead').trim() || null,
        condition: _v('eoCond'),
        date_code: _v('eoDC').trim() || null,
        firmware: _v('eoFirmware').trim() || null,
        hardware_code: _v('eoHardware').trim() || null,
        packaging: _v('eoPackaging').trim() || null,
        moq: parseInt(_v('eoMoq')) || null,
        warranty: _v('eoWarranty').trim() || null,
        country_of_origin: _v('eoCOO').trim() || null,
        notes: _v('eoNotes').trim() || null,
        status: _v('eoStatus'),
    };
    if (!data.vendor_name) { showToast('Vendor Name is required', 'error'); return; }
    try {
        await apiFetch('/api/offers/' + offerId, { method: 'PUT', body: data });
        closeModal('editOfferModal');
        showToast('Offer updated', 'success');
        loadOffers();
    } catch (e) { console.error('updateOffer:', e); showToast('Error updating offer', 'error'); }
}

// ── Quote Tab ──────────────────────────────────────────────────────────

async function loadQuote() {
    if (!currentReqId) return;
    const reqId = currentReqId;
    try {
        crmQuote = await apiFetch('/api/requisitions/' + reqId + '/quote');
        if (currentReqId !== reqId) return;
        renderQuote();
        updateQuoteTabBadge();
        if (crmQuote && crmQuote.status === 'won') loadBuyPlanV3();
    } catch (e) { console.error('loadQuote:', e); crmQuote = null; renderQuote(); }
}

function updateQuoteTabBadge() {
    document.querySelectorAll('#reqTabs .tab').forEach(t => {
        if (t.textContent.match(/^Quote/)) {
            t.textContent = crmQuote ? 'Quote (' + crmQuote.status + ')' : 'Quote';
        }
    });
}

async function buildQuoteFromSelected() {
    if (selectedOffers.size === 0) return;
    try {
        crmQuote = await apiFetch('/api/requisitions/' + currentReqId + '/quote', {
            method: 'POST', body: { offer_ids: Array.from(selectedOffers) }
        });
        showToast('Quote built — review and adjust sell prices', 'success');
        notifyStatusChange(crmQuote);
        const tabs = document.querySelectorAll('#reqTabs .tab');
        switchTab('quote', tabs[4]);
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) {
        console.error('buildQuoteFromSelected:', e);
        const msg = (e.message || '').toLowerCase();
        if (e.status === 400 && msg.includes('customer site')) {
            showToast('Link this requisition to a customer site first (Accounts tab)', 'error');
        } else {
            showToast('Error building quote: ' + (e.message || 'unknown error'), 'error');
        }
    }
}

function renderQuote() {
    const el = document.getElementById('quoteContent');
    if (!crmQuote) {
        const reqInfo = typeof _reqListData !== 'undefined' ? _reqListData.find(r => r.id === currentReqId) : null;
        const hasSite = reqInfo && reqInfo.customer_site_id;
        const steps = [];
        if (!hasSite) steps.push('<li style="color:var(--red)">Link a customer site to this requisition (go to Accounts)</li>');
        steps.push('<li>Log vendor offers on the <strong>Offers</strong> tab</li>');
        steps.push('<li>Select offers using the checkboxes, then click <strong>Build Quote from Selected</strong></li>');
        el.innerHTML = '<div class="empty" style="text-align:left;max-width:400px;margin:40px auto"><p style="font-weight:600;margin-bottom:8px">No quote yet</p><ol style="margin:0;padding-left:20px;line-height:1.8;font-size:12px">' + steps.join('') + '</ol></div>';
        return;
    }
    const q = crmQuote;
    const isDraft = q.status === 'draft';
    const lines = (q.line_items || []).map((item, i) => {
        const diffBadge = item._priceDiff ? ` <span style="font-size:9px;color:${item._priceDiff > 0 ? 'var(--red)' : 'var(--green)'}">${item._priceDiff > 0 ? '\u2191' : '\u2193'} $${Math.abs(item._priceDiff).toFixed(4)}</span>` : '';
        const sellInput = isDraft ? `<input type="number" step="0.0001" class="quote-sell-input" value="${item.sell_price||0}" onchange="updateQuoteLine(${i},'sell_price',this.value)">` : '$'+Number(item.sell_price||0).toFixed(4) + diffBadge;
        const leadInput = isDraft ? `<input type="text" class="quote-cell-input" value="${escAttr(item.lead_time||'')}" onchange="updateQuoteLineField(${i},'lead_time',this.value)" placeholder="—" style="width:60px">` : esc(item.lead_time || '—');
        const condInput = isDraft ? `<input type="text" class="quote-cell-input" value="${escAttr(item.condition||'')}" onchange="updateQuoteLineField(${i},'condition',this.value)" placeholder="—" style="width:50px">` : esc(item.condition || '—');
        const dcInput = isDraft ? `<input type="text" class="quote-cell-input" value="${escAttr(item.date_code||'')}" onchange="updateQuoteLineField(${i},'date_code',this.value)" placeholder="—" style="width:50px">` : esc(item.date_code || '—');
        const fwInput = isDraft ? `<input type="text" class="quote-cell-input" value="${escAttr(item.firmware||'')}" onchange="updateQuoteLineField(${i},'firmware',this.value)" placeholder="—" style="width:50px">` : esc(item.firmware || '—');
        const hwInput = isDraft ? `<input type="text" class="quote-cell-input" value="${escAttr(item.hardware_code||'')}" onchange="updateQuoteLineField(${i},'hardware_code',this.value)" placeholder="—" style="width:50px">` : esc(item.hardware_code || '—');
        return `<tr>
            <td>${esc(item.mpn)}</td>
            <td>${esc(item.manufacturer || '—')}</td>
            <td>${(item.qty||0).toLocaleString()}</td>
            <td class="quote-cost">$${Number(item.cost_price||0).toFixed(4)}</td>
            <td>${item.target_price != null ? '$'+Number(item.target_price).toFixed(4) : '—'}</td>
            <td>${sellInput}</td>
            <td class="quote-margin" id="qm-${i}" style="color:${(item.margin_pct||0) >= 20 ? 'var(--green)' : (item.margin_pct||0) >= 10 ? 'var(--amber)' : 'var(--red)'}">${Number(item.margin_pct||0).toFixed(1)}%</td>
            <td>${leadInput}</td>
            <td>${condInput}</td>
            <td>${dcInput}</td>
            <td>${fwInput}</td>
            <td>${hwInput}</td>
        </tr>`;
    }).join('');

    const statusActions = {
        draft: '<button class="btn btn-ghost" onclick="saveQuoteDraft()">Save Draft</button> <button class="btn btn-ghost" onclick="copyQuoteTable()">📋 Copy</button> <button class="btn btn-primary" onclick="sendQuoteEmail()">Send Quote</button>',
        sent: '<button class="btn btn-success" onclick="markQuoteResult(\'won\')">Mark Won</button> <button class="btn btn-danger" onclick="openLostModal()">Mark Lost</button> <button class="btn btn-ghost" onclick="reviseQuote()">Revise</button>',
        won: '<p style="color:var(--green);font-weight:600">✓ Won — $' + Number(q.won_revenue||0).toLocaleString() + '</p> <button class="btn btn-ghost" onclick="reviseQuote()">Re-quote</button> <button class="btn btn-ghost" onclick="copyQuoteTable()">📋 Copy</button> <button class="btn btn-ghost" onclick="saveQuoteDraft()">Edit</button>',
        lost: '<p style="color:var(--red);font-weight:600">✗ Lost — ' + esc(q.result_reason||'') + '</p> <button class="btn btn-ghost" onclick="reopenQuote(false)">Reopen Quote</button> <button class="btn btn-ghost" onclick="reopenQuote(true)">Reopen &amp; Revise</button>',
        revised: '<p style="color:var(--muted)">Superseded by Rev ' + (q.revision + 1) + '</p>',
    };

    const histBanner = crmQuote._isHistorical ? `
    <div style="background:var(--amber,#f59e0b)15;border:1px solid var(--amber,#f59e0b);border-radius:8px;padding:8px 14px;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:12px;color:var(--text2)">You are viewing a historical revision (Rev ${q.revision})</span>
        <button class="btn btn-ghost btn-sm" onclick="loadQuote()" style="font-size:11px">← Return to active quote</button>
    </div>` : '';

    el.innerHTML = `
    ${histBanner}
    <div class="quote-header">
        <div style="display:flex;align-items:center;gap:12px">
            <img src="/static/trio_logo.png" alt="TRIO" style="height:60px">
            <div>
                <div style="font-weight:700;font-size:13px;color:var(--text)">Trio Supply Chain Solutions</div>
                <div style="font-size:11px;color:var(--muted)">info@trioscs.com</div>
            </div>
        </div>
        <div style="text-align:right">
            <div><strong>${esc(q.quote_number)} Rev ${q.revision}</strong> <span class="status-badge status-${q.status}">${q.status}</span> <span id="quoteAutoSaveStatus" style="font-size:10px;color:var(--muted);margin-left:6px"></span></div>
            <div style="color:var(--text2);font-size:12px;margin-top:2px">
                ${esc(q.customer_name || '')}<br>
                ${esc(q.contact_name || '')} · ${q.contact_email ? '<a href="mailto:'+esc(q.contact_email)+'" onclick="autoLogEmail(\''+escAttr(q.contact_email)+'\',\''+escAttr(q.contact_name || '')+'\')">'+esc(q.contact_email)+'</a>' : ''}
            </div>
        </div>
    </div>
    <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
    <table class="tbl quote-table" style="font-size:11px">
        <thead><tr><th>MPN</th><th>Mfr</th><th>Qty</th><th>Cost</th><th>Target</th><th>Sell</th><th>Margin</th><th>Lead</th><th>Cond</th><th>DC</th><th>FW</th><th>HW</th></tr></thead>
        <tbody>${lines}</tbody>
    </table>
    </div>
    <div class="quote-markup">
        Quick Margin: <input type="number" id="quickMarkup" value="20" style="width:50px" min="0" max="99">%
        <button class="btn btn-ghost btn-sm" onclick="applyMarkup()">Apply to All</button>
    </div>
    <div class="quote-totals">
        <div>Cost: <strong>$${Number(q.total_cost||0).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
        <div>Revenue: <strong>$${Number(q.subtotal||0).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
        <div>Gross Profit: <strong style="color:var(--green)">$${Number((q.subtotal||0)-(q.total_cost||0)).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
        <div>Margin: <strong>${Number(q.total_margin_pct||0).toFixed(1)}%</strong></div>
    </div>
    <div class="quote-terms">
        <label>Terms <input id="qtTerms" value="${escAttr(q.payment_terms||'')}" placeholder="Net 30"></label>
        <label>Shipping <input id="qtShip" value="${escAttr(q.shipping_terms||'')}" placeholder="FOB Origin"></label>
        <label>Valid <input id="qtValid" type="number" value="${q.validity_days||7}" style="width:50px"> days</label>
    </div>
    <div class="quote-notes">
        <label>Notes<br><textarea id="qtNotes" rows="2" style="width:100%">${esc(q.notes||'')}</textarea></label>
    </div>
    <div class="quote-actions">${statusActions[q.status] || ''}</div>
    <div id="quoteHistorySection"></div>
    <div id="buyPlanSection"></div>`;
    loadQuoteHistory();
}

let _quoteAutoSaveTimer = null;
function _scheduleQuoteAutoSave() {
    clearTimeout(_quoteAutoSaveTimer);
    const indicator = document.getElementById('quoteAutoSaveStatus');
    if (indicator) indicator.textContent = '';
    _quoteAutoSaveTimer = setTimeout(async () => {
        if (!crmQuote || crmQuote.status !== 'draft') return;
        if (indicator) indicator.textContent = 'Saving\u2026';
        try {
            await saveQuoteDraft();
            if (indicator) indicator.textContent = 'Saved';
            setTimeout(() => { if (indicator && indicator.textContent === 'Saved') indicator.textContent = ''; }, 2000);
        } catch (e) {
            if (indicator) indicator.textContent = 'Save failed';
        }
    }, 500);
}

function updateQuoteLine(idx, field, value) {
    if (!crmQuote) return;
    const item = crmQuote.line_items[idx];
    if (field === 'sell_price') {
        item.sell_price = parseFloat(value) || 0;
        const cost = item.cost_price || 0;
        item.margin_pct = item.sell_price > 0 ? ((item.sell_price - cost) / item.sell_price * 100) : 0;
        const mEl = document.getElementById('qm-' + idx);
        if (mEl) {
            const mPct = item.margin_pct;
            const mColor = mPct >= 20 ? 'var(--green)' : mPct >= 10 ? 'var(--amber)' : 'var(--red)';
            mEl.innerHTML = `${mPct.toFixed(1)}% <span style="font-size:9px;color:${mColor}">(${mPct >= 20 ? 'good' : mPct >= 10 ? 'low' : 'thin'})</span>`;
        }
        refreshQuoteTotals();
    }
    _scheduleQuoteAutoSave();
}

function updateQuoteLineField(idx, field, value) {
    if (!crmQuote) return;
    crmQuote.line_items[idx][field] = value;
    _scheduleQuoteAutoSave();
}

function refreshQuoteTotals() {
    if (!crmQuote) return;
    let totalCost = 0, totalSell = 0;
    crmQuote.line_items.forEach(item => {
        totalCost += (item.cost_price || 0) * (item.qty || 0);
        totalSell += (item.sell_price || 0) * (item.qty || 0);
    });
    crmQuote.subtotal = totalSell;
    crmQuote.total_cost = totalCost;
    crmQuote.total_margin_pct = totalSell > 0 ? ((totalSell - totalCost) / totalSell * 100) : 0;
    const totalsEl = document.querySelector('.quote-totals');
    if (totalsEl) {
        const gp = totalSell - totalCost;
        totalsEl.innerHTML = `
            <div>Cost: <strong>$${Number(totalCost).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
            <div>Revenue: <strong>$${Number(totalSell).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
            <div>Gross Profit: <strong style="color:var(--green)">$${Number(gp).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
            <div>Margin: <strong>${Number(crmQuote.total_margin_pct).toFixed(1)}%</strong></div>`;
    }
}

function applyMarkup() {
    if (!crmQuote) return;
    const pct = parseFloat(document.getElementById('quickMarkup')?.value) || 0;
    crmQuote.line_items.forEach(item => {
        item.sell_price = pct >= 100 ? 0 : Math.round((item.cost_price || 0) / (1 - pct / 100) * 10000) / 10000;
        item.margin_pct = item.sell_price > 0 ? ((item.sell_price - (item.cost_price||0)) / item.sell_price * 100) : 0;
    });
    renderQuote();
}

async function saveQuoteDraft() {
    if (!crmQuote) return;
    if (saveQuoteDraft._busy) return; saveQuoteDraft._busy = true;
    try {
        await apiFetch('/api/quotes/' + crmQuote.id, {
            method: 'PUT', body: {
                line_items: crmQuote.line_items,
                payment_terms: document.getElementById('qtTerms')?.value || '',
                shipping_terms: document.getElementById('qtShip')?.value || '',
                validity_days: parseInt(document.getElementById('qtValid')?.value) || 7,
                notes: document.getElementById('qtNotes')?.value || '',
            }
        });
        showToast('Draft saved', 'success');
        loadQuote();
    } catch (e) { console.error('saveQuoteDraft:', e); showToast('Error saving draft', 'error'); }
    finally { saveQuoteDraft._busy = false; }
}

function copyQuoteTable() {
    if (!crmQuote) return;
    let table = 'Part Number    | Mfr  | Qty   | Unit Price | Lead Time\n';
    table += '─'.repeat(55) + '\n';
    (crmQuote.line_items || []).forEach(item => {
        const mpn = (item.mpn || '').padEnd(15);
        const mfr = (item.manufacturer || '—').substring(0, 5).padEnd(5);
        const qty = String(item.qty || 0).padStart(6);
        const price = ('$' + Number(item.sell_price || 0).toFixed(item.sell_price >= 1 ? 2 : 4)).padStart(11);
        const lead = item.lead_time || '—';
        table += mpn + '| ' + mfr + '| ' + qty + ' | ' + price + ' | ' + lead + '\n';
    });
    table += '─'.repeat(55) + '\n';
    table += ''.padStart(25) + 'Total: $' + Number(crmQuote.subtotal || 0).toLocaleString(undefined, {minimumFractionDigits: 2}) + '\n';
    const terms = [
        document.getElementById('qtTerms')?.value,
        document.getElementById('qtShip')?.value,
        'Valid ' + (document.getElementById('qtValid')?.value || 7) + ' days'
    ].filter(Boolean).join(' · ');
    table += 'Terms: ' + terms + '\n';
    navigator.clipboard.writeText(table).then(() => {
        showToast('Quote table copied to clipboard', 'success');
    }).catch(() => {
        showToast('Clipboard access denied — copy manually', 'error');
    });
}

function sendQuoteEmail() {
    if (!crmQuote) return;
    // Populate the send-quote modal with contact options
    const sel = document.getElementById('sqContactSelect');
    sel.innerHTML = '';
    const q = crmQuote;
    // Primary site contact
    if (q.contact_email) {
        const opt = document.createElement('option');
        opt.value = q.contact_email;
        opt.dataset.name = q.contact_name || '';
        opt.textContent = (q.contact_name ? q.contact_name + ' — ' : '') + q.contact_email;
        sel.appendChild(opt);
    }
    // Additional site contacts
    (q.site_contacts || []).forEach(function(c) {
        if (c.email && c.email !== q.contact_email) {
            const opt = document.createElement('option');
            opt.value = c.email;
            opt.dataset.name = c.full_name || '';
            opt.textContent = (c.full_name ? c.full_name + ' — ' : '') + c.email;
            sel.appendChild(opt);
        }
    });
    // Manual entry option
    var manOpt = document.createElement('option');
    manOpt.value = '__manual__';
    manOpt.textContent = 'Enter email manually...';
    sel.appendChild(manOpt);

    const sqNum = document.getElementById('sqQuoteNum'); if (sqNum) sqNum.textContent = q.quote_number + ' Rev ' + q.revision;
    const sqMan = document.getElementById('sqManualEmail'); if (sqMan) sqMan.value = '';
    onSqContactChange();
    openModal('sendQuoteModal');
}

function onSqContactChange() {
    var sel = document.getElementById('sqContactSelect');
    if (!sel) return;
    var manual = sel.value === '__manual__';
    const mr = document.getElementById('sqManualRow'); if (mr) { if (manual) mr.classList.remove('u-hidden'); else mr.classList.add('u-hidden'); }
    if (manual) setTimeout(function() { document.getElementById('sqManualEmail')?.focus(); }, 50);
}

async function confirmSendQuote() {
    if (!crmQuote) return;
    var sel = document.getElementById('sqContactSelect');
    var toEmail, toName;
    if (sel?.value === '__manual__') {
        toEmail = document.getElementById('sqManualEmail')?.value?.trim() || '';
        toName = '';
        if (!toEmail) { showToast('Enter an email address', 'error'); return; }
    } else {
        toEmail = sel.value;
        toName = sel.options[sel.selectedIndex].dataset.name || '';
    }
    var btn = document.querySelector('#sendQuoteModal .btn-primary');
    await guardBtn(btn, 'Sending…', async () => {
        closeModal('sendQuoteModal');
        // Optimistic UI: immediately show "Sending..." status
        const prevStatus = crmQuote.status;
        const badge = document.querySelector('.status-badge.status-' + prevStatus);
        if (badge) { badge.textContent = 'sending\u2026'; badge.className = 'status-badge status-sent'; }
        try {
            await saveQuoteDraft();
            var sendData = await apiFetch('/api/quotes/' + crmQuote.id + '/send', {
                method: 'POST',
                body: { to_email: toEmail, to_name: toName }
            });
            showToast('Quote sent to ' + (sendData.sent_to || toEmail), 'success');
            notifyStatusChange(sendData);
            loadQuote();
        } catch (e) {
            // Revert optimistic update on failure
            if (badge) { badge.textContent = prevStatus; badge.className = 'status-badge status-' + prevStatus; }
            logCatchError('sendQuoteEmail', e); showToast('Error sending quote: ' + (e.message||''), 'error');
        }
    });
}

// ── Quote History ──────────────────────────────────────────────────────
async function loadQuoteHistory() {
    if (!currentReqId) return;
    const el = document.getElementById('quoteHistorySection');
    if (!el) return;
    try {
        const quotes = await apiFetch('/api/requisitions/' + currentReqId + '/quotes');
        if (!quotes || quotes.length <= 1) { el.innerHTML = ''; return; }
        const rows = quotes.map(q => {
            const isCurrent = crmQuote && q.id === crmQuote.id;
            const date = q.sent_at ? new Date(q.sent_at).toLocaleDateString() : (q.created_at ? new Date(q.created_at).toLocaleDateString() : '—');
            const total = q.subtotal != null ? '$' + Number(q.subtotal).toLocaleString(undefined,{minimumFractionDigits:2}) : '—';
            const margin = q.total_margin_pct != null ? Number(q.total_margin_pct).toFixed(1) + '%' : '—';
            const statusCls = q.status === 'won' ? 'color:var(--green)' : q.status === 'lost' ? 'color:var(--red)' : '';
            return `<tr style="${isCurrent ? 'background:var(--teal-light,#d1ecf1)' : ''}">
                <td style="padding:4px 8px;font-size:11px">${esc(q.quote_number)} Rev ${q.revision}</td>
                <td style="padding:4px 8px;font-size:11px">${date}</td>
                <td style="padding:4px 8px;font-size:11px">${total}</td>
                <td style="padding:4px 8px;font-size:11px">${margin}</td>
                <td style="padding:4px 8px;font-size:11px;${statusCls};font-weight:600">${q.status}</td>
                <td style="padding:4px 8px;font-size:11px">${!isCurrent ? '<button class="btn btn-ghost btn-sm" onclick="loadSpecificQuote('+q.id+')" style="padding:1px 6px;font-size:10px">View</button>' : '<em style="color:var(--muted);font-size:10px">current</em>'}</td>
            </tr>`;
        }).join('');
        el.innerHTML = `
        <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:12px">
            <div style="font-weight:600;font-size:12px;margin-bottom:6px">Quote History (${quotes.length} revisions)</div>
            <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
            <table class="tbl" style="font-size:11px">
                <thead><tr><th>Quote #</th><th>Date</th><th>Total</th><th>Margin</th><th>Status</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
            </div>
        </div>`;
    } catch (e) { logCatchError('loadQuoteHistory', e); showToast('Failed to load quote history', 'error'); }
}

async function loadSpecificQuote(quoteId) {
    try {
        const quotes = await apiFetch('/api/requisitions/' + currentReqId + '/quotes');
        const q = quotes.find(x => x.id === quoteId);
        if (q) {
            // Find the latest (highest revision) quote for diff comparison
            const latest = quotes.reduce((a, b) => (b.revision > a.revision ? b : a), quotes[0]);
            if (latest && latest.id !== q.id) {
                const latestItems = latest.line_items || [];
                (q.line_items || []).forEach(item => {
                    const match = latestItems.find(li => li.mpn === item.mpn);
                    if (match && match.sell_price != null && item.sell_price != null) {
                        const diff = item.sell_price - match.sell_price;
                        if (Math.abs(diff) >= 0.0001) {
                            item._priceDiff = diff;
                        }
                    }
                });
            }
            crmQuote = q;
            crmQuote._isHistorical = true;
            renderQuote();
        }
    } catch (e) { logCatchError('loadSpecificQuote', e); showToast('Failed to load quote', 'error'); }
}

async function markQuoteResult(result) {
    if (!crmQuote) return;
    if (result === 'won') { markQuoteWonV3(); return; }
    if (markQuoteResult._busy) return; markQuoteResult._busy = true;
    // Optimistic UI
    const prevStatus = crmQuote.status;
    const badge = document.querySelector('.status-badge.status-' + prevStatus);
    if (badge) { badge.textContent = result === 'lost' ? 'updating\u2026' : result; badge.className = 'status-badge status-' + result; }
    try {
        const resultData = await apiFetch('/api/quotes/' + crmQuote.id + '/result', {
            method: 'POST', body: { result }
        });
        showToast('Quote updated', 'info');
        notifyStatusChange(resultData);
        loadQuote();
        _refreshCustPipeline();
    } catch (e) {
        // Revert on failure
        if (badge) { badge.textContent = prevStatus; badge.className = 'status-badge status-' + prevStatus; }
        console.error('markQuoteResult:', e); showToast('Error updating result', 'error');
    }
    finally { markQuoteResult._busy = false; }
}

function openLostModal() {
    openModal('lostModal');
}

// ── Buy Plan ────────────────────────────────────────────────────────────

let _currentBuyPlan = null;

function openBuyPlanModal() {
    if (!crmQuote) return;
    const modal = document.getElementById('buyPlanModal');
    openModal('buyPlanModal');
    const items = (crmQuote.line_items || []).map((item, i) => {
        const qty = item.qty || 0;
        return `
        <tr>
            <td><input type="checkbox" class="bp-check" data-idx="${i}" checked></td>
            <td>${esc(item.mpn)}</td>
            <td>${esc(item.manufacturer || '\u2014')}</td>
            <td>${esc(item.vendor_name || '\u2014')}</td>
            <td>${qty.toLocaleString()}</td>
            <td><input type="number" class="bp-plan-qty" data-idx="${i}" value="${qty}" min="1" style="width:70px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px;text-align:right" oninput="_debouncedUpdateBpTotals()"></td>
            <td>$${Number(item.cost_price||0).toFixed(4)}</td>
            <td class="bp-line-total">$${(qty * Number(item.cost_price||0)).toFixed(2)}</td>
            <td>${esc(item.lead_time || '\u2014')}</td>
        </tr>`;
    }).join('');
    const bpI = document.getElementById('bpItems'); if (bpI) bpI.innerHTML = items;
    const bpN = document.getElementById('bpSalespersonNotes'); if (bpN) bpN.value = '';
    updateBpTotals();
}

function updateBpTotals() {
    let total = 0;
    document.querySelectorAll('.bp-plan-qty').forEach(input => {
        const idx = parseInt(input.dataset.idx);
        const item = (crmQuote.line_items || [])[idx];
        if (!item) return;
        const qty = parseInt(input.value) || 0;
        const lineTotal = qty * Number(item.cost_price || 0);
        total += lineTotal;
        const row = input.closest('tr');
        const totalCell = row.querySelector('.bp-line-total');
        if (totalCell) totalCell.textContent = '$' + lineTotal.toFixed(2);
    });
    const bpTot = document.getElementById('bpTotal'); if (bpTot) bpTot.textContent = '$' + total.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

async function submitBuyPlan() {
    if (!crmQuote) return;
    const checks = document.querySelectorAll('.bp-check:checked');
    const selectedIndices = Array.from(checks).map(c => parseInt(c.dataset.idx));
    if (!selectedIndices.length) { showToast('Select at least one item', 'error'); return; }

    // Get offer IDs and plan quantities from line items
    const offerIds = [];
    const planQtys = {};
    selectedIndices.forEach(i => {
        const item = (crmQuote.line_items || [])[i];
        if (item && item.offer_id) {
            offerIds.push(item.offer_id);
            const qtyInput = document.querySelector('.bp-plan-qty[data-idx="' + i + '"]');
            if (qtyInput) planQtys[item.offer_id] = parseInt(qtyInput.value) || item.qty || 0;
        }
    });
    if (!offerIds.length) { showToast('No offer IDs found', 'error'); return; }

    const salespersonNotes = document.getElementById('bpSalespersonNotes')?.value?.trim() || '';
    var btn = document.querySelector('#buyPlanModal .btn-success');

    await guardBtn(btn, 'Submitting…', async () => {
        try {
            const res = await apiFetch('/api/quotes/' + crmQuote.id + '/buy-plan', {
                method: 'POST', body: {
                    offer_ids: offerIds,
                    plan_qtys: planQtys,
                    salesperson_notes: salespersonNotes
                }
            });
            showToast('Buy plan submitted for approval!', 'success');
            closeModal('buyPlanModal');
            notifyStatusChange(res);
            loadQuote();
        } catch (e) {
            logCatchError('submitBuyPlan', e);
            showToast('Failed to submit buy plan', 'error');
        }
    });
}

async function loadBuyPlan() {
    if (!crmQuote) return;
    try {
        _currentBuyPlan = await apiFetch('/api/buy-plans/for-quote/' + crmQuote.id);
    } catch (e) { logCatchError('loadBuyPlan', e); showToast('Failed to load buy plan', 'error'); _currentBuyPlan = null; }
    renderBuyPlanStatus();
}

var _bpRenderTarget = 'buyPlanSection';
function renderBuyPlanStatus(targetId) {
    if (targetId) _bpRenderTarget = targetId;
    const el = document.getElementById(_bpRenderTarget);
    if (!el) return;
    if (!_currentBuyPlan) { el.innerHTML = ''; return; }
    const bp = _currentBuyPlan;
    const isAdmin = window.__isAdmin;
    const isBuyer = ['buyer','trader','manager','admin'].includes(window.userRole);

    // Workflow: Draft -> Pending -> Approved -> Completed (only these four)
    const statusColors = {
        draft: 'var(--muted)',
        pending_approval: 'var(--amber)',
        approved: 'var(--green)',
        po_entered: 'var(--green)',
        po_confirmed: 'var(--green)',
        complete: 'var(--green)',
        rejected: 'var(--red)',
        cancelled: 'var(--muted)',
    };
    const statusLabels = {
        draft: 'Draft',
        pending_approval: 'Pending',
        approved: 'Approved',
        po_entered: 'Approved',
        po_confirmed: 'Approved',
        complete: 'Completed',
        rejected: 'Rejected',
        cancelled: 'Cancelled',
    };

    const statusColor = statusColors[bp.status] || 'var(--muted)';
    const statusLabel = statusLabels[bp.status] || bp.status;

    // Deal context header
    let contextHtml = '';
    if (bp.customer_name || bp.quote_number || bp.sales_order_number) {
        contextHtml = `<div class="info-card">
            ${bp.customer_name ? '<div><strong>Customer:</strong> '+esc(bp.customer_name)+'</div>' : ''}
            ${bp.quote_number ? '<div><strong>Quote:</strong> '+esc(bp.quote_number)+'</div>' : ''}
            ${bp.sales_order_number ? '<div><strong>Acctivate SO#:</strong> '+esc(bp.sales_order_number)+'</div>' : ''}
        </div>`;
    }

    // Margin summary bar (when revenue data available)
    let marginHtml = '';
    if (bp.total_revenue > 0) {
        const profitColor = bp.total_profit >= 0 ? 'var(--green)' : 'var(--red)';
        marginHtml = `<div class="stat-row">
            <div><strong>Cost:</strong> $${bp.total_cost.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
            <div><strong>Revenue:</strong> $${bp.total_revenue.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
            <div style="color:${profitColor}"><strong>Profit:</strong> $${bp.total_profit.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
            <div><strong>Margin:</strong> ${bp.overall_margin_pct}%</div>
        </div>`;
    }

    const hidePoCol = bp.is_stock_sale;
    let itemsHtml = (bp.line_items || []).map((item, i) => {
        const planQty = item.plan_qty || item.qty || 0;
        let poCell = '';
        if (!hidePoCol) {
            const poEditable = isBuyer && (bp.status === 'approved' || bp.status === 'po_entered' || bp.status === 'po_confirmed');
            poCell = poEditable
                ? `<input type="text" class="po-input" data-idx="${i}" placeholder="PO#" value="${esc(item.po_number||'')}" style="width:100px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px">`
                : (item.po_number ? `<span style="font-weight:600">${esc(item.po_number)}</span>` : '\u2014');
            const verifyIcon = item.po_verified
                ? '<span style="color:var(--green)" title="Verified">\u2713</span>'
                : (item.po_number ? '<span style="color:var(--amber)" title="Unverified">\u23F3</span>' : '');
            let poDetails = '';
            if (item.po_verified) {
                poDetails = `<div style="font-size:10px;color:var(--muted)">Sent to ${esc(item.po_recipient||'')} at ${item.po_sent_at||''}</div>`;
            } else if (item.po_entered_at) {
                poDetails = `<div style="font-size:10px;color:var(--muted)">Entered ${fmtDateTime(item.po_entered_at)}</div>`;
            }
            poCell = `<td>${poCell} ${verifyIcon}${poDetails}</td>`;
        }
        const vsBadge = item.vendor_score != null
            ? `<span class="badge ${item.vendor_score >= 66 ? 'b-proven' : item.vendor_score >= 33 ? 'b-developing' : 'b-caution'}">${Math.round(item.vendor_score)}</span>`
            : '<span class="badge b-new">New</span>';
        return `<tr>
            <td>${esc(item.mpn)}</td>
            <td>${esc(item.vendor_name)}</td>
            <td>${vsBadge}</td>
            <td style="font-size:11px">${esc(item.entered_by_name||'\u2014')}</td>
            <td>${planQty.toLocaleString()}</td>
            <td>$${Number(item.cost_price||0).toFixed(4)}</td>
            <td>${esc(item.lead_time||'\u2014')}</td>
            ${poCell}
        </tr>`;
    }).join('');

    // Notes sections
    let notesHtml = '';
    if (bp.salesperson_notes) {
        notesHtml += `<div style="background:#f0f9ff;padding:8px 10px;border-left:3px solid #2563eb;border-radius:4px;margin-bottom:8px;font-size:12px"><strong>Salesperson:</strong> ${esc(bp.salesperson_notes)}</div>`;
    }
    if (bp.manager_notes) {
        notesHtml += `<div style="background:#f0fdf4;padding:8px 10px;border-left:3px solid #16a34a;border-radius:4px;margin-bottom:8px;font-size:12px"><strong>Manager:</strong> ${esc(bp.manager_notes)}</div>`;
    }
    if (bp.rejection_reason) {
        notesHtml += `<div style="background:#fef2f2;padding:8px 10px;border-left:3px solid var(--red);border-radius:4px;margin-bottom:8px;font-size:12px"><strong>Rejected:</strong> ${esc(bp.rejection_reason)}</div>`;
    }
    if (bp.cancellation_reason) {
        notesHtml += `<div style="background:#f3f4f6;padding:8px 10px;border-left:3px solid var(--muted);border-radius:4px;margin-bottom:8px;font-size:12px"><strong>Cancelled:</strong> ${esc(bp.cancellation_reason)}${bp.cancelled_by ? ' by '+esc(bp.cancelled_by) : ''}</div>`;
    }

    let actionsHtml = '';
    const canApprove = isAdmin || window.userRole === 'manager';
    const isSubmitter = bp.submitted_by_id === window.__userId;
    const canSubmitDraft = (isSubmitter || canApprove) && bp.status === 'draft';

    // 1. Draft -> Ready to send (Sales: creator, or Admin)
    if (canSubmitDraft) {
        actionsHtml += `<div style="margin-top:12px"><button class="btn btn-primary" onclick="submitDraftBuyPlan()">Ready to send</button></div>`;
    }
    // 2. Pending -> Approve/Reject (Admin/Manager only)
    if (canApprove && bp.status === 'pending_approval') {
        actionsHtml += `
            <div style="margin-top:12px">
                <div class="field" style="margin-bottom:8px">
                    <label style="font-weight:600;font-size:12px">Acctivate Sales Order # <span style="color:var(--red)">*</span></label>
                    <input type="text" id="bpSalesOrderNumber" placeholder="Enter Acctivate SO#" style="width:200px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)">
                </div>
                <div style="margin-bottom:8px">
                    <textarea id="bpManagerNotes" placeholder="Manager notes (optional)\u2026" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;min-height:40px"></textarea>
                </div>
                <div style="display:flex;gap:8px">
                    <button class="btn btn-success" onclick="approveBuyPlan()">Approve</button>
                    <button class="btn btn-danger" onclick="openRejectBuyPlanModal()">Reject</button>
                </div>
            </div>`;
    }
    // 3. Approved -> Completed: Admin/Manager can complete from approved or PO-entered; Buyer only after PO entered (po_entered/po_confirmed)
    if (canApprove && (bp.status === 'approved' || bp.status === 'po_entered' || bp.status === 'po_confirmed')) {
        actionsHtml += `<div style="margin-top:12px"><button class="btn btn-success" onclick="completeBuyPlan()">Mark Complete</button></div>`;
    }
    if (isBuyer && !canApprove && (bp.status === 'po_entered' || bp.status === 'po_confirmed')) {
        actionsHtml += `<div style="margin-top:12px"><button class="btn btn-success" onclick="completeBuyPlan()">Mark Complete</button></div>`;
    }
    // Cancel: Pending — submitter or admin/manager
    if (bp.status === 'pending_approval') {
        const canCancel = canApprove || isSubmitter;
        if (canCancel) {
            actionsHtml += `<div style="margin-top:8px"><button class="btn btn-ghost" onclick="cancelBuyPlan()">Cancel Plan</button></div>`;
        }
    }
    if (!bp.is_stock_sale && isBuyer && (bp.status === 'approved' || bp.status === 'po_entered')) {
        const hasAnyPO = (bp.line_items || []).some(li => li.po_number);
        actionsHtml += `
            <div style="margin-top:12px">
                <button class="btn btn-primary" onclick="saveBuyPlanPOs()">Save PO Numbers</button>
                <button class="btn btn-ghost" onclick="verifyBuyPlanPOs()" ${hasAnyPO ? '' : 'disabled style="opacity:.5" title="Add PO numbers first"'}>Verify PO Sent</button>
            </div>`;
    }
    // Cancel button for approved plans with no POs (admin/manager only)
    if (canApprove && bp.status === 'approved') {
        const hasPOs = (bp.line_items || []).some(li => li.po_number);
        if (!hasPOs) {
            actionsHtml += `<div style="margin-top:8px"><button class="btn btn-ghost" onclick="cancelBuyPlan()">Cancel Plan</button></div>`;
        }
    }
    // Resubmit for rejected/cancelled (submitter or admin/manager)
    if ((bp.status === 'rejected' || bp.status === 'cancelled') && (isSubmitter || canApprove)) {
        actionsHtml += `<div style="margin-top:12px"><button class="btn btn-primary" onclick="resubmitBuyPlan()">Resubmit Buy Plan</button></div>`;
    }

    el.innerHTML = `
        <div class="card" style="margin-top:16px;border-left:4px solid ${statusColor}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div>
                    <strong>Buy Plan</strong>
                    <span class="status-badge" style="background:${statusColor};color:#fff;margin-left:8px">${statusLabel}</span>
                    ${bp.is_stock_sale ? '<span class="status-badge" style="background:#7c3aed;color:#fff;margin-left:4px">Stock Sale</span>' : ''}
                </div>
                <span style="font-size:11px;color:var(--muted)">${bp.status === 'draft' ? 'Created by ' : 'Submitted by '}${esc(bp.submitted_by||'')} ${bp.submitted_at ? '\xB7 '+fmtDateTime(bp.submitted_at) : ''}</span>
            </div>
            ${contextHtml}
            ${marginHtml}
            ${notesHtml}
            <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
            <table class="tbl" style="margin-bottom:0">
                <thead><tr><th>MPN</th><th>Vendor</th><th>V.Score</th><th>Buyer</th><th>Plan Qty</th><th>Cost</th><th>Lead</th>${hidePoCol ? '' : '<th>PO</th>'}</tr></thead>
                <tbody>${itemsHtml}</tbody>
            </table>
            </div>
            ${actionsHtml}
        </div>`;
}

async function submitDraftBuyPlan() {
    if (!_currentBuyPlan || _currentBuyPlan.status !== 'draft') return;
    if (submitDraftBuyPlan._busy) return; submitDraftBuyPlan._busy = true;
    try {
        await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/submit', { method: 'PUT' });
        showToast('Buy plan sent for approval', 'success');
        _currentBuyPlan = await apiFetch('/api/buy-plans/' + _currentBuyPlan.id);
        renderBuyPlanStatus(_bpRenderTarget);
        if (typeof loadBuyPlan === 'function') loadBuyPlan();
    } catch (e) { showToast('Failed to submit: ' + (e.message || e), 'error'); }
    finally { submitDraftBuyPlan._busy = false; }
}

async function approveBuyPlan() {
    if (!_currentBuyPlan) return;
    const soNumber = document.getElementById('bpSalesOrderNumber')?.value?.trim() || '';
    if (!soNumber) { showToast('Acctivate Sales Order # is required', 'error'); return; }
    if (approveBuyPlan._busy) return; approveBuyPlan._busy = true;
    const notes = document.getElementById('bpManagerNotes')?.value?.trim() || '';
    try {
        await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/approve', {
            method: 'PUT', body: { sales_order_number: soNumber, manager_notes: notes }
        });
        showToast('Buy plan approved — buyers notified', 'success');
        loadBuyPlan();
    } catch (e) { showToast('Failed to approve: ' + (e.message || e), 'error'); }
    finally { approveBuyPlan._busy = false; }
}

function openRejectBuyPlanModal() {
    // Show inline reject form instead of prompt()
    const container = document.getElementById('bpRejectForm');
    if (container) {
        container.style.display = '';
        var inp = container.querySelector('textarea');
        if (inp) { inp.value = ''; inp.focus(); }
        return;
    }
    // Fallback: create the form dynamically
    const section = document.querySelector('#buyPlanSection .card') || document.getElementById('buyPlanSection');
    if (!section) return;
    const form = document.createElement('div');
    form.id = 'bpRejectForm';
    form.style.cssText = 'margin-top:12px;padding:10px;border:1px solid var(--red);border-radius:8px;background:var(--red-light,#fef2f2)';
    form.innerHTML = `
        <div style="font-weight:600;font-size:12px;margin-bottom:6px;color:var(--red)">Reject Buy Plan</div>
        <textarea placeholder="Reason for rejection..." style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;min-height:40px;margin-bottom:8px"></textarea>
        <div style="display:flex;gap:8px">
            <button class="btn btn-danger btn-sm" onclick="rejectBuyPlan(this.closest('#bpRejectForm').querySelector('textarea').value)">Confirm Reject</button>
            <button class="btn btn-ghost btn-sm" onclick="this.closest('#bpRejectForm').style.display='none'">Cancel</button>
        </div>`;
    section.appendChild(form);
    form.querySelector('textarea').focus();
}

async function rejectBuyPlan(reason) {
    if (!_currentBuyPlan) return;
    if (rejectBuyPlan._busy) return; rejectBuyPlan._busy = true;
    try {
        await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/reject', {
            method: 'PUT', body: { reason }
        });
        showToast('Buy plan rejected', 'info');
        loadBuyPlan();
    } catch (e) { showToast('Failed to reject', 'error'); }
    finally { rejectBuyPlan._busy = false; }
}

async function saveBuyPlanPOs() {
    if (!_currentBuyPlan) return;
    if (saveBuyPlanPOs._busy) return; saveBuyPlanPOs._busy = true;
    const inputs = document.querySelectorAll('.po-input');
    const entries = [];
    for (const input of inputs) {
        const idx = parseInt(input.dataset.idx);
        const po = input.value.trim();
        entries.push({ line_index: idx, po_number: po || null });
    }
    if (!entries.length) { showToast('No PO fields found', 'error'); return; }
    try {
        const result = await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/po-bulk', {
            method: 'PUT', body: { entries }
        });
        showToast(result.changes + ' PO number(s) updated', 'success');
        loadBuyPlan();
    } catch (e) { showToast('Failed to save POs: ' + (e.message || e), 'error'); }
    finally { saveBuyPlanPOs._busy = false; }
}

async function completeBuyPlan() {
    if (!_currentBuyPlan) return;
    if (!confirm('Mark this buy plan as complete?')) return;
    if (completeBuyPlan._busy) return; completeBuyPlan._busy = true;
    try {
        await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/complete', { method: 'PUT' });
        showToast('Buy plan marked complete', 'success');
        loadBuyPlan();
    } catch (e) { showToast('Failed to complete: ' + (e.message || e), 'error'); }
    finally { completeBuyPlan._busy = false; }
}

async function cancelBuyPlan() {
    if (!_currentBuyPlan) return;
    const reason = prompt('Cancellation reason (optional):');
    if (reason === null) return;
    try {
        await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/cancel', {
            method: 'PUT', body: { reason }
        });
        showToast('Buy plan cancelled', 'info');
        loadBuyPlan();
    } catch (e) { showToast('Failed to cancel: ' + (e.message || e), 'error'); }
}

async function resubmitBuyPlan() {
    if (!_currentBuyPlan) return;
    const notes = prompt('Updated notes for resubmission (optional):');
    if (notes === null) return;
    try {
        const res = await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/resubmit', {
            method: 'PUT', body: { salesperson_notes: notes }
        });
        showToast('Buy plan resubmitted for approval', 'success');
        _currentBuyPlan = await apiFetch('/api/buy-plans/' + res.new_plan_id);
        renderBuyPlanStatus();
    } catch (e) { showToast('Failed to resubmit: ' + (e.message || e), 'error'); }
}

async function verifyBuyPlanPOs() {
    if (!_currentBuyPlan) return;
    showToast('Scanning sent emails for PO verification…', 'info');
    try {
        const result = await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/verify-po');
        const verified = Object.values(result.verifications || {}).filter(v => v.verified).length;
        const total = Object.keys(result.verifications || {}).length;
        showToast(verified + '/' + total + ' POs verified', verified === total ? 'success' : 'info');
        _currentBuyPlan = { ..._currentBuyPlan, line_items: result.line_items, status: result.status };
        renderBuyPlanStatus();
    } catch (e) { showToast('Verification failed', 'error'); }
}

// ── Buy Plan V3 (AI-powered) ─────────────────────────────────────────

let _currentBuyPlanV3 = null;
var _bpV3RenderTarget = 'buyPlanSection';

async function markQuoteWonV3() {
    if (!crmQuote) return;
    if (markQuoteWonV3._busy) return; markQuoteWonV3._busy = true;
    const el = document.getElementById('buyPlanSection');
    if (el) el.innerHTML = '<div class="spinner-row"><div class="spinner"></div>Building AI-optimized buy plan\u2026</div>';
    try {
        const resultData = await apiFetch('/api/quotes/' + crmQuote.id + '/result', {
            method: 'POST', body: { result: 'won' }
        });
        notifyStatusChange(resultData);
        crmQuote.status = 'won';
        renderQuote();
        _currentBuyPlanV3 = await apiFetch('/api/quotes/' + crmQuote.id + '/buy-plan-v3/build', { method: 'POST' });
        renderBuyPlanV3Status();
        showToast('AI buy plan built \u2014 review and submit', 'success');
    } catch (e) {
        logCatchError('markQuoteWonV3', e);
        showToast('Failed: ' + (e.message || e), 'error');
        if (el) el.innerHTML = '';
    } finally { markQuoteWonV3._busy = false; }
}

async function loadBuyPlanV3() {
    if (!crmQuote) return;
    try {
        const resp = await apiFetch('/api/buy-plans-v3?quote_id=' + crmQuote.id);
        const plans = (resp.items || []);
        if (plans.length > 0) {
            _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + plans[0].id);
        } else {
            _currentBuyPlanV3 = null;
        }
    } catch (e) { logCatchError('loadBuyPlanV3', e); _currentBuyPlanV3 = null; }
    renderBuyPlanV3Status();
}

function _bpV3StatusColor(status) {
    return { draft:'var(--muted)', pending:'var(--amber)', active:'var(--green)',
        halted:'var(--red)', completed:'var(--green)', cancelled:'var(--muted)' }[status] || 'var(--muted)';
}
function _bpV3StatusLabel(status) {
    return { draft:'Draft', pending:'Pending Approval', active:'Active',
        halted:'Halted', completed:'Completed', cancelled:'Cancelled' }[status] || status;
}
function _bpV3SOBadge(soStatus) {
    if (!soStatus || soStatus === 'pending') return '<span class="badge b-pend">SO Pending</span>';
    if (soStatus === 'approved') return '<span class="badge b-appr">SO Verified</span>';
    return '<span class="badge b-rej">SO Rejected</span>';
}
function _bpV3ScoreBadge(score) {
    if (score == null) return '';
    const s = Math.round(score);
    const cls = s >= 75 ? 'b-proven' : s >= 50 ? 'b-developing' : 'b-caution';
    return '<span class="badge ' + cls + '">' + s + '</span>';
}
function _bpV3LineBadge(lineStatus) {
    const map = { awaiting_po:'Awaiting PO', pending_verify:'Verify PO', verified:'Verified', issue:'Issue' };
    const cls = { awaiting_po:'b-pend', pending_verify:'b-draft', verified:'b-appr', issue:'b-rej' };
    return '<span class="badge ' + (cls[lineStatus]||'b-draft') + '">' + (map[lineStatus]||lineStatus) + '</span>';
}
function _fmtMoney(v) {
    if (v == null) return '\u2014';
    return '$' + Number(v).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
}

function renderBuyPlanV3Status(targetId) {
    if (targetId) _bpV3RenderTarget = targetId;
    const el = document.getElementById(_bpV3RenderTarget);
    if (!el) return;
    if (!_currentBuyPlanV3) { el.innerHTML = ''; return; }
    const bp = _currentBuyPlanV3;
    const statusColor = _bpV3StatusColor(bp.status);
    const isDraft = bp.status === 'draft';

    // AI Summary
    let summaryHtml = '';
    if (bp.ai_summary) {
        summaryHtml = '<div style="background:#f0f9ff;padding:10px 12px;border-left:3px solid #2563eb;border-radius:4px;margin-bottom:10px;font-size:12px;line-height:1.5">'
            + '<strong style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#2563eb">AI Analysis</strong><br>'
            + esc(bp.ai_summary) + '</div>';
    }

    // AI Flags
    let flagsHtml = '';
    if (bp.ai_flags && bp.ai_flags.length) {
        flagsHtml = '<div style="margin-bottom:10px">';
        for (const f of bp.ai_flags) {
            const fcolor = f.severity === 'critical' ? 'var(--red)' : f.severity === 'warning' ? 'var(--amber)' : '#2563eb';
            const ficon = f.severity === 'critical' ? '\u26d4' : f.severity === 'warning' ? '\u26a0\ufe0f' : '\u2139\ufe0f';
            flagsHtml += '<div style="padding:6px 10px;border-left:3px solid ' + fcolor + ';background:var(--bg-alt,#f9fafb);border-radius:4px;margin-bottom:4px;font-size:12px">'
                + ficon + ' ' + esc(f.message) + '</div>';
        }
        flagsHtml += '</div>';
    }

    // Context
    let contextHtml = '';
    if (bp.customer_name || bp.quote_number || bp.sales_order_number) {
        contextHtml = '<div class="info-card">'
            + (bp.customer_name ? '<div><strong>Customer:</strong> ' + esc(bp.customer_name) + '</div>' : '')
            + (bp.quote_number ? '<div><strong>Quote:</strong> ' + esc(bp.quote_number) + '</div>' : '')
            + (bp.sales_order_number ? '<div><strong>SO#:</strong> ' + esc(bp.sales_order_number) + '</div>' : '')
            + (bp.customer_po_number ? '<div><strong>Customer PO:</strong> ' + esc(bp.customer_po_number) + '</div>' : '')
            + '</div>';
    }

    // Financial summary
    let marginHtml = '';
    if (bp.total_cost != null) {
        const profit = (bp.total_revenue || 0) - (bp.total_cost || 0);
        const profitColor = profit >= 0 ? 'var(--green)' : 'var(--red)';
        marginHtml = '<div class="stat-row">'
            + '<div><strong>Cost:</strong> ' + _fmtMoney(bp.total_cost) + '</div>'
            + (bp.total_revenue ? '<div><strong>Revenue:</strong> ' + _fmtMoney(bp.total_revenue) + '</div>' : '')
            + (bp.total_revenue ? '<div style="color:' + profitColor + '"><strong>Profit:</strong> ' + _fmtMoney(profit) + '</div>' : '')
            + (bp.total_margin_pct != null ? '<div><strong>Margin:</strong> ' + Number(bp.total_margin_pct).toFixed(1) + '%</div>' : '')
            + '</div>';
    }

    // Notes
    let notesHtml = '';
    if (bp.salesperson_notes) notesHtml += '<div style="background:#f0f9ff;padding:8px 10px;border-left:3px solid #2563eb;border-radius:4px;margin-bottom:8px;font-size:12px"><strong>Sales:</strong> ' + esc(bp.salesperson_notes) + '</div>';
    if (bp.approval_notes) notesHtml += '<div style="background:#f0fdf4;padding:8px 10px;border-left:3px solid #16a34a;border-radius:4px;margin-bottom:8px;font-size:12px"><strong>Manager:</strong> ' + esc(bp.approval_notes) + '</div>';
    if (bp.so_rejection_note) notesHtml += '<div style="background:#fef2f2;padding:8px 10px;border-left:3px solid var(--red);border-radius:4px;margin-bottom:8px;font-size:12px"><strong>SO Rejected:</strong> ' + esc(bp.so_rejection_note) + '</div>';

    // Role checks
    const canApprove = window.__isAdmin || window.userRole === 'manager';
    const isBuyer = ['buyer','trader','manager','admin'].includes(window.userRole);
    const isPending = bp.status === 'pending';
    const isActive = bp.status === 'active';
    const isHalted = bp.status === 'halted';
    const isEditable = isDraft || (isPending && canApprove);

    // Lines table — varies by status and role
    const lines = bp.lines || [];
    let extraHeaders = '';
    if (isPending && canApprove) extraHeaders = '<th>Mgr Note</th>';
    if (isActive && isBuyer) extraHeaders = '<th>PO / Action</th>';
    if (isActive && !isBuyer) extraHeaders = '';

    let linesHtml = lines.map(l => {
        const showCompare = isDraft || (isPending && canApprove);
        const compareBtn = showCompare ? ' <a href="javascript:void(0)" onclick="openOfferComparisonV3(' + bp.id + ',' + l.requirement_id + ',' + l.id + ')" style="font-size:10px;color:#2563eb;text-decoration:underline">compare</a>' : '';
        const qtyCell = isEditable
            ? '<input type="number" class="bpv3-qty" data-line-id="' + l.id + '" value="' + (l.quantity||0) + '" min="1" style="width:70px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px;text-align:right">'
            : (l.quantity||0).toLocaleString();

        // Extra cells based on context
        let extraCell = '';
        if (isPending && canApprove) {
            extraCell = '<td><input type="text" class="bpv3-mgr-note" data-line-id="' + l.id + '" placeholder="Note\u2026" value="' + (l.manager_note ? l.manager_note.replace(/"/g,'&quot;') : '') + '" style="width:100px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:10px"></td>';
        }
        if (isActive && isBuyer) {
            if (l.status === 'awaiting_po') {
                extraCell = '<td>'
                    + '<div style="display:flex;gap:4px;align-items:center">'
                    + '<input type="text" class="bpv3-po" data-line-id="' + l.id + '" placeholder="PO#" style="width:80px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:10px">'
                    + '<input type="date" class="bpv3-ship" data-line-id="' + l.id + '" style="width:110px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:10px">'
                    + '<button class="btn btn-primary btn-sm" style="font-size:10px;padding:3px 8px" onclick="confirmPOV3(' + bp.id + ',' + l.id + ')">Confirm</button>'
                    + '</div>'
                    + '<div style="margin-top:4px"><a href="javascript:void(0)" onclick="openFlagIssueV3(' + bp.id + ',' + l.id + ')" style="font-size:10px;color:var(--red);text-decoration:underline">Flag issue</a></div>'
                    + '</td>';
            } else if (l.status === 'pending_verify') {
                extraCell = '<td><span style="font-size:11px">' + esc(l.po_number) + '</span> <span style="color:var(--amber);font-size:10px">awaiting ops verify</span></td>';
            } else if (l.status === 'verified') {
                extraCell = '<td><span style="font-size:11px;color:var(--green)">' + esc(l.po_number) + ' \u2713</span></td>';
            } else if (l.status === 'issue') {
                const issueLabels = {sold_out:'Sold Out',price_changed:'Price Changed',lead_time_changed:'Lead Time Changed',other:'Other'};
                extraCell = '<td><span style="font-size:11px;color:var(--red)">' + (issueLabels[l.issue_type]||l.issue_type) + '</span>'
                    + (l.issue_note ? '<div style="font-size:10px;color:var(--muted)">' + esc(l.issue_note) + '</div>' : '') + '</td>';
            }
        }
        // Ops PO verification inline
        if (isActive && l.status === 'pending_verify' && canApprove) {
            extraCell = '<td><span style="font-size:11px">' + esc(l.po_number) + '</span>'
                + '<div style="display:flex;gap:4px;margin-top:4px">'
                + '<button class="btn btn-success btn-sm" style="font-size:10px;padding:2px 6px" onclick="verifyPOV3(' + bp.id + ',' + l.id + ',\'approve\')">Verify</button>'
                + '<button class="btn btn-danger btn-sm" style="font-size:10px;padding:2px 6px" onclick="openRejectPOV3(' + bp.id + ',' + l.id + ')">Reject</button>'
                + '</div></td>';
        }

        return '<tr data-line-id="' + l.id + '">'
            + '<td>' + esc(l.mpn) + compareBtn + '</td>'
            + '<td>' + esc(l.vendor_name || '\u2014') + '</td>'
            + '<td>' + _bpV3ScoreBadge(l.ai_score) + '</td>'
            + '<td style="font-size:11px">' + esc(l.buyer_name || '\u2014') + '</td>'
            + '<td class="mono">' + qtyCell + '</td>'
            + '<td class="mono">$' + Number(l.unit_cost||0).toFixed(4) + '</td>'
            + '<td class="mono">' + (l.margin_pct != null ? Number(l.margin_pct).toFixed(1) + '%' : '\u2014') + '</td>'
            + '<td>' + esc(l.lead_time || '\u2014') + '</td>'
            + (isDraft ? '' : '<td>' + _bpV3LineBadge(l.status) + '</td>')
            + extraCell
            + '</tr>';
    }).join('');

    // ── Actions block ──
    let actionsHtml = '';

    // Draft: salesperson submit form
    if (isDraft) {
        actionsHtml = '<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:14px">'
            + '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px">'
            + '<div class="field" style="flex:1;min-width:180px"><label style="font-weight:600;font-size:12px">Acctivate SO# <span style="color:var(--red)">*</span></label>'
            + '<input type="text" id="bpV3SO" placeholder="Enter Acctivate SO#" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)"></div>'
            + '<div class="field" style="flex:1;min-width:180px"><label style="font-weight:600;font-size:12px">Customer PO# <span style="font-size:11px;color:var(--muted)">(optional)</span></label>'
            + '<input type="text" id="bpV3CustPO" placeholder="Customer PO number" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)"></div>'
            + '</div>'
            + '<div class="field" style="margin-bottom:10px"><label style="font-weight:600;font-size:12px">Notes for Buyers</label>'
            + '<textarea id="bpV3Notes" rows="2" placeholder="Special instructions, context\u2026" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)"></textarea></div>'
            + '<div style="display:flex;gap:8px"><button class="btn btn-primary" onclick="submitBuyPlanV3()">Submit Buy Plan</button></div>'
            + '</div>';
    }

    // Pending: manager approve/reject
    if (isPending && canApprove) {
        actionsHtml = '<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:14px">'
            + '<div class="field" style="margin-bottom:10px"><label style="font-weight:600;font-size:12px">Manager Notes</label>'
            + '<textarea id="bpV3MgrNotes" rows="2" placeholder="Approval notes, instructions for buyers\u2026" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)"></textarea></div>'
            + '<div style="display:flex;gap:8px;align-items:center">'
            + '<button class="btn btn-success" onclick="approveBuyPlanV3()">Approve</button>'
            + '<button class="btn btn-danger" onclick="openRejectBuyPlanV3()">Reject</button>'
            + '</div>'
            + '<div id="bpV3RejectForm" style="display:none;margin-top:10px;padding:10px;border:1px solid var(--red);border-radius:8px;background:#fef2f2">'
            + '<div style="font-weight:600;font-size:12px;margin-bottom:6px;color:var(--red)">Reject Buy Plan</div>'
            + '<textarea id="bpV3RejectReason" placeholder="Reason for rejection\u2026" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;min-height:40px;margin-bottom:8px"></textarea>'
            + '<div style="display:flex;gap:8px"><button class="btn btn-danger btn-sm" onclick="rejectBuyPlanV3()">Confirm Reject</button>'
            + '<button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'bpV3RejectForm\').style.display=\'none\'">Cancel</button></div>'
            + '</div>'
            + '</div>';
    }

    // Active: ops SO verification (when so_status is pending)
    if (isActive && bp.so_status === 'pending' && canApprove) {
        actionsHtml += '<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:14px">'
            + '<div style="font-weight:600;font-size:12px;margin-bottom:8px">SO Verification</div>'
            + '<p style="font-size:12px;color:var(--muted);margin-bottom:8px">Verify that SO# <strong>' + esc(bp.sales_order_number) + '</strong> was properly set up in Acctivate.</p>'
            + '<div style="display:flex;gap:8px;align-items:center">'
            + '<button class="btn btn-success btn-sm" onclick="verifySOV3(\'approve\')">Verify SO</button>'
            + '<button class="btn btn-danger btn-sm" onclick="openRejectSOV3()">Reject SO</button>'
            + '<button class="btn btn-ghost btn-sm" onclick="openHaltSOV3()">Halt</button>'
            + '</div>'
            + '<div id="bpV3SORejectForm" style="display:none;margin-top:8px;padding:8px;border:1px solid var(--red);border-radius:6px;background:#fef2f2">'
            + '<textarea id="bpV3SORejectNote" placeholder="Reason\u2026" style="width:100%;padding:6px;border:1px solid var(--border);border-radius:4px;font-size:12px;min-height:30px;margin-bottom:6px"></textarea>'
            + '<div style="display:flex;gap:6px"><button class="btn btn-danger btn-sm" onclick="verifySOV3(\'reject\')">Confirm Reject</button>'
            + '<button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'bpV3SORejectForm\').style.display=\'none\'">Cancel</button></div></div>'
            + '<div id="bpV3SOHaltForm" style="display:none;margin-top:8px;padding:8px;border:1px solid var(--amber);border-radius:6px;background:#fffbeb">'
            + '<textarea id="bpV3SOHaltNote" placeholder="Reason to halt\u2026" style="width:100%;padding:6px;border:1px solid var(--border);border-radius:4px;font-size:12px;min-height:30px;margin-bottom:6px"></textarea>'
            + '<div style="display:flex;gap:6px"><button class="btn btn-ghost btn-sm" style="border-color:var(--amber);color:var(--amber)" onclick="verifySOV3(\'halt\')">Confirm Halt</button>'
            + '<button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'bpV3SOHaltForm\').style.display=\'none\'">Cancel</button></div></div>'
            + '</div>';
    }

    // Halted: resubmit for salesperson
    if (isHalted && (bp.submitted_by_id === window.__userId || canApprove)) {
        actionsHtml += '<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:14px">'
            + '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px">'
            + '<div class="field" style="flex:1;min-width:180px"><label style="font-weight:600;font-size:12px">Corrected SO#</label>'
            + '<input type="text" id="bpV3ResubSO" value="' + (bp.sales_order_number||'').replace(/"/g,'&quot;') + '" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)"></div>'
            + '<div class="field" style="flex:1;min-width:180px"><label style="font-weight:600;font-size:12px">Customer PO#</label>'
            + '<input type="text" id="bpV3ResubCustPO" value="' + (bp.customer_po_number||'').replace(/"/g,'&quot;') + '" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)"></div>'
            + '</div>'
            + '<div class="field" style="margin-bottom:10px"><label style="font-weight:600;font-size:12px">Notes</label>'
            + '<textarea id="bpV3ResubNotes" rows="2" placeholder="Corrections made\u2026" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)"></textarea></div>'
            + '<button class="btn btn-primary" onclick="resubmitBuyPlanV3()">Resubmit</button>'
            + '</div>';
    }

    el.innerHTML = '<div class="card" style="margin-top:16px;border-left:4px solid ' + statusColor + '">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">'
        + '<div><strong>Buy Plan V3</strong>'
        + ' <span class="status-badge" style="background:' + statusColor + ';color:#fff;margin-left:8px">' + _bpV3StatusLabel(bp.status) + '</span>'
        + (bp.status !== 'draft' && bp.so_status ? ' ' + _bpV3SOBadge(bp.so_status) : '')
        + (bp.is_stock_sale ? ' <span class="status-badge" style="background:#7c3aed;color:#fff;margin-left:4px">Stock Sale</span>' : '')
        + (bp.auto_approved ? ' <span class="badge b-appr" style="margin-left:4px">Auto-Approved</span>' : '')
        + '</div>'
        + '<span style="font-size:11px;color:var(--muted)">' + (bp.submitted_by_name ? 'by ' + esc(bp.submitted_by_name) : '') + (bp.created_at ? ' \xB7 ' + fmtDateTime(bp.created_at) : '') + '</span>'
        + '</div>'
        + summaryHtml + flagsHtml + contextHtml + marginHtml + notesHtml
        + '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch"><table class="tbl" style="margin-bottom:0"><thead><tr>'
        + '<th>MPN</th><th>Vendor</th><th>Score</th><th>Buyer</th><th>Qty</th><th>Unit Cost</th><th>Margin</th><th>Lead</th>'
        + (isDraft ? '' : '<th>Status</th>') + extraHeaders
        + '</tr></thead><tbody>' + linesHtml + '</tbody></table></div>'
        + actionsHtml + '</div>';
}

async function submitBuyPlanV3() {
    if (!_currentBuyPlanV3 || _currentBuyPlanV3.status !== 'draft') return;
    const soNum = (document.getElementById('bpV3SO')?.value || '').trim();
    if (!soNum) { showToast('Acctivate SO# is required', 'error'); document.getElementById('bpV3SO')?.focus(); return; }

    // Collect line edits (quantity changes + vendor swaps)
    const lineEdits = [];
    for (const line of (_currentBuyPlanV3.lines || [])) {
        const qtyInput = document.querySelector('.bpv3-qty[data-line-id="' + line.id + '"]');
        const newQty = qtyInput ? (parseInt(qtyInput.value) || line.quantity) : line.quantity;
        if (line._swapped || newQty !== line.quantity) {
            lineEdits.push({ requirement_id: line.requirement_id, offer_id: line.offer_id, quantity: newQty });
        }
    }

    const custPO = (document.getElementById('bpV3CustPO')?.value || '').trim() || null;
    const notes = (document.getElementById('bpV3Notes')?.value || '').trim() || null;

    const btn = document.querySelector('#buyPlanSection .btn-primary');
    await guardBtn(btn, 'Submitting\u2026', async () => {
        try {
            const body = { sales_order_number: soNum, customer_po_number: custPO, salesperson_notes: notes };
            if (lineEdits.length) body.line_edits = lineEdits;
            const res = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id + '/submit', { method: 'POST', body });
            const msg = res.auto_approved ? 'Buy plan auto-approved \u2014 buyers notified!' : 'Buy plan submitted for approval';
            showToast(msg, 'success');
            _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id);
            renderBuyPlanV3Status();
        } catch (e) {
            logCatchError('submitBuyPlanV3', e);
            showToast('Failed to submit: ' + (e.message || e), 'error');
        }
    });
}

async function approveBuyPlanV3() {
    if (!_currentBuyPlanV3 || _currentBuyPlanV3.status !== 'pending') return;
    if (approveBuyPlanV3._busy) return; approveBuyPlanV3._busy = true;

    // Collect line overrides (qty changes, vendor swaps, manager notes)
    const overrides = [];
    for (const line of (_currentBuyPlanV3.lines || [])) {
        const qtyInput = document.querySelector('.bpv3-qty[data-line-id="' + line.id + '"]');
        const noteInput = document.querySelector('.bpv3-mgr-note[data-line-id="' + line.id + '"]');
        const newQty = qtyInput ? (parseInt(qtyInput.value) || line.quantity) : line.quantity;
        const note = noteInput ? noteInput.value.trim() : '';
        if (line._swapped || newQty !== line.quantity || note) {
            const o = { line_id: line.id };
            if (line._swapped) o.offer_id = line.offer_id;
            if (newQty !== line.quantity) o.quantity = newQty;
            if (note) o.manager_note = note;
            overrides.push(o);
        }
    }

    const notes = (document.getElementById('bpV3MgrNotes')?.value || '').trim() || null;
    const btn = document.querySelector('#' + _bpV3RenderTarget + ' .btn-success');
    try {
        await guardBtn(btn, 'Approving\u2026', async () => {
            const body = { action: 'approve', notes };
            if (overrides.length) body.line_overrides = overrides;
            await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id + '/approve', { method: 'POST', body });
            showToast('Buy plan approved \u2014 buyers notified', 'success');
            _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id);
            renderBuyPlanV3Status();
        });
    } catch (e) {
        logCatchError('approveBuyPlanV3', e);
        showToast('Failed to approve: ' + (e.message || e), 'error');
    } finally { approveBuyPlanV3._busy = false; }
}

function openRejectBuyPlanV3() {
    const form = document.getElementById('bpV3RejectForm');
    if (form) { form.style.display = ''; form.querySelector('textarea')?.focus(); }
}

async function rejectBuyPlanV3() {
    if (!_currentBuyPlanV3 || _currentBuyPlanV3.status !== 'pending') return;
    const reason = (document.getElementById('bpV3RejectReason')?.value || '').trim();
    if (!reason) { showToast('Rejection reason is required', 'error'); return; }
    if (rejectBuyPlanV3._busy) return; rejectBuyPlanV3._busy = true;
    try {
        await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id + '/approve', {
            method: 'POST', body: { action: 'reject', notes: reason }
        });
        showToast('Buy plan rejected', 'info');
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id);
        renderBuyPlanV3Status();
    } catch (e) {
        logCatchError('rejectBuyPlanV3', e);
        showToast('Failed to reject: ' + (e.message || e), 'error');
    } finally { rejectBuyPlanV3._busy = false; }
}

// ── Buyer PO Execution ───────────────────────────────────────────────

async function confirmPOV3(planId, lineId) {
    const poInput = document.querySelector('.bpv3-po[data-line-id="' + lineId + '"]');
    const shipInput = document.querySelector('.bpv3-ship[data-line-id="' + lineId + '"]');
    const po = (poInput?.value || '').trim();
    const ship = (shipInput?.value || '').trim();
    if (!po) { showToast('PO number is required', 'error'); poInput?.focus(); return; }
    if (!ship) { showToast('Estimated ship date is required', 'error'); shipInput?.focus(); return; }
    try {
        await apiFetch('/api/buy-plans-v3/' + planId + '/lines/' + lineId + '/confirm-po', {
            method: 'POST', body: { po_number: po, estimated_ship_date: ship + 'T00:00:00Z' }
        });
        showToast('PO confirmed', 'success');
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + planId);
        renderBuyPlanV3Status();
    } catch (e) { showToast('Failed: ' + (e.message || e), 'error'); }
}

function openFlagIssueV3(planId, lineId) {
    const existing = document.getElementById('bpV3IssueForm-' + lineId);
    if (existing) { existing.style.display = ''; return; }
    const row = document.querySelector('tr[data-line-id="' + lineId + '"]');
    if (!row) return;
    const cell = row.querySelector('td:last-child');
    const form = document.createElement('div');
    form.id = 'bpV3IssueForm-' + lineId;
    form.style.cssText = 'margin-top:6px;padding:6px;border:1px solid var(--red);border-radius:6px;background:#fef2f2';
    form.innerHTML = '<select class="bpv3-issue-type" style="padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px;margin-bottom:4px;width:100%">'
        + '<option value="sold_out">Sold Out</option><option value="price_changed">Price Changed</option>'
        + '<option value="lead_time_changed">Lead Time Changed</option><option value="other">Other</option></select>'
        + '<input class="bpv3-issue-note" placeholder="Note\u2026" style="width:100%;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px;margin-bottom:4px">'
        + '<div style="display:flex;gap:4px"><button class="btn btn-danger btn-sm" style="font-size:10px;padding:2px 6px" onclick="submitFlagIssueV3(' + planId + ',' + lineId + ')">Flag</button>'
        + '<button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 6px" onclick="this.closest(\'div[id^=bpV3IssueForm]\').style.display=\'none\'">Cancel</button></div>';
    cell.appendChild(form);
}

async function submitFlagIssueV3(planId, lineId) {
    const form = document.getElementById('bpV3IssueForm-' + lineId);
    if (!form) return;
    const issueType = form.querySelector('.bpv3-issue-type')?.value || 'other';
    const note = (form.querySelector('.bpv3-issue-note')?.value || '').trim() || null;
    if (issueType === 'other' && !note) { showToast('Note required for "Other" issue', 'error'); return; }
    try {
        await apiFetch('/api/buy-plans-v3/' + planId + '/lines/' + lineId + '/issue', {
            method: 'POST', body: { issue_type: issueType, note }
        });
        showToast('Issue flagged', 'info');
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + planId);
        renderBuyPlanV3Status();
    } catch (e) { showToast('Failed: ' + (e.message || e), 'error'); }
}

// ── Ops Verification ────────────────────────────────────────────────

function openRejectSOV3() {
    document.getElementById('bpV3SOHaltForm') && (document.getElementById('bpV3SOHaltForm').style.display = 'none');
    const f = document.getElementById('bpV3SORejectForm');
    if (f) { f.style.display = ''; f.querySelector('textarea')?.focus(); }
}

function openHaltSOV3() {
    document.getElementById('bpV3SORejectForm') && (document.getElementById('bpV3SORejectForm').style.display = 'none');
    const f = document.getElementById('bpV3SOHaltForm');
    if (f) { f.style.display = ''; f.querySelector('textarea')?.focus(); }
}

async function verifySOV3(action) {
    if (!_currentBuyPlanV3) return;
    const noteEl = action === 'reject' ? document.getElementById('bpV3SORejectNote')
        : action === 'halt' ? document.getElementById('bpV3SOHaltNote') : null;
    const note = noteEl ? (noteEl.value || '').trim() : null;
    if ((action === 'reject' || action === 'halt') && !note) {
        showToast('A note is required', 'error'); noteEl?.focus(); return;
    }
    try {
        await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id + '/verify-so', {
            method: 'POST', body: { action, rejection_note: note }
        });
        showToast(action === 'approve' ? 'SO verified' : 'SO ' + action + 'ed', action === 'approve' ? 'success' : 'info');
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id);
        renderBuyPlanV3Status();
    } catch (e) { showToast('Failed: ' + (e.message || e), 'error'); }
}

async function verifyPOV3(planId, lineId, action) {
    try {
        await apiFetch('/api/buy-plans-v3/' + planId + '/lines/' + lineId + '/verify-po', {
            method: 'POST', body: { action }
        });
        showToast('PO ' + (action === 'approve' ? 'verified' : 'rejected'), action === 'approve' ? 'success' : 'info');
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + planId);
        renderBuyPlanV3Status();
    } catch (e) { showToast('Failed: ' + (e.message || e), 'error'); }
}

function openRejectPOV3(planId, lineId) {
    const note = prompt('Reason for rejecting this PO:');
    if (note === null) return;
    if (!note.trim()) { showToast('Reason is required', 'error'); return; }
    apiFetch('/api/buy-plans-v3/' + planId + '/lines/' + lineId + '/verify-po', {
        method: 'POST', body: { action: 'reject', rejection_note: note.trim() }
    }).then(() => {
        showToast('PO rejected', 'info');
        return apiFetch('/api/buy-plans-v3/' + planId);
    }).then(plan => {
        _currentBuyPlanV3 = plan;
        renderBuyPlanV3Status();
    }).catch(e => showToast('Failed: ' + (e.message || e), 'error'));
}

// ── Resubmit ────────────────────────────────────────────────────────

async function resubmitBuyPlanV3() {
    if (!_currentBuyPlanV3) return;
    const soNum = (document.getElementById('bpV3ResubSO')?.value || '').trim();
    if (!soNum) { showToast('SO# is required', 'error'); return; }
    const custPO = (document.getElementById('bpV3ResubCustPO')?.value || '').trim() || null;
    const notes = (document.getElementById('bpV3ResubNotes')?.value || '').trim() || null;
    try {
        const res = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id + '/resubmit', {
            method: 'POST', body: { sales_order_number: soNum, customer_po_number: custPO, salesperson_notes: notes }
        });
        showToast(res.auto_approved ? 'Resubmitted and auto-approved!' : 'Resubmitted for approval', 'success');
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + res.plan_id);
        renderBuyPlanV3Status();
    } catch (e) { showToast('Failed: ' + (e.message || e), 'error'); }
}

async function openOfferComparisonV3(planId, reqId, currentLineId) {
    const modal = document.getElementById('offerComparisonV3Modal');
    if (!modal) return;
    openModal('offerComparisonV3Modal');
    const body = modal.querySelector('.modal');
    body.innerHTML = '<h2>Offer Comparison</h2><div class="spinner-row"><div class="spinner"></div>Loading offers\u2026</div>';
    try {
        const data = await apiFetch('/api/buy-plans-v3/' + planId + '/offers/' + reqId);
        const offers = data.offers || [];
        const selectedIds = new Set(data.selected_offer_ids || []);
        let html = '<h2>Offers for ' + esc(data.mpn || 'MPN') + '</h2>';
        html += '<p style="font-size:12px;color:var(--muted);margin-bottom:12px">Target qty: ' + (data.target_qty || 0).toLocaleString() + ' \u2014 click a row to swap vendor</p>';
        if (!offers.length) {
            html += '<p class="empty">No alternative offers available</p>';
        } else {
            html += '<table class="tbl"><thead><tr><th></th><th>Vendor</th><th>Unit Price</th><th>Qty Avail</th><th>Lead</th><th>Condition</th></tr></thead><tbody>';
            for (const o of offers) {
                const sel = selectedIds.has(o.offer_id);
                const stale = o.is_stale ? ' style="opacity:.6"' : '';
                html += '<tr' + stale + ' onclick="swapLineOfferV3(' + currentLineId + ',' + o.offer_id + ',' + reqId + ')" style="cursor:pointer' + (sel ? ';background:var(--teal-light)' : '') + '">'
                    + '<td>' + (sel ? '<strong>\u2713</strong>' : '') + '</td>'
                    + '<td>' + esc(o.vendor_name) + (o.is_stale ? ' <span style="font-size:10px;color:var(--red)">(stale)</span>' : '') + '</td>'
                    + '<td class="mono">' + (o.unit_price != null ? '$' + Number(o.unit_price).toFixed(4) : '\u2014') + '</td>'
                    + '<td class="mono">' + (o.qty_available != null ? o.qty_available.toLocaleString() : '\u2014') + '</td>'
                    + '<td>' + esc(o.lead_time || '\u2014') + '</td>'
                    + '<td>' + esc(o.condition || '\u2014') + '</td>'
                    + '</tr>';
            }
            html += '</tbody></table>';
        }
        html += '<div class="mactions"><button class="btn btn-ghost" onclick="closeModal(\'offerComparisonV3Modal\')">Close</button></div>';
        body.innerHTML = html;
    } catch (e) {
        logCatchError('openOfferComparisonV3', e);
        body.innerHTML = '<h2>Offer Comparison</h2><p style="color:var(--red)">Failed to load offers</p><div class="mactions"><button class="btn btn-ghost" onclick="closeModal(\'offerComparisonV3Modal\')">Close</button></div>';
    }
}

async function swapLineOfferV3(lineId, offerId, reqId) {
    if (!_currentBuyPlanV3 || _currentBuyPlanV3.status !== 'draft') return;
    const line = (_currentBuyPlanV3.lines || []).find(l => l.id === lineId);
    if (!line) return;
    if (line.offer_id === offerId) { closeModal('offerComparisonV3Modal'); return; }
    // Update the line edit locally — actual swap happens on submit via line_edits
    line.offer_id = offerId;
    line._swapped = true;
    // Fetch offer details to update display
    try {
        const data = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id + '/offers/' + reqId);
        const offer = (data.offers || []).find(o => o.offer_id === offerId);
        if (offer) {
            line.vendor_name = offer.vendor_name;
            line.unit_cost = offer.unit_price;
            line.lead_time = offer.lead_time;
            line.condition = offer.condition;
        }
    } catch (e) { logCatchError('swapLineOfferV3', e); }
    closeModal('offerComparisonV3Modal');
    renderBuyPlanV3Status();
    showToast('Vendor swapped \u2014 will apply on submit', 'info');
}

// ── Buy Plans Admin List ──────────────────────────────────────────────

let _bpFilter = '';
let _buyPlans = [];
let _bpMyOnly = false;

async function showBuyPlans() {
    showView('view-buyplans');
    setCurrentReqId(null);
    await loadBuyPlans();
}

async function loadBuyPlans() {
    var bpl = document.getElementById('buyPlansList');
    if (bpl && !_buyPlans.length) bpl.innerHTML = '<div class="spinner-row"><div class="spinner"></div>Loading buy plans\u2026</div>';
    try {
        let url = '/api/buy-plans-v3';
        if (_bpFilter) url += '?status=' + encodeURIComponent(_bpFilter);
        const resp = await apiFetch(url);
        _buyPlans = resp.items || [];
        renderBuyPlansList();
    } catch (e) {
        showToast('Failed to load buy plans', 'error');
    }
}

function setBpFilter(status, btn) {
    _bpFilter = status;
    document.querySelectorAll('#bpStatusPills .fp').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    loadBuyPlans();
}

function toggleBpMyOnly(checked) {
    _bpMyOnly = checked;
    renderBuyPlansList();
}

let _bpSortCol = null;
let _bpSortDir = 'asc';

function _bpSortArrow(col) {
    if (_bpSortCol !== col) return '\u21c5';
    return _bpSortDir === 'asc' ? '\u25b2' : '\u25bc';
}

function sortBpList(col) {
    if (_bpSortCol === col) {
        if (_bpSortDir === 'asc') _bpSortDir = 'desc';
        else { _bpSortCol = null; _bpSortDir = 'asc'; }
    } else {
        _bpSortCol = col;
        _bpSortDir = 'asc';
    }
    renderBuyPlansList();
}

function renderBuyPlansList() {
    const el = document.getElementById('buyPlansList');
    let data = [..._buyPlans];

    // "My Assignments" filter
    if (_bpMyOnly && window.__userName) {
        const myName = window.__userName.toLowerCase();
        data = data.filter(bp => (bp.submitted_by_name || '').toLowerCase() === myName);
    }
    // Search filter
    const q = (document.getElementById('bpSearch') || {}).value || '';
    if (q) {
        const lq = q.toLowerCase();
        data = data.filter(bp => (bp.customer_name || '').toLowerCase().includes(lq)
            || (bp.quote_number || '').toLowerCase().includes(lq)
            || (bp.sales_order_number || '').toLowerCase().includes(lq)
            || (bp.submitted_by_name || '').toLowerCase().includes(lq));
    }

    if (!data.length) {
        el.innerHTML = stateEmpty('No buy plans found', 'Build a buy plan by marking a quote as won');
        return;
    }

    const statusLabels = {draft:'Draft',pending:'Pending',active:'Active',halted:'Halted',completed:'Completed',cancelled:'Cancelled'};
    const statusBadge = {draft:'b-draft',pending:'b-pend',active:'b-appr',halted:'b-rej',completed:'b-comp',cancelled:'b-canc'};

    // Sort
    if (_bpSortCol) {
        data.sort((a, b) => {
            let va, vb;
            switch (_bpSortCol) {
                case 'req': va = (a.quote_number || ''); vb = (b.quote_number || ''); break;
                case 'status': va = (a.status || ''); vb = (b.status || ''); break;
                case 'customer': va = (a.customer_name || ''); vb = (b.customer_name || ''); break;
                case 'items': va = a.line_count || 0; vb = b.line_count || 0; break;
                case 'total': va = a.total_cost || 0; vb = b.total_cost || 0; break;
                case 'so': va = (a.sales_order_number || ''); vb = (b.sales_order_number || ''); break;
                case 'by': va = (a.submitted_by_name || ''); vb = (b.submitted_by_name || ''); break;
                case 'date': va = (a.created_at || ''); vb = (b.created_at || ''); break;
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _bpSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _bpSortDir === 'asc' ? va - vb : vb - va;
        });
    }

    const thC = (col) => _bpSortCol === col ? ' class="sorted"' : '';
    const sa = (col) => `<span class="sort-arrow">${_bpSortArrow(col)}</span>`;

    let html = `<div style="padding:0 16px"><table class="tbl"><thead><tr>
        <th onclick="sortBpList('req')"${thC('req')}>Quote ${sa('req')}</th>
        <th onclick="sortBpList('status')"${thC('status')}>Status ${sa('status')}</th>
        <th onclick="sortBpList('customer')"${thC('customer')}>Customer ${sa('customer')}</th>
        <th onclick="sortBpList('items')"${thC('items')}>Lines ${sa('items')}</th>
        <th onclick="sortBpList('total')"${thC('total')}>Total Cost ${sa('total')}</th>
        <th onclick="sortBpList('so')"${thC('so')}>SO# ${sa('so')}</th>
        <th onclick="sortBpList('by')"${thC('by')}>Submitted By ${sa('by')}</th>
        <th onclick="sortBpList('date')"${thC('date')}>Date ${sa('date')}</th>
    </tr></thead><tbody>`;

    for (const bp of data) {
        const label = statusLabels[bp.status] || bp.status;
        const bc = statusBadge[bp.status] || 'b-draft';
        const flagIcon = bp.ai_flag_count ? ' <span style="color:var(--amber)" title="' + bp.ai_flag_count + ' flag(s)">\u26a0\ufe0f</span>' : '';
        html += `<tr onclick="openBuyPlanDetailV3(${bp.id})" style="cursor:pointer">
            <td><b class="cust-link">${esc(bp.quote_number || 'Q-' + bp.quote_id)}</b></td>
            <td><span class="badge ${bc}">${label}</span>${bp.is_stock_sale ? ' <span class="badge b-stock">Stock</span>' : ''}${bp.auto_approved ? ' <span class="badge b-appr" style="font-size:9px">Auto</span>' : ''}${flagIcon}</td>
            <td>${esc(bp.customer_name || '\u2014')}</td>
            <td class="mono">${bp.line_count || 0}</td>
            <td class="mono">${_fmtMoney(bp.total_cost)}</td>
            <td class="mono">${esc(bp.sales_order_number || '\u2014')}</td>
            <td>${esc(bp.submitted_by_name || '\u2014')}</td>
            <td style="font-size:11px;color:var(--muted)">${bp.created_at ? fmtDateTime(bp.created_at) : '\u2014'}</td>
        </tr>`;
    }

    html += '</tbody></table></div>';
    el.innerHTML = html;
}

async function openBuyPlanDetailV3(planId) {
    const el = document.getElementById('buyPlansList');
    el.innerHTML = '<div class="spinner-row"><div class="spinner"></div>Loading\u2026</div>';
    try {
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + planId);
    } catch (e) { showToast('Failed to load buy plan', 'error'); el.innerHTML = ''; return; }
    const backBtn = '<button class="btn btn-ghost" onclick="loadBuyPlans()" style="margin-bottom:12px">\u2190 Back to list</button>';
    el.innerHTML = backBtn;
    const section = document.createElement('div');
    section.id = 'buyPlanDetailV3Section';
    el.appendChild(section);
    renderBuyPlanV3Status('buyPlanDetailV3Section');
}

// ── Token-Based Approval ────────────────────────────────────────────────

async function checkTokenApproval() {
    if (!location.hash.startsWith('#approve-token/')) return false;
    const token = location.hash.replace('#approve-token/', '');
    if (!token) return false;
    try {
        const bp = await apiFetch('/api/buy-plans/token/' + encodeURIComponent(token));
        showView('view-buyplans');
        const el = document.getElementById('buyPlansList');
        const statusLabel = bp.status === 'pending_approval' ? 'Pending Approval' : bp.status;
        el.innerHTML = `
            <div class="card" style="max-width:600px;margin:40px auto;border-left:4px solid var(--amber)">
                <h2 style="margin-bottom:16px">Buy Plan Approval</h2>
                <div class="info-card">
                    ${bp.customer_name ? '<div><strong>Customer:</strong> '+esc(bp.customer_name)+'</div>' : ''}
                    ${bp.quote_number ? '<div><strong>Quote:</strong> '+esc(bp.quote_number)+'</div>' : ''}
                    <div><strong>Status:</strong> ${esc(statusLabel)}</div>
                    <div><strong>Submitted by:</strong> ${esc(bp.submitted_by || '\u2014')}</div>
                    <div><strong>Items:</strong> ${(bp.line_items||[]).length} line items</div>
                </div>
                ${bp.salesperson_notes ? '<div style="background:#f0f9ff;padding:8px 10px;border-left:3px solid #2563eb;border-radius:4px;margin-bottom:12px;font-size:12px"><strong>Salesperson:</strong> '+esc(bp.salesperson_notes)+'</div>' : ''}
                <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
                <table class="tbl" style="margin-bottom:12px">
                    <thead><tr><th>MPN</th><th>Vendor</th><th>Plan Qty</th><th>Cost</th><th>Lead</th></tr></thead>
                    <tbody>${(bp.line_items||[]).map(li => '<tr><td>'+esc(li.mpn)+'</td><td>'+esc(li.vendor_name)+'</td><td>'+(li.plan_qty||li.qty||0)+'</td><td>$'+(Number(li.cost_price||0).toFixed(4))+'</td><td>'+esc(li.lead_time||'\u2014')+'</td></tr>').join('')}</tbody>
                </table>
                </div>
                ${bp.status === 'pending_approval' ? `
                <div style="margin-top:16px">
                    <div class="field" style="margin-bottom:8px">
                        <label style="font-weight:600;font-size:12px">Acctivate Sales Order # <span style="color:var(--red)">*</span></label>
                        <input type="text" id="tokenSoNumber" placeholder="Enter Acctivate SO#" style="width:200px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)">
                    </div>
                    <div style="margin-bottom:8px">
                        <textarea id="tokenManagerNotes" placeholder="Manager notes (optional)..." style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;min-height:40px"></textarea>
                    </div>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-success" onclick="tokenApprovePlan('${escAttr(token)}')">Approve</button>
                        <button class="btn btn-danger" onclick="tokenRejectPlan('${escAttr(token)}')">Reject</button>
                    </div>
                </div>` : '<p style="color:var(--muted);font-size:12px">This plan is no longer pending approval (status: '+statusLabel+').</p>'}
            </div>`;
        return true;
    } catch (e) {
        showToast('Invalid or expired approval link', 'error');
        return false;
    }
}

async function tokenApprovePlan(token) {
    const soNumber = document.getElementById('tokenSoNumber')?.value?.trim() || '';
    if (!soNumber) { showToast('Acctivate Sales Order # is required', 'error'); return; }
    const notes = document.getElementById('tokenManagerNotes')?.value?.trim() || '';
    try {
        await apiFetch('/api/buy-plans/token/' + encodeURIComponent(token) + '/approve', {
            method: 'PUT', body: { sales_order_number: soNumber, manager_notes: notes }
        });
        _showTokenResult('approved', 'Buy plan approved — buyers have been notified.');
    } catch (e) { showToast('Failed to approve: ' + (e.message || e), 'error'); }
}

async function tokenRejectPlan(token) {
    const reason = prompt('Rejection reason:');
    if (reason === null) return;
    try {
        await apiFetch('/api/buy-plans/token/' + encodeURIComponent(token) + '/reject', {
            method: 'PUT', body: { reason }
        });
        _showTokenResult('rejected', 'Buy plan has been rejected.');
    } catch (e) { showToast('Failed to reject: ' + (e.message || e), 'error'); }
}

function _showTokenResult(status, message) {
    const el = document.querySelector('.main') || document.getElementById('mainContent');
    if (!el) return;
    const color = status === 'approved' ? 'var(--green)' : 'var(--red)';
    const icon = status === 'approved' ? '&#10003;' : '&#10007;';
    el.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:300px;text-align:center;padding:40px">
            <div style="width:64px;height:64px;border-radius:50%;background:${color}15;display:flex;align-items:center;justify-content:center;font-size:28px;color:${color};margin-bottom:16px">${icon}</div>
            <h2 style="font-size:18px;margin:0 0 8px">${status === 'approved' ? 'Approved' : 'Rejected'}</h2>
            <p style="color:var(--text2);font-size:13px;margin:0 0 24px">${esc(message)}</p>
            <div style="display:flex;gap:12px">
                <button class="btn btn-primary" onclick="location.hash='';showView('buyplans');loadBuyPlans()">Go to Buy Plans</button>
                <button class="btn btn-ghost" onclick="location.hash='';showView('list');loadRequisitions()">Dashboard</button>
            </div>
        </div>`;
    location.hash = '';
}

async function submitLost() {
    if (!crmQuote) return;
    try {
        const lostData = await apiFetch('/api/quotes/' + crmQuote.id + '/result', {
            method: 'POST', body: {
                result: 'lost',
                reason: document.getElementById('lostReason')?.value || '',
                notes: document.getElementById('lostNotes')?.value || '',
            }
        });
        closeModal('lostModal');
        showToast('Quote marked as lost', 'info');
        notifyStatusChange(lostData);
        loadQuote();
        _refreshCustPipeline();
    } catch (e) { console.error('submitLost:', e); showToast('Error submitting', 'error'); }
}

async function reviseQuote() {
    if (!crmQuote) return;
    if (reviseQuote._busy) return; reviseQuote._busy = true;
    try {
        crmQuote = await apiFetch('/api/quotes/' + crmQuote.id + '/revise', { method: 'POST' });
        showToast('New revision created', 'success');
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) { console.error('reviseQuote:', e); showToast('Error revising quote', 'error'); }
    finally { reviseQuote._busy = false; }
}

async function reopenQuote(revise) {
    if (!crmQuote) return;
    try {
        crmQuote = await apiFetch('/api/quotes/' + crmQuote.id + '/reopen', {
            method: 'POST', body: { revise: revise }
        });
        showToast(revise ? 'Quote reopened with new revision' : 'Quote reopened', 'success');
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) { console.error('reopenQuote:', e); showToast('Error reopening quote', 'error'); }
}

// ── Pricing History ────────────────────────────────────────────────────

async function openPricingHistory(mpn) {
    openModal('phModal');
    const phMpn = document.getElementById('phMpn'); if (phMpn) phMpn.textContent = mpn;
    const phContent = document.getElementById('phContent');
    if (phContent) phContent.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const data = await apiFetch('/api/pricing-history/' + encodeURIComponent(mpn));
        if (!data.history?.length) {
            if (phContent) phContent.innerHTML = '<p class="empty">No pricing history for this MPN</p>';
            return;
        }
        let html = '<table class="tbl"><thead><tr><th>Date</th><th>Qty</th><th>Sell</th><th>Margin</th><th>Customer</th><th>Result</th></tr></thead><tbody>';
        data.history.forEach(h => {
            html += '<tr><td>' + fmtDate(h.date) + '</td><td>' + (h.qty||0).toLocaleString() + '</td><td>$' + Number(h.sell_price||0).toFixed(4) + '</td><td>' + Number(h.margin_pct||0).toFixed(1) + '%</td><td>' + esc(h.customer||'') + '</td><td>' + (h.result ? '<span class="status-badge status-'+h.result+'">'+h.result+'</span>' : '—') + '</td></tr>';
        });
        html += '</tbody></table>';
        html += '<div class="ph-summary">Avg: $' + Number(data.avg_price||0).toFixed(4) + ' · Margin: ' + Number(data.avg_margin||0).toFixed(1) + '%' + (data.price_range ? ' · Range: $'+Number(data.price_range[0]).toFixed(4)+' – $'+Number(data.price_range[1]).toFixed(4) : '') + '</div>';
        if (phContent) phContent.innerHTML = html;
    } catch (e) { logCatchError('pricingHistory', e); if (phContent) phContent.innerHTML = '<p class="empty">Error loading pricing</p>'; }
}

// ── Clone Requisition ──────────────────────────────────────────────────

async function cloneRequisition(reqId) {
    if (!confirm('Clone this requisition? All parts and offers will be copied.')) return;
    try {
        const data = await apiFetch('/api/requisitions/' + reqId + '/clone', { method: 'POST' });
        showToast('Requisition cloned', 'success');
        await loadRequisitions();
        toggleDrillDown(data.id);
    } catch (e) { console.error('cloneRequisition:', e); showToast('Error cloning requisition', 'error'); }
}

// ── User list loader for owner dropdowns ──────────────────────────────
let _userListCache = null;
async function loadUserOptions(selectId) {
    try {
        if (!_userListCache) {
            try { _userListCache = await apiFetch('/api/users/list'); }
            catch (e) { logCatchError('loadUserOptions', e); _userListCache = []; }
        }
        const sel = document.getElementById(selectId);
        if (!sel) return;
        sel.innerHTML = '<option value="">— None —</option>' +
            _userListCache.map(u => '<option value="' + u.id + '">' + esc(u.name) + ' (' + u.role + ')</option>').join('');
    } catch (e) { logCatchError('loadUserOptions', e); }
}

// ── Customer site typeahead for req creation ──────────────────────────
let _siteListCache = null;
async function loadSiteOptions() {
    try {
        const companies = await apiFetch('/api/companies/typeahead');
        _siteListCache = [];
        companies.forEach(c => {
            const sites = c.sites || [];
            if (sites.length === 0) {
                // Company exists but has no site — still show it
                _siteListCache.push({
                    id: null,
                    companyId: c.id,
                    label: c.name,
                    companyName: c.name,
                    siteName: '',
                    needsSite: true,
                });
            } else {
                sites.forEach(s => {
                    _siteListCache.push({
                        id: s.id,
                        companyId: c.id,
                        label: sites.length > 1 && s.site_name !== c.name ? c.name + ' — ' + s.site_name : c.name,
                        companyName: c.name,
                        siteName: s.site_name,
                    });
                });
            }
        });
    } catch (e) { console.error('loadSiteOptions:', e); }
}

// ── Site Typeahead ────────────────────────────────────────────────────
function filterSiteTypeahead(query) {
    const list = document.getElementById('nrSiteList');
    if (!list) return;
    if (!_siteListCache) {
        loadSiteOptions().then(() => filterSiteTypeahead(query));
        return;
    }
    const q = query.toLowerCase().trim();
    const matches = q ? _siteListCache.filter(s => s.label.toLowerCase().includes(q)).slice(0, 8) : _siteListCache.slice(0, 8);
    let html = '';
    if (matches.length === 0) {
        html = '<div class="site-typeahead-item" style="color:var(--muted)">No matches found</div>';
    } else {
        html = matches.map(s => {
            if (s.needsSite) {
                return '<div class="site-typeahead-item" onclick="autoCreateSiteAndSelect(' + s.companyId + ',\'' + escAttr(s.companyName) + '\')" style="color:var(--amber)">' + esc(s.companyName) + ' <span style="font-size:10px;opacity:.7">(no site — click to add)</span></div>';
            }
            return '<div class="site-typeahead-item" onclick="selectSite(' + s.id + ',\'' + escAttr(s.label) + '\')">' + esc(s.label) + '</div>';
        }).join('');
    }
    // Always show "+ New Account" at the bottom
    const qEsc = escAttr(q);
    html += '<div class="site-typeahead-item site-typeahead-add" onclick="quickCreateCompany(\'' + qEsc + '\')">+ New Account' + (q ? ': <b>' + esc(q) + '</b>' : '') + '</div>';
    list.innerHTML = html;
    list.classList.add('show');
}

function selectSite(id, label) {
    const siEl = document.getElementById('nrSiteId'); if (siEl) siEl.value = id;
    const ssEl = document.getElementById('nrSiteSearch'); if (ssEl) { ssEl.value = label; ssEl.style.display = 'none'; }
    document.getElementById('nrSiteList')?.classList.remove('show');
    // Show selected badge
    const sel = document.getElementById('nrSiteSelected');
    if (sel) {
        const lbl = document.getElementById('nrSiteSelectedLabel'); if (lbl) lbl.textContent = label;
        sel.classList.remove('u-hidden');
    }
    // Load contacts for the selected site's company
    loadNrContacts(id);
}

async function autoCreateSiteAndSelect(companyId, companyName) {
    // Company exists without a site — auto-create "HQ" site and select it
    try {
        const site = await apiFetch('/api/companies/' + companyId + '/sites', {
            method: 'POST', body: { site_name: 'HQ' }
        });
        await loadSiteOptions();
        selectSite(site.id, companyName);
        showToast('Default site created for ' + companyName, 'success');
    } catch (e) { showToast('Failed to create site', 'error'); }
}

async function quickCreateCompany(prefill) {
    // Close typeahead, open the new company modal with pre-filled name
    document.getElementById('nrSiteList')?.classList.remove('show');
    const ncN = document.getElementById('ncName'); if (ncN) ncN.value = prefill || '';
    // Mark that we came from the req modal so we can auto-select after
    window._quickCreateFromReq = true;
    openModal('newCompanyModal', 'ncName');
}

async function loadNrContacts(siteId) {
    const field = document.getElementById('nrContactField');
    const select = document.getElementById('nrContactSelect');
    if (!field || !select) return;
    // Find site in cache to get company info
    const site = (_siteListCache || []).find(s => s.id === siteId);
    if (!site) { field.classList.add('u-hidden'); return; }
    // Fetch company sites to get contacts
    try {
        const companies = await apiFetch(`/api/companies?search=${encodeURIComponent(site.companyName)}`);
        const company = companies.find(c => c.name === site.companyName);
        if (!company || !company.sites) { field.classList.add('u-hidden'); return; }
        const contacts = company.sites
            .filter(s => s.contact_name)
            .map(s => ({ siteId: s.id, name: s.contact_name, email: s.contact_email, siteName: s.site_name }));
        if (contacts.length === 0) { field.classList.add('u-hidden'); return; }
        select.innerHTML = '<option value="">— Select contact —</option>' +
            contacts.map(c =>
                `<option value="${c.siteId}" ${c.siteId === siteId ? 'selected' : ''}>${esc(c.name)}${c.email ? ' (' + esc(c.email) + ')' : ''} — ${esc(c.siteName)}</option>`
            ).join('');
        field.classList.remove('u-hidden');
    } catch (e) { logCatchError('loadNrContacts', e); field.classList.add('u-hidden'); }
}

// Close typeahead on outside click
document.addEventListener('click', function(e) {
    const list = document.getElementById('nrSiteList');
    if (list && !e.target.closest('.site-typeahead')) {
        list.classList.remove('show');
    }
});

// ── Suggested Contacts ────────────────────────────────────────────────

let scContext = {};  // {type: 'vendor'|'site', id: ..., domain: ..., name: ...}
let scResults = [];
let scSelected = new Set();

function openSuggestedContacts(type, id, domain, name) {
    scContext = { type, id, domain, name };
    scResults = [];
    scSelected.clear();
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('scModalTitle', 'textContent', 'Suggested Contacts — ' + (name || domain));
    _s('scModalSubtitle', 'textContent', type === 'vendor'
        ? 'Select contacts to add to this vendor card'
        : 'Select a contact to set as the site\'s primary contact');
    _s('scTitleFilter', 'value', '');
    _s('scResults', 'innerHTML', '<p class="empty">Click Search to find contacts at ' + esc(domain) + '</p>');
    const scBtn = document.getElementById('scAddBtn'); if (scBtn) scBtn.style.display = 'none';
    openModal('suggestedContactsModal', 'scTitleFilter');
}

async function searchSuggestedContacts() {
    const domain = scContext.domain;
    if (!domain) return;
    const title = document.getElementById('scTitleFilter')?.value?.trim() || '';
    const el = document.getElementById('scResults');
    el.innerHTML = '<p class="empty" style="padding:12px">Searching…</p>';
    scSelected.clear();
    updateScAddBtn();
    try {
        let url = `/api/suggested-contacts?domain=${encodeURIComponent(domain)}`;
        if (scContext.name) url += `&name=${encodeURIComponent(scContext.name)}`;
        if (title) url += `&title=${encodeURIComponent(title)}`;
        const data = await apiFetch(url);
        scResults = data.contacts || [];
        renderSuggestedContacts();
    } catch (e) {
        el.innerHTML = '<p class="empty" style="padding:12px">' + esc(e.message || 'Error searching contacts') + '</p>';
        console.error('searchSuggestedContacts:', e);
    }
}

function renderSuggestedContacts() {
    const el = document.getElementById('scResults');
    if (!scResults.length) {
        el.innerHTML = '<p class="empty" style="padding:12px">No contacts found — try a different title filter or check the domain</p>';
        const scBtn = document.getElementById('scAddBtn'); if (scBtn) scBtn.style.display = 'none';
        return;
    }
    el.innerHTML = scResults.map((c, i) => {
        const checked = scSelected.has(i) ? 'checked' : '';
        const selClass = scSelected.has(i) ? ' selected' : '';
        return `<div class="sc-row${selClass}" onclick="scToggle(${i}, event)">
            <input type="checkbox" ${checked} onclick="event.stopPropagation();scToggle(${i}, event)">
            <div class="sc-info">
                <div class="sc-name">${esc(c.full_name || '—')}</div>
                <div class="sc-title">${esc(c.title || 'No title')}</div>
                <div class="sc-meta">
                    ${c.email ? '<a href="mailto:' + escAttr(c.email) + '" onclick="event.stopPropagation();autoLogEmail(\'' + escAttr(c.email) + '\',\'' + escAttr(c.full_name || '') + '\')">✉ ' + esc(c.email) + '</a>' : ''}
                    ${c.phone ? phoneLink(c.phone, {company_id: _selectedCustId, origin: 'enrichment_contacts'}) : ''}
                    ${c.linkedin_url ? '<a href="' + escAttr(c.linkedin_url) + '" target="_blank" onclick="event.stopPropagation()">LinkedIn ↗</a>' : ''}
                    ${c.location ? '<span>📍 ' + esc(c.location) + '</span>' : ''}
                </div>
            </div>
            <span class="sc-badge">${esc(c.source || '')}</span>
        </div>`;
    }).join('');
    updateScAddBtn();
}

function scToggle(idx, e) {
    if (e) e.stopPropagation();
    if (scContext.type === 'site') {
        // Single-select for sites (one primary contact)
        scSelected.clear();
        scSelected.add(idx);
    } else {
        // Multi-select for vendors
        if (scSelected.has(idx)) scSelected.delete(idx);
        else scSelected.add(idx);
    }
    renderSuggestedContacts();
}

function updateScAddBtn() {
    const btn = document.getElementById('scAddBtn');
    if (scSelected.size > 0) {
        btn.style.display = '';
        btn.textContent = scContext.type === 'site'
            ? 'Set as Primary Contact'
            : `Add Selected (${scSelected.size})`;
    } else {
        btn.style.display = 'none';
    }
}

async function addSelectedSuggestedContacts() {
    const contacts = [...scSelected].map(i => scResults[i]).filter(Boolean);
    if (!contacts.length) return;
    try {
        if (scContext.type === 'vendor') {
            const data = await apiFetch('/api/suggested-contacts/add-to-vendor', {
                method: 'POST', body: { vendor_card_id: scContext.id, contacts }
            });
            showToast(`${data.added} contact${data.added !== 1 ? 's' : ''} added`, 'success');
            closeModal('suggestedContactsModal');
            openVendorPopup(scContext.id);  // Refresh vendor popup
        } else if (scContext.type === 'site') {
            const c = contacts[0];
            await apiFetch('/api/suggested-contacts/add-to-site', {
                method: 'POST', body: { site_id: scContext.id, contact: c }
            });
            showToast('Contact set on site', 'success');
            closeModal('suggestedContactsModal');
            loadCustomers();
        }
    } catch (e) {
        console.error('addSelectedSuggestedContacts:', e);
        showToast('Error adding contacts', 'error');
    }
}


// ── Unified Enrichment ────────────────────────────────────────────────

async function unifiedEnrichCompany(companyId) {
    showToast('Enriching company — this may take a moment…', 'info');
    try {
        // Trigger customer enrichment waterfall (contacts) in parallel with deep enrichment (firmographics)
        const [custRes, deepRes] = await Promise.allSettled([
            apiFetch(`/api/enrichment/customer/${companyId}`, { method: 'POST', body: { force: true } }),
            apiFetch(`/api/enrichment/company/${companyId}`, { method: 'POST', body: { force: true } }),
        ]);

        const cResult = custRes.status === 'fulfilled' ? custRes.value : {};
        const dResult = deepRes.status === 'fulfilled' ? deepRes.value : {};

        const contactsAdded = cResult.contacts_added || 0;
        const sourcesUsed = (cResult.sources_used || []).join(', ');
        const fields = dResult.enriched_fields || [];
        const dataFields = fields.filter(f => !f.startsWith('contact_queued:')).length;

        let msg = '';
        if (dataFields > 0) msg += `${dataFields} field${dataFields !== 1 ? 's' : ''} updated`;
        if (contactsAdded > 0) {
            if (msg) msg += ', ';
            msg += `${contactsAdded} contact${contactsAdded !== 1 ? 's' : ''} added`;
            if (sourcesUsed) msg += ` (via ${sourcesUsed})`;
        }
        if (!msg) msg = cResult.error || dResult.status || 'done';

        showToast('Enrichment: ' + msg, (contactsAdded > 0 || dataFields > 0) ? 'success' : 'info');
        loadCustomers();
    } catch (e) {
        console.error('unifiedEnrichCompany:', e);
        showToast('Enrichment failed: ' + (e.message || e), 'error');
    }
}

// ── Customer Enrichment Helpers ──────────────────────────────────────

function _custEnrichBadge(company) {
    const status = company.customer_enrichment_status;
    if (status === 'complete') return '<span style="color:#22c55e;font-size:10px" title="Contacts complete">&#9679;</span>';
    if (status === 'partial') return '<span style="color:#eab308;font-size:10px" title="Contacts partial">&#9679;</span>';
    return '<span style="color:#ef4444;font-size:10px" title="Contacts missing/stale">&#9679;</span>';
}

function _contactRoleBadge(role) {
    const colors = { buyer: '#3b82f6', technical: '#8b5cf6', decision_maker: '#f59e0b', operations: '#64748b' };
    const labels = { buyer: 'Buyer', technical: 'Technical', decision_maker: 'Decision Maker', operations: 'Ops' };
    const color = colors[role] || '#94a3b8';
    const label = labels[role] || role || '';
    if (!label) return '';
    return `<span style="background:${color}20;color:${color};padding:1px 5px;border-radius:3px;font-size:9px;font-weight:600;margin-left:3px">${label}</span>`;
}

function _enrichSourceBadge(source) {
    if (!source) return '';
    const colors = { lusha: '#7c3aed', apollo: '#f97316', hunter: '#10b981', manual: '#64748b' };
    const color = colors[source] || '#94a3b8';
    return `<span style="color:${color};font-size:9px;margin-left:3px">via ${source}</span>`;
}

async function verifyContactEmail(contactId, email) {
    try {
        showToast('Verifying email...', 'info');
        const res = await apiFetch('/api/enrichment/verify-email', { method: 'POST', body: { email: email } });
        const status = res.status || 'unknown';
        const score = res.score || 0;
        showToast(`Email ${email}: ${status} (score: ${score})`, status === 'valid' ? 'success' : 'warn');
    } catch (e) {
        showToast('Email verification failed: ' + (e.message || e), 'error');
    }
}

async function loadCreditUsage() {
    try {
        const data = await apiFetch('/api/enrichment/credits');
        const el = document.getElementById('creditUsagePanel');
        if (!el || !data.credits) return;
        let html = '<div style="display:flex;flex-wrap:wrap;gap:12px">';
        for (const c of data.credits) {
            const pct = c.limit > 0 ? Math.round((c.used / c.limit) * 100) : 0;
            const color = pct >= 90 ? 'var(--red)' : pct >= 70 ? 'var(--amber)' : 'var(--green)';
            html += `<div style="flex:1;min-width:140px;padding:8px;border:1px solid var(--border);border-radius:6px">
                <div style="font-size:11px;font-weight:600;text-transform:capitalize">${c.provider.replace('_',' ')}</div>
                <div style="font-size:18px;font-weight:700;color:${color}">${c.used}<span style="font-size:11px;color:var(--muted)">/${c.limit}</span></div>
                <div style="height:4px;background:var(--border);border-radius:2px;margin-top:4px">
                    <div style="height:100%;width:${pct}%;background:${color};border-radius:2px"></div>
                </div>
            </div>`;
        }
        html += '</div>';
        el.innerHTML = html;
    } catch (e) {
        console.error('loadCreditUsage:', e);
    }
}

async function loadCustomerGaps() {
    const el = document.getElementById('customerGapsList');
    if (!el) return;
    try {
        const data = await apiFetch('/api/enrichment/customer-gaps?limit=100');
        const gaps = data.gaps || [];
        if (!gaps.length) { el.innerHTML = '<p class="empty">All customer accounts have contacts!</p>'; return; }
        let html = '<table class="tbl"><thead><tr><th>Company</th><th>Domain</th><th>Owner</th><th>Contacts Needed</th><th>Status</th><th>Action</th></tr></thead><tbody>';
        for (const g of gaps) {
            const ownerBadge = g.account_owner_id
                ? '<span style="color:var(--green);font-size:10px">Assigned</span>'
                : '<span style="color:var(--muted);font-size:10px">Unassigned</span>';
            html += `<tr>
                <td>${esc(g.company_name)}</td>
                <td style="font-size:11px;color:var(--muted)">${esc(g.domain || '-')}</td>
                <td>${ownerBadge}</td>
                <td>${g.contacts_needed}</td>
                <td>${g.current_status || 'missing'}</td>
                <td><button class="btn btn-sm" onclick="unifiedEnrichCompany(${g.company_id})">Enrich</button></td>
            </tr>`;
        }
        html += '</tbody></table>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<p class="empty" style="color:var(--red)">Failed to load gaps</p>';
    }
}

async function startCustomerBackfill() {
    const status = document.getElementById('gapStatus');
    if (status) status.textContent = 'Starting backfill...';
    try {
        const res = await apiFetch('/api/enrichment/customer-backfill', { method: 'POST', body: { max_accounts: 50 } });
        if (status) status.textContent = `Done: ${res.enriched || 0} enriched, ${res.errors || 0} errors`;
        loadCustomerGaps();
    } catch (e) {
        if (status) status.textContent = 'Error: ' + (e.message || e);
    }
}

async function unifiedEnrichVendor(vendorId) {
    showToast('Enriching vendor — this may take a moment…', 'info');
    try {
        const res = await apiFetch(`/api/enrichment/vendor/${vendorId}`, {
            method: 'POST',
            body: { force: true },
        });
        if (res.status === 'completed') {
            const n = (res.enriched_fields || []).length;
            showToast(`Enrichment complete — ${n} field${n !== 1 ? 's' : ''} updated`, 'success');
        } else {
            showToast('Enrichment: ' + (res.status || 'done'));
        }
        openVendorPopup(vendorId);
    } catch (e) {
        console.error('unifiedEnrichVendor:', e);
        showToast('Enrichment failed: ' + (e.message || e), 'error');
    }
}


// ── Init CRM on page load ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    loadUserOptions('asSiteOwner');
    loadSiteOptions();
    // loVendor is now a text input in the inline Log Offer modal (app.js)
    initNameAutocomplete('ncName', 'ncNameList', null, { types: 'all' });
    // Check for token-based approval links
    checkTokenApproval();
});


// ══════════════════════════════════════════════════════════════════════
// Intelligence Layer — AI-powered features
// ══════════════════════════════════════════════════════════════════════

// ── Feature 1: AI Contact Enrichment ──────────────────────────────────

async function findAIContacts(entityType, entityId, companyName, domain) {
    const btn = event?.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Searching…'; }
    try {
        const data = await apiFetch('/api/ai/find-contacts', {
            method: 'POST', body: {
                entity_type: entityType,
                entity_id: entityId,
                company_name: companyName,
                domain: domain || null,
            }
        });
        if (data.total === 0) {
            showToast('No contacts found', 'info');
            return;
        }
        showToast(`Found ${data.total} contact(s)`, 'success');
        openAIContactsPanel(data.contacts, entityType, entityId);
    } catch (e) {
        console.error('findAIContacts:', e);
        showToast('Contact search error', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🤖 Find Contacts'; }
    }
}

let _aiPanelContext = {};  // {entityType, entityId}

function openAIContactsPanel(contacts, entityType, entityId) {
    _aiPanelContext = { entityType, entityId };
    const old = document.getElementById('aiContactsBg');
    if (old) old.remove();

    const isVendor = entityType === 'vendor';
    const bg = document.createElement('div');
    bg.id = 'aiContactsBg';
    bg.className = 'ai-panel-bg';
    bg.onclick = e => { if (e.target === bg) bg.remove(); };
    bg.innerHTML = `
        <div class="ai-panel">
            <div class="ai-panel-header">
                <h3>Found Contacts <span style="font-size:11px;color:var(--muted);font-weight:400">(${contacts.length})</span></h3>
                <button class="btn-close-ai" onclick="document.getElementById('aiContactsBg').remove()">✕</button>
            </div>
            <div style="max-height:400px;overflow-y:auto">
                ${contacts.map(c => `
                    <div class="ai-contact-row" id="aiRow${c.id}">
                        <div class="ai-contact-info">
                            <div class="ai-contact-name">${esc(c.full_name)}</div>
                            <div class="ai-contact-title">${esc(c.title || 'No title')}</div>
                            <div class="ai-contact-meta">
                                ${c.email ? `<a href="mailto:${escAttr(c.email)}" onclick="autoLogEmail('${escAttr(c.email)}','${escAttr(c.full_name || '')}')">✉ ${esc(c.email)}</a>` : ''}
                                ${c.phone ? phoneLink(c.phone, {company_id: _selectedCustId, origin: 'ai_contacts'}) : ''}
                                ${c.linkedin_url ? `<a href="${escAttr(c.linkedin_url)}" target="_blank">LinkedIn ↗</a>` : ''}
                            </div>
                        </div>
                        <div class="ai-contact-actions">
                            <span class="badge ${c.confidence === 'high' ? 'badge-green' : c.confidence === 'medium' ? 'badge-yellow' : 'badge-gray'}"
                                  title="Source: ${esc(c.source)}">${esc(c.confidence)}</span>
                            ${!c.is_saved
                                ? `<button class="btn btn-ghost btn-sm" id="aiSave${c.id}" onclick="saveAIContact(${c.id})" title="${isVendor ? 'Add to vendor card' : 'Save contact'}">${isVendor ? 'Add' : 'Save'}</button>`
                                : '<span class="badge badge-green">Added</span>'}
                            <button class="btn btn-danger btn-sm" onclick="deleteAIContact(${c.id})" title="Remove contact">✕</button>
                        </div>
                    </div>
                `).join('')}
            </div>
            <div class="mactions" style="margin-top:12px">
                <button class="btn btn-ghost" onclick="document.getElementById('aiContactsBg').remove()">Close</button>
            </div>
        </div>
    `;
    document.body.appendChild(bg);
}

async function saveAIContact(contactId) {
    const btn = document.getElementById(`aiSave${contactId}`);
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try {
        // First mark as saved in prospect_contacts
        const pc = await apiFetch(`/api/ai/prospect-contacts/${contactId}/save`, {
            method: 'POST', body: {}
        });
        // If it's a vendor, also add to vendor_contacts so it shows on the card
        if (_aiPanelContext.entityType === 'vendor' && _aiPanelContext.entityId) {
            const contact = pc.contact || pc;
            await apiFetch('/api/suggested-contacts/add-to-vendor', {
                method: 'POST', body: {
                    vendor_card_id: _aiPanelContext.entityId,
                    contacts: [{
                        full_name: contact.full_name || '',
                        title: contact.title || '',
                        email: contact.email || '',
                        phone: contact.phone || '',
                        linkedin_url: contact.linkedin_url || '',
                        source: contact.source || 'ai',
                    }]
                }
            });
            showToast('Contact added to vendor', 'success');
            // Refresh vendor contacts in background
            loadVendorContacts(_aiPanelContext.entityId);
        } else {
            showToast('Contact saved', 'success');
        }
        if (btn) { btn.outerHTML = '<span class="badge badge-green">Added</span>'; }
    } catch (e) {
        showToast('Save error', 'error');
        if (btn) { btn.disabled = false; btn.textContent = _aiPanelContext.entityType === 'vendor' ? 'Add' : 'Save'; }
    }
}

async function deleteAIContact(contactId) {
    if (!confirm('Remove this contact?')) return;
    try {
        await apiFetch(`/api/ai/prospect-contacts/${contactId}`, { method: 'DELETE' });
        const row = document.getElementById(`aiRow${contactId}`);
        if (row) row.remove();
        showToast('Contact removed', 'info');
    } catch (e) {
        showToast('Delete error', 'error');
    }
}


// ── Feature 2: Response Parse Preview ─────────────────────────────────

async function parseResponseAI(responseId) {
    const btn = event?.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Parsing…'; }
    try {
        const data = await apiFetch(`/api/ai/parse-response/${responseId}`, { method: 'POST' });
        if (!data.parsed) {
            showToast(data.reason || 'Could not parse', 'info');
            return;
        }
        openParsePreviewModal(data, responseId);
    } catch (e) {
        console.error('parseResponseAI:', e);
        showToast('Parse error', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🤖 Parse'; }
    }
}

function openParsePreviewModal(data, responseId) {
    const old = document.getElementById('parseBg');
    if (old) old.remove();

    const confPct = Math.round((data.confidence || 0) * 100);
    const confClass = confPct >= 80 ? 'parse-conf-high' : confPct >= 50 ? 'parse-conf-med' : 'parse-conf-low';

    const partsHtml = (data.parts || []).map(p => `
        <tr>
            <td><strong>${esc(p.mpn || '—')}</strong></td>
            <td>${esc(p.status || '—')}</td>
            <td>${p.qty_available || '—'}</td>
            <td>${p.unit_price ? '$' + Number(p.unit_price).toFixed(4) : '—'}</td>
            <td>${esc(p.lead_time || '—')}</td>
            <td>${esc(p.condition || '—')}</td>
            <td>${esc(p.date_code || '—')}</td>
        </tr>
    `).join('');

    window._pendingDraftOffers = data.draft_offers || [];

    const bg = document.createElement('div');
    bg.id = 'parseBg';
    bg.className = 'ai-panel-bg';
    bg.onclick = e => { if (e.target === bg) bg.remove(); };
    bg.innerHTML = `
        <div class="ai-panel" style="max-width:760px">
            <div class="ai-panel-header">
                <h3>Parsed Vendor Response</h3>
                <button class="btn-close-ai" onclick="document.getElementById('parseBg').remove()">✕</button>
            </div>
            <div class="parse-header">
                <div style="display:flex;gap:8px;align-items:center">
                    <span class="parse-confidence ${confClass}">${confPct}%</span>
                    <span class="parse-classification">${esc(data.classification || 'unknown')}</span>
                </div>
                ${data.auto_apply ? '<span class="badge badge-green">Auto-apply eligible</span>' : ''}
                ${data.needs_review ? '<span class="badge badge-yellow">Needs review</span>' : ''}
            </div>
            ${data.vendor_notes ? `<p style="font-size:12px;color:var(--text2);margin:8px 0;padding:8px;background:var(--bg);border-radius:6px">${esc(data.vendor_notes)}</p>` : ''}
            ${(data.parts || []).length ? `
            <table class="parse-parts-table">
                <thead><tr><th>MPN</th><th>Status</th><th>Qty</th><th>Price</th><th>Lead Time</th><th>Cond</th><th>DC</th></tr></thead>
                <tbody>${partsHtml}</tbody>
            </table>` : '<p class="empty">No parts extracted</p>'}
            <div class="parse-actions">
                ${(data.draft_offers || []).length > 0 ? `
                    <button class="btn btn-primary" onclick="saveParsedOffers(${responseId})">
                        Save ${data.draft_offers.length} Offer(s)
                    </button>` : ''}
                <button class="btn btn-ghost" onclick="document.getElementById('parseBg').remove()">Close</button>
            </div>
        </div>
    `;
    document.body.appendChild(bg);
}

async function saveParsedOffers(responseId) {
    const offers = window._pendingDraftOffers || [];
    if (!currentReqId) { showToast('No requisition selected', 'error'); return; }
    if (saveParsedOffers._busy) return; saveParsedOffers._busy = true;
    try {
        const data = await apiFetch('/api/ai/save-parsed-offers', {
            method: 'POST', body: { response_id: responseId, offers, requisition_id: currentReqId }
        });
        showToast(`Saved ${data.created} offer(s) — review in Offers tab`, 'success');
        document.getElementById('parseBg')?.remove();
        loadOffers();
    } catch (e) {
        showToast('Save error', 'error');
    } finally { saveParsedOffers._busy = false; }
}


// ── Upgrade 2: Parse Response Attachments ────────────────────────────

async function parseResponseAttachments(responseId) {
    const btn = event ? event.target : null;
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Parsing…'; }
    try {
        const data = await apiFetch(`/api/email-mining/parse-response-attachments/${responseId}`, {
            method: 'POST',
        });
        if (data.parseable === 0) {
            showToast('No parseable attachments found on this response', 'warning');
            return;
        }
        showToast(
            `Parsed ${data.rows_parsed} rows from ${data.parseable} file(s) — ${data.sightings_created} sightings created`,
            data.sightings_created > 0 ? 'success' : 'info'
        );
    } catch (e) {
        showToast('Attachment parse error: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '📎 Attachments'; }
    }
}


// ── Feature 3: Company Intel Card ─────────────────────────────────────

async function loadCompanyIntel(companyName, domain, targetEl) {
    if (!targetEl) return;
    targetEl.innerHTML = '<p style="padding:8px;font-size:11px;color:var(--muted)">Loading intel…</p>';
    try {
        const params = new URLSearchParams({ company_name: companyName });
        if (domain) params.set('domain', domain);
        const data = await apiFetch('/api/ai/company-intel?' + params);
        if (!data.available) {
            targetEl.innerHTML = '';
            return;
        }
        renderIntelCard(data.intel, targetEl);
    } catch (e) {
        targetEl.innerHTML = '';
    }
}

function renderIntelCard(intel, el) {
    const metaItems = [];
    if (intel.revenue) metaItems.push(`<div class="intel-meta-item"><span class="intel-meta-label">Revenue</span> ${esc(intel.revenue)}</div>`);
    if (intel.employees) metaItems.push(`<div class="intel-meta-item"><span class="intel-meta-label">Employees</span> ${esc(intel.employees)}</div>`);
    if (intel.products) metaItems.push(`<div class="intel-meta-item"><span class="intel-meta-label">Products</span> ${esc(intel.products)}</div>`);

    el.innerHTML = `
        <details class="intel-card">
            <summary>🔍 Company Intel</summary>
            <div class="intel-body">
                ${intel.summary ? `<div class="intel-summary">${esc(intel.summary)}</div>` : ''}
                ${metaItems.length ? `<div class="intel-meta">${metaItems.join('')}</div>` : ''}
                ${intel.components_they_buy?.length ? `
                    <div class="intel-tags">
                        ${intel.components_they_buy.map(c => `<span class="intel-tag">${esc(c)}</span>`).join('')}
                    </div>` : ''}
                ${intel.opportunity_signals?.length ? `
                    <div class="intel-section">
                        <div class="intel-section-title">Opportunity Signals</div>
                        ${intel.opportunity_signals.map(s => `<div class="intel-signal">${esc(s)}</div>`).join('')}
                    </div>` : ''}
                ${intel.recent_news?.length ? `
                    <div class="intel-section">
                        <div class="intel-section-title">Recent News</div>
                        ${intel.recent_news.slice(0, 3).map(n => `<div style="font-size:11px;color:var(--text2);padding:2px 0">📰 ${esc(n)}</div>`).join('')}
                    </div>` : ''}
                ${intel.sources?.length ? `<div class="intel-source">Sources: ${intel.sources.slice(0, 3).map(esc).join(', ')}</div>` : ''}
            </div>
        </details>
    `;
}


// ── Site Contacts CRUD ─────────────────────────────────────────────────

function openAddSiteContact(siteId) {
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('scSiteId', 'value', siteId); _s('scContactId', 'value', '');
    _s('siteContactModalTitle', 'textContent', 'Add Contact');
    ['scFullName','scTitle','scEmail','scPhone','scNotes'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    _s('scPrimary', 'checked', false);
    openModal('siteContactModal', 'scFullName');
}

async function openEditSiteContact(siteId, contactId) {
    try {
        const contacts = await apiFetch('/api/sites/' + siteId + '/contacts');
        const c = contacts.find(x => x.id === contactId);
        if (!c) { showToast('Contact not found', 'error'); return; }
        const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
        _s('scSiteId', 'value', siteId); _s('scContactId', 'value', contactId);
        _s('siteContactModalTitle', 'textContent', 'Edit Contact');
        _s('scFullName', 'value', c.full_name || ''); _s('scTitle', 'value', c.title || '');
        _s('scEmail', 'value', c.email || ''); _s('scPhone', 'value', c.phone || '');
        _s('scNotes', 'value', c.notes || ''); _s('scPrimary', 'checked', !!c.is_primary);
        openModal('siteContactModal', 'scFullName');
    } catch (e) { logCatchError('openEditSiteContact', e); showToast('Error loading contact', 'error'); }
}

async function saveSiteContact() {
    const _v = id => document.getElementById(id)?.value || '';
    const siteId = _v('scSiteId');
    const contactId = _v('scContactId');
    const data = {
        full_name: _v('scFullName').trim(),
        title: _v('scTitle').trim() || null,
        email: _v('scEmail').trim() || null,
        phone: _v('scPhone').trim() || null,
        notes: _v('scNotes').trim() || null,
        is_primary: document.getElementById('scPrimary')?.checked || false,
    };
    if (!data.full_name) { showToast('Name is required', 'error'); return; }
    try {
        const url = contactId
            ? '/api/sites/' + siteId + '/contacts/' + contactId
            : '/api/sites/' + siteId + '/contacts';
        await apiFetch(url, {
            method: contactId ? 'PUT' : 'POST', body: data
        });
        closeModal('siteContactModal');
        showToast(contactId ? 'Contact updated' : 'Contact added', 'success');
        // Refresh the site detail panel
        const panel = document.getElementById('siteDetail-' + siteId);
        if (panel) { panel.style.display = 'none'; toggleSiteDetail(parseInt(siteId)); }
    } catch (e) { console.error('saveSiteContact:', e); showToast('Error saving contact', 'error'); }
}

async function deleteSiteContact(siteId, contactId, name) {
    if (!confirm('Remove contact "' + name + '"?')) return;
    try {
        await apiFetch('/api/sites/' + siteId + '/contacts/' + contactId, { method: 'DELETE' });
        showToast('Contact removed', 'info');
        const panel = document.getElementById('siteDetail-' + siteId);
        if (panel) { panel.style.display = 'none'; toggleSiteDetail(siteId); }
    } catch (e) { console.error('deleteSiteContact:', e); showToast('Error deleting contact', 'error'); }
}

function filterSiteContacts(input, siteId) {
    const q = (input.value || '').trim().toLowerCase();
    const grid = document.getElementById('contactGrid-' + siteId);
    if (!grid) return;
    grid.querySelectorAll('.si-contact-card').forEach(card => {
        const text = card.dataset.contactSearch || '';
        card.style.display = !q || text.includes(q) ? '' : 'none';
    });
}

// ── Company Activity Tracking ─────────────────────────────────────────

async function loadCompanyActivityStatus(companyId) {
    const el = document.getElementById('actHealth-' + companyId);
    if (!el || el.dataset.loaded) return;
    try {
        const d = await apiFetch('/api/companies/' + companyId + '/activity-status');
        const colors = { green: 'var(--green)', yellow: 'var(--amber)', red: 'var(--red)', no_activity: 'var(--muted)' };
        const labels = { green: 'Active', yellow: 'At risk', red: 'Stale', no_activity: 'No activity' };
        const daysText = d.days_since_activity != null ? ' (' + d.days_since_activity + 'd)' : '';
        el.innerHTML = `<span class="badge activity-badge" style="background:color-mix(in srgb,${colors[d.status]} 15%,transparent);color:${colors[d.status]}">${labels[d.status]}${daysText}</span>`;
        el.dataset.loaded = '1';
    } catch(e) { logCatchError('companyActivityStatus', e); showToast('Failed to load activity status', 'error'); }
}

async function loadCompanyActivities(companyId) {
    const el = document.getElementById('actList-' + companyId);
    if (!el) return;
    try {
        const activities = await apiFetch('/api/companies/' + companyId + '/activities');
        if (!activities.length) {
            el.innerHTML = '<p class="empty" style="padding:4px;font-size:11px">No activity recorded yet</p>';
            return;
        }
        el.innerHTML = activities.slice(0, 10).map(a => {
            const icons = { email_sent: '&#x1f4e4;', email_received: '&#x1f4e5;', call_outbound: '&#x1f4de;', call_inbound: '&#x1f4f2;', note: '&#x1f4dd;', ownership_warning: '&#x26a0;&#xfe0f;' };
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
                <span class="act-row-meta">${typeof fmtRelative === 'function' ? fmtRelative(a.created_at) : (a.created_at || '').slice(0, 10)}</span>
            </div>`;
        }).join('');
    } catch(e) { logCatchError('companyActivities', e); el.innerHTML = '<p class="empty" style="font-size:11px">Error</p>'; }
}

async function saveLogCall() {
    const _v = id => document.getElementById(id)?.value || '';
    const companyId = _v('lcCompanyId');
    const data = {
        phone: _v('lcPhone').trim() || null,
        contact_name: _v('lcContactName').trim() || null,
        direction: _v('lcDirection'),
        duration_seconds: parseInt(_v('lcDuration')) || null,
        notes: _v('lcNotes').trim() || null,
    };
    try {
        await apiFetch('/api/companies/' + companyId + '/activities/call', {
            method: 'POST', body: data
        });
        closeModal('logCallModal');
        showToast('Call logged', 'success');
        // Invalidate RFQ activity cache if viewing a requisition
        if (currentReqId && window._ddTabCache && window._ddTabCache[currentReqId]) delete window._ddTabCache[currentReqId].activity;
        const el = document.getElementById('actList-' + companyId);
        if (el) el.innerHTML = '<p class="empty" style="padding:4px;font-size:11px">Loading...</p>';
        loadCompanyActivities(parseInt(companyId));
        const healthEl = document.getElementById('actHealth-' + companyId);
        if (healthEl) { delete healthEl.dataset.loaded; loadCompanyActivityStatus(parseInt(companyId)); }
    } catch(e) { console.error('saveLogCall:', e); showToast('Error logging call', 'error'); }
}

function openLogNoteModal(companyId, companyName) {
    const _s = (id, prop, v) => { const el = document.getElementById(id); if (el) el[prop] = v; };
    _s('lnCompanyId', 'value', companyId); _s('lnCompanyName', 'textContent', companyName);
    ['lnContactName','lnNotes'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    openModal('logNoteModal', 'lnNotes');
}

async function saveLogNote() {
    const _v = id => document.getElementById(id)?.value || '';
    const companyId = _v('lnCompanyId');
    const notes = _v('lnNotes').trim();
    if (!notes) { showToast('Note text is required', 'error'); return; }
    const data = {
        contact_name: _v('lnContactName').trim() || null,
        notes: notes,
    };
    try {
        await apiFetch('/api/companies/' + companyId + '/activities/note', {
            method: 'POST', body: data
        });
        closeModal('logNoteModal');
        showToast('Note added', 'success');
        // Invalidate RFQ activity cache if viewing a requisition
        if (currentReqId && window._ddTabCache && window._ddTabCache[currentReqId]) delete window._ddTabCache[currentReqId].activity;
        const el = document.getElementById('actList-' + companyId);
        if (el) el.innerHTML = '<p class="empty" style="padding:4px;font-size:11px">Loading...</p>';
        loadCompanyActivities(parseInt(companyId));
        const healthEl = document.getElementById('actHealth-' + companyId);
        if (healthEl) { delete healthEl.dataset.loaded; loadCompanyActivityStatus(parseInt(companyId)); }
    } catch(e) { console.error('saveLogNote:', e); showToast('Error adding note', 'error'); }
}

// ── Proactive Offers ──────────────────────────────────────────────────

let _proactiveGroups = [];
let _proactiveStats = {};
let _proactiveSent = [];
let _proactiveTab = 'matches';
let _proactiveSendSiteId = null;
let _proactiveSendMatchIds = [];
let _proactiveGroupsOpen = new Set();   // expanded customer groups
let _proactiveSelected = new Set();      // checked match IDs for send

// Proactive badge auto-refresh every 5 minutes
setInterval(() => { if (typeof refreshProactiveBadge === 'function') refreshProactiveBadge(); }, 5 * 60 * 1000);
let _proactiveSiteContacts = [];

async function showProactiveOffers() {
    showView('view-proactive');
    setCurrentReqId(null);
    switchProactiveTab('matches');
}

function switchProactiveTab(tab, btn) {
    _proactiveTab = tab;
    document.querySelectorAll('#proactiveTabs .tab').forEach(t => t.classList.remove('on'));
    if (btn) btn.classList.add('on');
    else document.querySelectorAll('#proactiveTabs .tab').forEach(t => {
        if (t.textContent.toLowerCase().includes(tab)) t.classList.add('on');
    });
    const _p = (id, show) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (show) { el.classList.remove('hidden'); el.style.display = ''; }
        else { el.classList.add('hidden'); el.style.display = 'none'; }
    };
    _p('proactiveMatchesPanel', tab === 'matches');
    _p('proactiveSentPanel', tab === 'sent');
    _p('proactiveScorecardPanel', tab === 'scorecard');
    if (tab === 'matches') loadProactiveMatches();
    else if (tab === 'sent') loadProactiveSent();
    else if (tab === 'scorecard') loadProactiveScorecard();
}

async function loadProactiveMatches() {
    try {
        const resp = await apiFetch('/api/proactive/matches');
        _proactiveGroups = resp.groups || [];
        _proactiveStats = resp.stats || {};
        // Pre-select all matches
        _proactiveSelected.clear();
        for (const g of _proactiveGroups) for (const m of g.matches) _proactiveSelected.add(String(m.id));
        // Auto-expand first group if nothing open
        if (_proactiveGroups.length && _proactiveGroupsOpen.size === 0)
            _proactiveGroupsOpen.add(_proactiveGroups[0].customer_site_id);
        renderProactiveStatsBar();
        renderProactiveMatches();
    } catch (e) { showToast('Failed to load matches', 'error'); }
}

function _marginColor(pct) {
    if (pct == null) return 'var(--muted)';
    if (pct > 30) return 'var(--green)';
    if (pct >= 15) return 'var(--amber)';
    return 'var(--red)';
}

function _scoreBadge(score) {
    let bg = 'var(--muted)';
    if (score >= 80) bg = 'var(--green)';
    else if (score >= 60) bg = 'var(--teal)';
    else if (score >= 40) bg = 'var(--amber)';
    else bg = 'var(--red)';
    return `<span style="display:inline-block;padding:1px 6px;border-radius:10px;font-size:10px;font-weight:700;color:#fff;background:${bg}">${score}</span>`;
}

function _fmtDaysAgo(isoDate) {
    if (!isoDate) return '—';
    const days = Math.floor((Date.now() - new Date(isoDate).getTime()) / 86400000);
    if (days <= 0) return 'Today';
    if (days === 1) return '1d ago';
    if (days < 30) return days + 'd ago';
    if (days < 365) return Math.floor(days / 30) + 'mo ago';
    return Math.floor(days / 365) + 'y ago';
}

function renderProactiveStatsBar() {
    const el = document.getElementById('proactiveStatsBar');
    if (!el) return;
    const s = _proactiveStats;
    if (!s.total) { el.classList.add('u-hidden'); return; }
    el.classList.remove('u-hidden');
    el.innerHTML = `<div style="display:flex;gap:16px;flex-wrap:wrap;padding:10px 12px;background:var(--bg2);border-radius:8px;font-size:12px">
        <div><span style="font-weight:700;font-size:18px;color:var(--teal)">${s.total}</span> <span style="color:var(--muted)">Matches</span></div>
        <div><span style="font-weight:700;font-size:18px">${s.avg_score || 0}</span> <span style="color:var(--muted)">Avg Score</span></div>
        <div><span style="font-weight:700;font-size:18px;color:${_marginColor(s.avg_margin)}">${s.avg_margin != null ? s.avg_margin + '%' : '—'}</span> <span style="color:var(--muted)">Avg Margin</span></div>
        <div><span style="font-weight:700;font-size:18px;color:var(--green)">${s.high_margin_count}</span> <span style="color:var(--muted)">&gt;30% Margin</span></div>
    </div>`;
}

function _vendorScoreClass(score) {
    if (score == null) return '';
    if (score >= 70) return 'color:var(--green)';
    if (score >= 40) return 'color:var(--amber)';
    return 'color:var(--red)';
}

function _vendorTip(m) {
    const parts = [];
    if (m.vendor_score != null) parts.push('Score: ' + m.vendor_score);
    if (m.overall_win_rate != null) parts.push('Win: ' + m.overall_win_rate + '%');
    if (m.ghost_rate != null) parts.push('Ghost: ' + m.ghost_rate + '%');
    return parts.join(' · ') || '';
}

function toggleProactiveGroup(siteId) {
    if (_proactiveGroupsOpen.has(siteId)) _proactiveGroupsOpen.delete(siteId);
    else _proactiveGroupsOpen.add(siteId);
    renderProactiveMatches();
}

function toggleProactiveSelect(matchId) {
    const k = String(matchId);
    if (_proactiveSelected.has(k)) _proactiveSelected.delete(k); else _proactiveSelected.add(k);
}

function toggleAllProactiveInGroup(siteId, checked) {
    const g = _proactiveGroups.find(g => g.customer_site_id === siteId);
    if (!g) return;
    for (const m of g.matches) { const k = String(m.id); if (checked) _proactiveSelected.add(k); else _proactiveSelected.delete(k); }
    renderProactiveMatches();
}

function _allGroupChecked(group) {
    return group.matches.length > 0 && group.matches.every(m => _proactiveSelected.has(String(m.id)));
}

function _selectedCountInGroup(group) {
    return group.matches.filter(m => _proactiveSelected.has(String(m.id))).length;
}

async function doNotOfferMatch(matchId, siteId) {
    const group = _proactiveGroups.find(g => g.customer_site_id === siteId);
    const m = group ? group.matches.find(x => x.id === matchId) : null;
    if (!m || !group) return;
    try {
        await apiFetch('/api/proactive/do-not-offer', { method: 'POST', body: { items: [{ mpn: m.mpn, company_id: group.company_id }] } });
        showToast(`"${m.mpn}" will not be offered to ${group.company_name} again`);
        loadProactiveMatches();
        if (typeof refreshProactiveBadge === 'function') refreshProactiveBadge();
    } catch (e) { showToast('Failed to set Do Not Offer', 'error'); }
}

async function doNotOfferSelected(siteId) {
    const group = _proactiveGroups.find(g => g.customer_site_id === siteId);
    if (!group) return;
    const items = group.matches
        .filter(m => _proactiveSelected.has(String(m.id)))
        .map(m => ({ mpn: m.mpn, company_id: group.company_id }));
    if (!items.length) { showToast('Select items first', 'error'); return; }
    try {
        await apiFetch('/api/proactive/do-not-offer', { method: 'POST', body: { items } });
        showToast(`${items.length} part${items.length !== 1 ? 's' : ''} suppressed for ${group.company_name}`);
        loadProactiveMatches();
        if (typeof refreshProactiveBadge === 'function') refreshProactiveBadge();
    } catch (e) { showToast('Failed to set Do Not Offer', 'error'); }
}

function renderProactiveMatches() {
    const el = document.getElementById('proactiveMatchesPanel');
    if (!_proactiveGroups.length) {
        el.innerHTML = '<p class="empty">No matches yet this week. This page shows you when a vendor offers parts that your past customers have requested — so you can reconnect and close a sale.</p>';
        return;
    }
    let html = '<div style="border:1px solid var(--border);border-radius:10px;overflow:hidden;background:var(--bg2)">';

    for (const group of _proactiveGroups) {
        const isOpen = _proactiveGroupsOpen.has(group.customer_site_id);
        const bestScore = Math.max(...group.matches.map(m => m.match_score || 0));
        const margins = group.matches.filter(m => m.margin_pct != null).map(m => m.margin_pct);
        const bestMargin = margins.length ? Math.max(...margins) : null;
        const selCount = _selectedCountInGroup(group);

        // ── Customer header row (always visible) ──
        html += `<div style="display:flex;align-items:center;gap:12px;padding:12px 16px;cursor:pointer;border-bottom:1px solid var(--border);transition:background .15s;${isOpen ? 'background:var(--bg3)' : ''}" onclick="toggleProactiveGroup(${group.customer_site_id})" class="proactive-group-hdr">
            <span style="font-size:10px;color:var(--muted);width:14px;text-align:center;transition:transform .2s;${isOpen ? 'transform:rotate(90deg)' : ''}">&#9654;</span>
            <span style="font-weight:700;font-size:14px;flex:1">${esc(group.company_name)}${group.site_name ? '<span style="color:var(--muted);font-weight:400;font-size:12px;margin-left:6px">' + esc(group.site_name) + '</span>' : ''}</span>
            <span style="font-size:11px;color:var(--muted);background:var(--bg);padding:2px 8px;border-radius:10px">${group.matches.length} match${group.matches.length !== 1 ? 'es' : ''}</span>
            ${_scoreBadge(bestScore)}
            <span style="font-weight:700;font-size:12px;min-width:50px;text-align:right;color:${_marginColor(bestMargin)}">${bestMargin != null ? bestMargin.toFixed(1) + '%' : '—'}</span>
            <div style="display:flex;gap:6px;margin-left:12px" onclick="event.stopPropagation()">
                <button class="btn btn-primary btn-sm" onclick="openProactiveSendModal(${group.customer_site_id})">Send</button>
                <button class="btn btn-ghost btn-sm" onclick="dismissProactiveGroup(${group.customer_site_id})">Dismiss</button>
            </div>
        </div>`;

        // ── Expanded detail ──
        if (isOpen) {
            html += `<div style="border-bottom:1px solid var(--border);background:var(--bg)">`;
            // Sub-header with selection info + DNO button
            html += `<div style="padding:8px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border)">
                <div style="font-size:11px;color:var(--muted)">
                    <input type="checkbox" style="accent-color:var(--teal);width:14px;height:14px;vertical-align:middle;margin-right:6px" onchange="toggleAllProactiveInGroup(${group.customer_site_id},this.checked)" ${_allGroupChecked(group) ? 'checked' : ''}>
                    <strong>${selCount} selected</strong>
                </div>
                <button class="btn btn-sm" style="background:transparent;color:var(--red);border:1px solid rgba(248,81,73,.3);font-size:10px" onclick="doNotOfferSelected(${group.customer_site_id})">&#128683; Do Not Offer Selected</button>
            </div>`;

            html += `<div style="overflow-x:auto"><table class="tbl" style="font-size:11px;width:100%"><thead><tr>
                <th style="width:28px"></th>
                <th>Part</th>
                <th>Cond</th>
                <th>Warranty</th>
                <th>Lead Time</th>
                <th>Location</th>
                <th style="text-align:right">Price</th>
                <th style="text-align:right">Qty</th>
                <th>Vendor</th>
                <th>Buyer</th>
                <th style="text-align:center">Score</th>
                <th style="text-align:right">Margin</th>
                <th style="text-align:center;font-size:9px" title="Do Not Offer Again">DNO</th>
                <th></th>
            </tr></thead><tbody>`;

            for (const m of group.matches) {
                const checked = _proactiveSelected.has(String(m.id));
                const vs = m.vendor_score != null ? m.vendor_score.toFixed(0) : '—';
                const cphBadge = m.customer_purchase_count > 0
                    ? `<span style="font-size:9px;color:var(--purple);background:rgba(167,139,250,.1);padding:1px 6px;border-radius:8px;margin-left:4px">${m.customer_purchase_count}x bought</span>`
                    : '';
                html += `<tr style="border-bottom:1px solid var(--border)">
                    <td><input type="checkbox" style="accent-color:var(--teal);width:14px;height:14px" ${checked ? 'checked' : ''} onchange="toggleProactiveSelect(${m.id})"></td>
                    <td><strong>${esc(m.mpn)}</strong>${cphBadge}</td>
                    <td>${esc(m.condition || '—')}</td>
                    <td>${esc(m.warranty || '—')}</td>
                    <td>${esc(m.lead_time || '—')}</td>
                    <td>${esc(m.country_of_origin || '—')}</td>
                    <td style="text-align:right;font-family:monospace;font-size:11px">${m.unit_price != null ? '$' + Number(m.unit_price).toFixed(4) : '—'}</td>
                    <td style="text-align:right">${(m.qty_available || 0).toLocaleString()}</td>
                    <td><span>${esc(m.vendor_name)}</span> <span style="font-size:9px;padding:1px 5px;border-radius:8px;border:1px solid var(--border);${_vendorScoreClass(m.vendor_score)}" title="${_vendorTip(m)}">${vs}</span></td>
                    <td style="font-size:10px;color:var(--muted)">${esc(m.entered_by_name || '—')}</td>
                    <td style="text-align:center">${_scoreBadge(m.match_score)}</td>
                    <td style="text-align:right;font-weight:700;color:${_marginColor(m.margin_pct)}">${m.margin_pct != null ? m.margin_pct.toFixed(1) + '%' : '—'}</td>
                    <td style="text-align:center"><input type="checkbox" style="accent-color:var(--red);width:14px;height:14px" onchange="doNotOfferMatch(${m.id},${group.customer_site_id})" title="Do not offer this part to ${esc(group.company_name)} again"></td>
                    <td style="text-align:center"><button class="btn btn-ghost btn-sm" style="padding:2px 6px;font-size:9px" onclick="dismissSingleMatch(${m.id})">&#10005;</button></td>
                </tr>`;
            }
            html += '</tbody></table></div></div>';
        }
    }
    html += '</div>';
    el.innerHTML = html;
}

async function dismissSingleMatch(matchId) {
    try {
        await apiFetch('/api/proactive/dismiss', { method: 'POST', body: { match_ids: [matchId] } });
        showToast('Match dismissed');
        loadProactiveMatches();
        if (typeof refreshProactiveBadge === 'function') refreshProactiveBadge();
    } catch (e) { showToast('Failed to dismiss', 'error'); }
}

async function dismissProactiveGroup(siteId) {
    const ids = [];
    _proactiveGroups.forEach(g => {
        if (g.customer_site_id === siteId) g.matches.forEach(m => ids.push(m.id));
    });
    if (!ids.length) return;
    try {
        await apiFetch('/api/proactive/dismiss', { method: 'POST', body: { match_ids: ids } });
        showToast('Matches dismissed', 'info');
        loadProactiveMatches();
        if (typeof refreshProactiveBadge === 'function') refreshProactiveBadge();
    } catch (e) { showToast('Failed to dismiss', 'error'); }
}

async function refreshProactiveMatches() {
    const btn = document.getElementById('proactiveRefreshBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
    try {
        const result = await apiFetch('/api/proactive/refresh', { method: 'POST' });
        const total = result.total_new || 0;
        showToast(total ? `Found ${total} new match${total !== 1 ? 'es' : ''}` : 'No new matches found');
        loadProactiveMatches();
        if (typeof refreshProactiveBadge === 'function') refreshProactiveBadge();
    } catch (e) { showToast('Scan failed', 'error'); }
    finally { if (btn) { btn.disabled = false; btn.textContent = 'Refresh Matches'; } }
}

async function openProactiveSendModal(siteId) {
    _proactiveSendSiteId = siteId;
    // Get selected match IDs for this site from _proactiveSelected state
    const group = _proactiveGroups.find(g => g.customer_site_id === siteId);
    _proactiveSendMatchIds = group
        ? group.matches.filter(m => _proactiveSelected.has(String(m.id))).map(m => m.id)
        : [];
    if (!_proactiveSendMatchIds.length) { showToast('Select at least one item', 'error'); return; }

    // Load contacts
    try {
        _proactiveSiteContacts = await apiFetch('/api/proactive/contacts/' + siteId);
    } catch (e) { logCatchError('proactiveContacts', e); _proactiveSiteContacts = []; }

    // Company name from group already resolved above
    const companyName = group ? group.company_name : '';

    // Populate modal
    const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    _s('psSiteId', siteId); _s('psSubject', 'Parts Available — ' + companyName); _s('psNotes', '');

    // Render contacts
    const contactsEl = document.getElementById('psContacts');
    if (!_proactiveSiteContacts.length) {
        contactsEl.innerHTML = '<p class="empty">No contacts on this customer site</p>';
    } else {
        contactsEl.innerHTML = _proactiveSiteContacts.map(c => `
            <label style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
                <input type="checkbox" class="ps-contact" value="${c.id}" ${c.is_primary ? 'checked' : ''}>
                ${esc(c.full_name)} ${c.email ? '<span style="color:var(--muted);font-size:11px">(' + esc(c.email) + ')</span>' : ''}
                ${c.is_primary ? '<span style="font-size:10px;color:var(--teal)">Primary</span>' : ''}
            </label>
        `).join('');
    }

    // Render items with sell price inputs
    const itemsEl = document.getElementById('psItems');
    const selectedMatches = [];
    if (group) {
        group.matches.forEach(m => {
            if (_proactiveSendMatchIds.includes(m.id)) selectedMatches.push(m);
        });
    }
    itemsEl.innerHTML = selectedMatches.map(m => {
        const defaultSell = m.our_cost ? (m.our_cost * 1.3).toFixed(4) : (m.unit_price ? (m.unit_price * 1.3).toFixed(4) : '0');
        return `<tr>
            <td>${esc(m.mpn)}</td>
            <td>${esc(m.vendor_name)}</td>
            <td>${(m.qty_available||0).toLocaleString()}</td>
            <td>$${m.our_cost != null ? Number(m.our_cost).toFixed(4) : (m.unit_price != null ? Number(m.unit_price).toFixed(4) : '—')}</td>
            <td><input type="number" step="0.0001" class="ps-sell" data-id="${m.id}" value="${defaultSell}" style="width:90px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px" oninput="_debouncedUpdateProactivePreview()"></td>
            <td class="ps-margin" data-id="${m.id}"></td>
        </tr>`;
    }).join('');
    updateProactivePreview();

    // Reset draft preview
    const draftPreview = document.getElementById('psDraftPreview');
    if (draftPreview) draftPreview.innerHTML = '<p style="color:var(--muted);font-style:italic">Click "AI Draft" to generate a personalized email, or type your own message here.</p>';
    const draftStatus = document.getElementById('psDraftStatus');
    if (draftStatus) draftStatus.textContent = '';
    _proactiveDraftHtml = null;

    openModal('proactiveSendModal');
}

let _proactiveDraftHtml = null;

async function generateProactiveDraft() {
    const btn = document.getElementById('psDraftBtn');
    const status = document.getElementById('psDraftStatus');
    const preview = document.getElementById('psDraftPreview');
    if (!btn || !preview) return;

    const contactIds = Array.from(document.querySelectorAll('.ps-contact:checked')).map(c => parseInt(c.value));
    const sellPrices = {};
    document.querySelectorAll('.ps-sell').forEach(input => {
        sellPrices[input.dataset.id] = parseFloat(input.value) || 0;
    });

    btn.disabled = true;
    btn.textContent = 'Drafting…';
    if (status) status.textContent = '';

    try {
        const result = await apiFetch('/api/proactive/draft', {
            method: 'POST',
            body: {
                match_ids: _proactiveSendMatchIds,
                contact_ids: contactIds,
                sell_prices: sellPrices,
                notes: document.getElementById('psNotes')?.value?.trim() || null,
            }
        });
        if (result.html) {
            preview.innerHTML = result.html;
            _proactiveDraftHtml = result.html;
        }
        if (result.subject) {
            const psS = document.getElementById('psSubject'); if (psS) psS.value = result.subject;
        }
        if (status) status.textContent = 'Draft generated — edit as needed';
    } catch (e) {
        logCatchError('proactiveDraft', e);
        if (status) status.textContent = 'Draft failed — send will use default template';
    } finally {
        btn.disabled = false;
        btn.textContent = 'AI Draft';
    }
}

function updateProactivePreview() {
    let totalSell = 0, totalCost = 0;
    document.querySelectorAll('.ps-sell').forEach(input => {
        const id = input.dataset.id;
        const sell = parseFloat(input.value) || 0;
        const group = _proactiveGroups.find(g => g.customer_site_id === _proactiveSendSiteId);
        const match = group ? group.matches.find(m => m.id === parseInt(id)) : null;
        const cost = match ? (match.our_cost || match.unit_price || 0) : 0;
        const targetQty = match ? (match.target_qty || 0) : 0;
        const availQty = match ? (match.qty_available || 0) : 0;
        const qty = targetQty > 0 ? Math.min(availQty, targetQty) : availQty;
        const margin = sell > 0 ? ((sell - cost) / sell * 100).toFixed(1) : '0.0';
        const marginEl = document.querySelector(`.ps-margin[data-id="${id}"]`);
        if (marginEl) marginEl.textContent = margin + '%';
        totalSell += sell * qty;
        totalCost += cost * qty;
    });
    const totalMargin = totalSell > 0 ? ((totalSell - totalCost) / totalSell * 100).toFixed(1) : '0.0';
    const previewEl = document.getElementById('psPreview');
    if (previewEl) previewEl.innerHTML = `Revenue: <strong>$${totalSell.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}</strong> · Margin: <strong>${totalMargin}%</strong> · Profit: <strong>$${(totalSell - totalCost).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}</strong>`;
}

async function sendProactiveOffer() {
    const contactIds = Array.from(document.querySelectorAll('.ps-contact:checked')).map(c => parseInt(c.value));
    if (!contactIds.length) { showToast('Select at least one contact', 'error'); return; }
    var btn = document.getElementById('psmSendBtn');

    const sellPrices = {};
    document.querySelectorAll('.ps-sell').forEach(input => {
        sellPrices[input.dataset.id] = parseFloat(input.value) || 0;
    });

    // Capture email HTML from draft preview (if user edited or AI-drafted)
    const draftPreview = document.getElementById('psDraftPreview');
    let emailHtml = null;
    if (draftPreview && _proactiveDraftHtml) {
        // Use current content of the editable preview (may have been hand-edited)
        emailHtml = draftPreview.innerHTML;
    }

    await guardBtn(btn, 'Sending…', async () => {
        try {
            await apiFetch('/api/proactive/send', {
                method: 'POST',
                body: {
                    match_ids: _proactiveSendMatchIds,
                    contact_ids: contactIds,
                    sell_prices: sellPrices,
                    subject: document.getElementById('psSubject')?.value?.trim() || '',
                    notes: document.getElementById('psNotes')?.value?.trim() || null,
                    email_html: emailHtml,
                }
            });
            showToast('Proactive offer sent!', 'success');
            closeModal('proactiveSendModal');
            loadProactiveMatches();
            if (typeof refreshProactiveBadge === 'function') refreshProactiveBadge();
        } catch (e) { showToast('Failed to send', 'error'); }
    });
}

async function loadProactiveSent() {
    try {
        _proactiveSent = await apiFetch('/api/proactive/offers');
        renderProactiveSent();
    } catch (e) { showToast('Failed to load sent offers', 'error'); }
}

function renderProactiveSent() {
    const el = document.getElementById('proactiveSentPanel');
    if (!el) return;
    if (!_proactiveSent || !_proactiveSent.length) {
        el.innerHTML = '<p class="empty">No proactive offers sent yet</p>';
        return;
    }
    const statusColors = { sent: 'var(--teal)', replied: 'var(--amber)', converted: 'var(--green)', expired: 'var(--muted)' };
    el.innerHTML = _proactiveSent.map(po => {
        const color = statusColors[po.status] || 'var(--muted)';
        const itemCount = (po.line_items || []).length;
        const convertBtn = po.status === 'sent' || po.status === 'replied'
            ? `<button class="btn btn-success btn-sm" onclick="convertProactiveOffer(${po.id})" style="margin-top:8px">Convert to Win</button>`
            : '';
        return `
        <div class="card" style="margin-bottom:8px;border-left:4px solid ${color}">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                    <strong>${esc(po.company_name)}</strong>
                    ${po.site_name ? ' — ' + esc(po.site_name) : ''}
                    <span class="status-badge" style="background:${color};color:#fff;margin-left:8px;font-size:10px">${po.status}</span>
                </div>
                <span style="font-size:11px;color:var(--muted)">${po.sent_at ? fmtDateTime(po.sent_at) : ''}</span>
            </div>
            <div style="font-size:12px;color:var(--text2);margin-top:4px">
                ${itemCount} item${itemCount !== 1 ? 's' : ''} · Revenue: $${Number(po.total_sell||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}
                · To: ${(po.recipient_emails||[]).join(', ')}
            </div>
            ${convertBtn}
        </div>`;
    }).join('');
}

async function convertProactiveOffer(offerId) {
    if (!confirm('Convert this proactive offer to a Win? This will create a requisition, quote, and buy plan.')) return;
    if (convertProactiveOffer._busy) return; convertProactiveOffer._busy = true;
    try {
        const result = await apiFetch('/api/proactive/convert/' + offerId, { method: 'POST' });
        showToast('Converted! Requisition #' + result.requisition_id + ' created with buy plan.', 'success');
        loadProactiveSent();
    } catch (e) { showToast('Conversion failed', 'error'); }
    finally { convertProactiveOffer._busy = false; }
}

async function loadProactiveScorecard() {
    try {
        const data = await apiFetch('/api/proactive/scorecard');
        renderProactiveScorecard(data);
    } catch (e) { showToast('Failed to load scorecard', 'error'); }
}

function renderProactiveScorecard(data) {
    const el = document.getElementById('proactiveScorecardPanel');
    if (!el) return;
    if (!data) { el.textContent = 'No scorecard data available'; return; }
    const summaryCards = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px">
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--teal)">${data.total_sent||0}</div>
            <div style="font-size:11px;color:var(--muted)">Total Sent</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--green)">${data.total_converted||0}</div>
            <div style="font-size:11px;color:var(--muted)">Total Converted</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--teal)">${data.total_quoted||0}</div>
            <div style="font-size:11px;color:var(--muted)">Quoted</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--amber)">${data.total_po||0}</div>
            <div style="font-size:11px;color:var(--muted)">PO</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--text)">${data.conversion_rate||0}%</div>
            <div style="font-size:11px;color:var(--muted)">Overall Rate</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--green)" title="$${Number(data.converted_revenue||0).toLocaleString()}">${fmtCurrency(data.converted_revenue)}</div>
            <div style="font-size:11px;color:var(--muted)">Won Revenue</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--green)" title="$${Number(data.gross_profit||0).toLocaleString()}">${fmtCurrency(data.gross_profit)}</div>
            <div style="font-size:11px;color:var(--muted)">Gross Profit</div>
        </div>
    </div>`;

    let breakdownHtml = '';
    if (data.breakdown && data.breakdown.length) {
        breakdownHtml = `
        <h3 style="margin:16px 0 8px;font-size:14px;font-weight:600">Salesperson Scorecard</h3>
        <div style="overflow-x:auto">
        <table class="tbl">
            <thead><tr>
                <th>Salesperson</th>
                <th style="text-align:right">Sent</th>
                <th style="text-align:right">Quoted</th>
                <th style="text-align:right">PO</th>
                <th style="text-align:right">Converted</th>
                <th style="text-align:right">Rate</th>
                <th style="text-align:right" title="Estimated revenue from open quoted deals">Anticipated</th>
                <th style="text-align:right">Won Revenue</th>
                <th style="text-align:right">Gross Profit</th>
            </tr></thead>
            <tbody>${data.breakdown.map((b, i) => {
                const medal = i === 0 ? ' 🥇' : i === 1 ? ' 🥈' : i === 2 ? ' 🥉' : '';
                const rateColor = b.conversion_rate >= 30 ? 'var(--green)' : b.conversion_rate >= 15 ? 'var(--amber)' : 'var(--muted)';
                return `<tr>
                    <td><strong>${esc(b.salesperson_name)}</strong>${medal}</td>
                    <td style="text-align:right">${b.sent}</td>
                    <td style="text-align:right">${b.quoted||0}</td>
                    <td style="text-align:right">${b.po||0}</td>
                    <td style="text-align:right">${b.converted}</td>
                    <td style="text-align:right;color:${rateColor};font-weight:600">${b.conversion_rate}%</td>
                    <td style="text-align:right;color:var(--amber)" title="$${Number(b.anticipated_revenue||0).toLocaleString()}">${fmtCurrency(b.anticipated_revenue)}</td>
                    <td style="text-align:right;color:var(--green)" title="$${Number(b.revenue||0).toLocaleString()}">${fmtCurrency(b.revenue)}</td>
                    <td style="text-align:right;color:var(--green)" title="$${Number(b.gross_profit||0).toLocaleString()}">${fmtCurrency(b.gross_profit)}</td>
                </tr>`;
            }).join('')}</tbody>
        </table>
        <p style="font-size:10px;color:var(--muted);margin-top:8px">Conversion Rate = PO ÷ Sent. Green (≥30%) = strong. Amber (≥15%) = average. Target 30%+ for top performer status.</p>
        </div>`;
    }

    el.innerHTML = summaryCards + breakdownHtml;
}

// ── Performance Tracking ─────────────────────────────────────────────

let _perfVendorSort = 'composite_score';
let _perfVendorOrder = 'desc';
let _perfActiveOnly = true;

function showPerformance() {
    // Redirect to scorecard page (backward compat)
    sidebarNav('scorecard', document.getElementById('navScorecard'));
}

async function loadManagerDigest() {
    const el = document.getElementById('perfDigestPanel');
    if (!el) return;
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const data = await apiFetch('/api/sales/manager-digest');
        let html = '<div style="padding:0 16px">';
        // Summary cards
        const s = data.summary || data;
        html += '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px">';
        const cards = [
            { label: 'Active RFQs', val: s.active_rfqs ?? s.total_active ?? '—', color: 'var(--blue)' },
            { label: 'Offers Today', val: s.offers_today ?? s.new_offers ?? '—', color: 'var(--green)' },
            { label: 'Quotes Sent', val: s.quotes_sent ?? s.total_quotes ?? '—', color: 'var(--purple)' },
            { label: 'Pending Follow-ups', val: s.pending_followups ?? s.follow_ups ?? '—', color: 'var(--amber)' },
            { label: 'Response Rate', val: s.response_rate != null ? Math.round(s.response_rate) + '%' : '—', color: 'var(--teal)' }
        ];
        for (const c of cards) {
            html += `<div class="card-v2" style="padding:16px;min-width:120px;text-align:center">
                <div style="font-size:24px;font-weight:800;color:${c.color}">${c.val}</div>
                <div style="font-size:11px;color:var(--muted);margin-top:4px">${c.label}</div>
            </div>`;
        }
        html += '</div>';
        // Team activity table
        const team = data.team || data.team_activity || [];
        if (team.length) {
            html += '<h3 style="font-size:14px;margin-bottom:8px">Team Activity</h3>';
            html += '<table class="tbl"><thead><tr><th>Name</th><th>RFQs</th><th>Offers</th><th>Quotes</th><th>Response Rate</th><th>Last Active</th></tr></thead><tbody>';
            for (const m of team) {
                html += `<tr>
                    <td><b>${esc(m.name || m.user_name || '')}</b></td>
                    <td class="mono">${m.rfqs ?? m.active_rfqs ?? '—'}</td>
                    <td class="mono">${m.offers ?? m.total_offers ?? '—'}</td>
                    <td class="mono">${m.quotes ?? m.total_quotes ?? '—'}</td>
                    <td class="mono">${m.response_rate != null ? Math.round(m.response_rate) + '%' : '—'}</td>
                    <td style="font-size:11px">${m.last_active ? fmtDateTime(m.last_active) : '—'}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }
        html += '</div>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<p class="empty">Failed to load digest</p>';
    }
}

let _availScoreRole = 'buyer';
async function loadAvailScores(role) {
    if (role) _availScoreRole = role;
    const el = document.getElementById('perfAvailScorePanel');
    if (!el) return;
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const data = await apiFetch(`/api/performance/avail-scores?role=${_availScoreRole}`);
        const entries = data.entries || [];
        const month = data.month || '';
        let html = '<div style="padding:0 16px">';
        // Role toggle
        html += `<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
            <div class="fpills fpills-sm">
                <button type="button" class="fp fp-sm ${_availScoreRole==='buyer'?'on':''}" onclick="loadAvailScores('buyer')">Buyers</button>
                <button type="button" class="fp fp-sm ${_availScoreRole==='sales'?'on':''}" onclick="loadAvailScores('sales')">Sales</button>
            </div>
        </div>`;
        if (!entries.length) {
            html += '<p class="empty">No Avail Score data yet — scores are computed daily</p></div>';
            el.innerHTML = html;
            return;
        }
        html += _renderAvailScoreTable(entries, _availScoreRole, month);
        html += '</div>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<p class="empty">Failed to load Avail Scores</p>';
    }
}

async function loadVendorScorecards(sortBy, order) {
    if (sortBy) _perfVendorSort = sortBy;
    if (order) _perfVendorOrder = order;
    const el = document.getElementById('perfVendorPanel');
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const search = document.getElementById('perfVendorSearch')?.value || '';
        const data = await apiFetch(`/api/performance/vendors?sort_by=${_perfVendorSort}&order=${_perfVendorOrder}&limit=100&search=${encodeURIComponent(search)}`);
        renderVendorScorecards(data);
    } catch (e) {
        el.innerHTML = `<p class="empty">Error loading scorecards</p>`;
    }
}

function renderVendorScorecards(data) {
    const el = document.getElementById('perfVendorPanel');
    const items = data.items || [];
    if (!items.length) {
        el.innerHTML = '<p class="empty">No vendor scorecard data yet — scorecards are computed daily</p>';
        return;
    }

    function sa(col) {
        if (col !== _perfVendorSort) return '<span class="sort-arrow">\u21c5</span>';
        return `<span class="sort-arrow">${_perfVendorOrder === 'asc' ? '\u25b2' : '\u25bc'}</span>`;
    }
    function thC(col) { return col === _perfVendorSort ? ' class="sorted"' : ''; }
    function toggleSort(col) {
        if (_perfVendorSort === col) _perfVendorOrder = _perfVendorOrder === 'desc' ? 'asc' : 'desc';
        else { _perfVendorSort = col; _perfVendorOrder = 'desc'; }
        loadVendorScorecards();
    }

    function metricCell(val, invert) {
        if (val === null || val === undefined) return '<td class="metric-cell na">N/A</td>';
        const score = invert ? 1 - val : val;
        let cls = 'metric-red';
        if (score >= 0.7) cls = 'metric-green';
        else if (score >= 0.4) cls = 'metric-yellow';
        return `<td class="metric-cell ${cls}">${(val * 100).toFixed(0)}%</td>`;
    }

    window._perfToggleSort = toggleSort;

    const searchBar = `<div style="margin:0 16px 10px;display:flex;align-items:center;gap:12px">
        <input type="text" id="perfVendorSearch" placeholder="Search vendors..." value="${document.getElementById('perfVendorSearch')?.value||''}" class="sbox" oninput="_debouncedLoadVendorScorecards()" style="width:300px">
        <label style="font-size:11px;display:flex;align-items:center;gap:4px;color:var(--muted);cursor:pointer;white-space:nowrap"><input type="checkbox" id="perfActiveOnly" ${_perfActiveOnly ? 'checked' : ''} onchange="_perfActiveOnly=this.checked;loadVendorScorecards()"> Active only</label>
    </div>`;

    let html = searchBar + `<div style="overflow-x:auto;padding:0 16px"><table class="tbl">
        <thead><tr>
            <th onclick="window._perfToggleSort('composite_score')"${thC('composite_score')}>Vendor ${sa('composite_score')}</th>
            <th onclick="window._perfToggleSort('response_rate')"${thC('response_rate')}>Response Rate ${sa('response_rate')}</th>
            <th onclick="window._perfToggleSort('quote_conversion')"${thC('quote_conversion')}>Quote Rate ${sa('quote_conversion')}</th>
            <th onclick="window._perfToggleSort('po_conversion')"${thC('po_conversion')}>PO Rate ${sa('po_conversion')}</th>
            <th onclick="window._perfToggleSort('avg_review_rating')"${thC('avg_review_rating')}>Reviews ${sa('avg_review_rating')}</th>
            <th onclick="window._perfToggleSort('composite_score')"${thC('composite_score')}>Score ${sa('composite_score')}</th>
        </tr></thead><tbody>`;

    let filteredItems = items;
    if (_perfActiveOnly) {
        filteredItems = items.filter(v => v.interaction_count > 0);
    }

    for (const v of filteredItems) {
        if (!v.is_sufficient_data) {
            html += `<tr class="cold-start"><td style="display:flex;align-items:center;gap:8px">${window.engRing ? window.engRing(0, 28) : ''}<strong>${esc(v.vendor_name)}</strong></td>${metricCell(v.response_rate)}<td colspan="4" class="metric-cell na" style="text-align:center;font-style:italic">Low data (${v.interaction_count} interactions)</td></tr>`;
            continue;
        }
        const reviewDisplay = v.avg_review_rating !== null && v.avg_review_rating !== undefined
            ? `<td class="metric-cell ${v.avg_review_rating >= 0.7 ? 'metric-green' : v.avg_review_rating >= 0.4 ? 'metric-yellow' : 'metric-red'}">${(v.avg_review_rating * 5).toFixed(1)}/5</td>`
            : '<td class="metric-cell na">N/A</td>';
        const ringScore = v.composite_score != null ? Math.round(v.composite_score * 100) : 0;
        html += `<tr>
            <td style="display:flex;align-items:center;gap:8px">${window.engRing ? window.engRing(ringScore, 28) : ''}<strong>${v.vendor_name}</strong></td>
            ${metricCell(v.response_rate)}
            ${metricCell(v.quote_conversion)}
            ${metricCell(v.po_conversion)}
            ${reviewDisplay}
            ${metricCell(v.composite_score)}
        </tr>`;
    }

    html += '</tbody></table></div>';
    if (window.__isAdmin) {
        html += `<div style="margin:10px 16px 0"><button class="btn btn-ghost btn-sm" onclick="refreshVendorScorecards()">Refresh Scorecards</button></div>`;
    }
    el.innerHTML = html;
}

async function refreshVendorScorecards() {
    try {
        await apiFetch('/api/performance/vendors/refresh', {method:'POST'});
        loadVendorScorecards();
    } catch (e) {
        showToast('Error refreshing: ' + (e.message || e), 'error');
    }
}

// ── Buyer Leaderboard ──

let _leaderboardMonth = '';

async function loadBuyerLeaderboard(month) {
    const el = document.getElementById('perfBuyerPanel');
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const monthsData = await apiFetch('/api/performance/buyers/months');
        const months = monthsData.months || [];
        if (!month) {
            _leaderboardMonth = months.length ? months[0] : new Date().toISOString().slice(0,7);
        } else {
            _leaderboardMonth = month;
        }
        const data = await apiFetch(`/api/performance/buyers?month=${_leaderboardMonth.slice(0,7)}`);
        renderBuyerLeaderboard(data, months);
    } catch (e) {
        el.innerHTML = '<p class="empty">No leaderboard data yet — computed daily</p>';
    }
}

function renderBuyerLeaderboard(data, months) {
    const el = document.getElementById('perfBuyerPanel');
    const entries = data.entries || [];

    let monthSelector = `<select class="tb-select" onchange="loadBuyerLeaderboard(this.value)">`;
    for (const m of months) {
        const label = new Date(m + '-15').toLocaleDateString('en-US', {month:'long', year:'numeric'});
        monthSelector += `<option value="${m}" ${m === _leaderboardMonth ? 'selected' : ''}>${label}</option>`;
    }
    monthSelector += '</select>';

    const totalPts = entries.reduce((s, e) => s + e.total_points, 0);
    const topScorer = entries.length ? entries[0].user_name : '\u2014';
    const totalOffers = entries.reduce((s, e) => s + e.offers_logged, 0);
    const ytdTotalPts = entries.reduce((s, e) => s + (e.ytd_total_points || 0), 0);

    let html = `<div style="display:flex;align-items:center;gap:10px;margin:0 16px 12px;flex-wrap:wrap">
        <div>${monthSelector}</div>
        ${window.__isAdmin ? '<button class="tb-btn" onclick="refreshBuyerLeaderboard()">Refresh</button>' : ''}
    </div>`;

    html += `<div class="perf-summary" style="padding:0 16px">
        <div class="perf-card"><div class="perf-card-num">${totalOffers}</div><div class="perf-card-label">Offers Logged</div></div>
        <div class="perf-card"><div class="perf-card-num">${totalPts}</div><div class="perf-card-label">Monthly Points</div></div>
        <div class="perf-card"><div class="perf-card-num">${topScorer}</div><div class="perf-card-label">Top Scorer</div></div>
        <div class="perf-card"><div class="perf-card-num">${ytdTotalPts}</div><div class="perf-card-label">YTD Points</div></div>
    </div>`;

    if (!entries.length) {
        html += '<p class="empty">No data for this month</p>';
        el.innerHTML = html;
        return;
    }

    const currentEmail = (window.__userEmail || '').toLowerCase();

    html += `<div style="overflow-x:auto;padding:0 16px"><table class="tbl"><thead><tr>
        <th>#</th><th>Buyer</th>
        <th>Offers (x1)</th><th>Quoted (x3)</th><th>Buy Plan (x5)</th><th>PO Confirmed (x8)</th><th>Inventory Lists (x2)</th>
        <th>Total</th>
        <th style="border-left:2px solid var(--border)">YTD Offers</th><th>YTD PO Conf.</th><th>YTD Points</th>
    </tr></thead><tbody>`;

    for (const e of entries) {
        const isMe = e.user_id && e.user_id === window.userId;
        let rowCls = '';
        if (e.rank === 1) rowCls = 'sc-gold';
        else if (e.rank === 2) rowCls = 'sc-silver';
        else if (isMe) rowCls = 'lb-highlight';
        const medal = e.rank === 1 ? ' \ud83e\udd47' : e.rank === 2 ? ' \ud83e\udd48' : e.rank === 3 ? ' \ud83e\udd49' : '';
        html += `<tr class="${rowCls}">
            <td><strong>${e.rank}${medal}</strong></td>
            <td>${e.user_name || 'Unknown'}</td>
            <td>${e.offers_logged} <span class="pts">(${e.points_offers})</span></td>
            <td>${e.offers_quoted} <span class="pts">(${e.points_quoted})</span></td>
            <td>${e.offers_in_buyplan} <span class="pts">(${e.points_buyplan})</span></td>
            <td>${e.offers_po_confirmed} <span class="pts">(${e.points_po})</span></td>
            <td>${e.stock_lists_uploaded || 0} <span class="pts">(${e.points_stock || 0})</span></td>
            <td><strong>${e.total_points}</strong></td>
            <td style="border-left:2px solid var(--border)">${e.ytd_offers_logged || 0}</td>
            <td>${e.ytd_offers_po_confirmed || 0}</td>
            <td><strong>${e.ytd_total_points || 0}</strong></td>
        </tr>`;
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
}

async function refreshBuyerLeaderboard() {
    try {
        await apiFetch('/api/performance/buyers/refresh', {method:'POST'});
        loadBuyerLeaderboard(_leaderboardMonth);
    } catch (e) {
        showToast('Error refreshing: ' + (e.message || e), 'error');
    }
}

// ── Salesperson Scorecard ────────────────────────────────────────────

let _salesScorecardMonth = null;
let _salesScorecardData = null;
let _salesSortCol = 'won_revenue';
let _salesSortDir = 'desc';

async function loadSalespersonScorecard(month) {
    const el = document.getElementById('perfSalesPanel');
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        if (!month) {
            _salesScorecardMonth = new Date().toISOString().slice(0,7);
        } else {
            _salesScorecardMonth = month;
        }
        const data = await apiFetch(`/api/performance/salespeople?month=${_salesScorecardMonth}`);
        _salesScorecardData = data;
        renderSalespersonScorecard(data);
    } catch (e) {
        el.innerHTML = '<p class="empty">No scorecard data available</p>';
    }
}

function _sortSalesEntries(entries, col, dir) {
    return entries.slice().sort((a, b) => {
        let av, bv;
        if (col.startsWith('ytd_')) {
            const k = col.slice(4);
            av = a.ytd[k] ?? 0;
            bv = b.ytd[k] ?? 0;
        } else {
            av = a.monthly[col] ?? 0;
            bv = b.monthly[col] ?? 0;
        }
        return dir === 'desc' ? bv - av : av - bv;
    });
}

function sortSalesScorecard(col) {
    if (_salesSortCol === col) {
        _salesSortDir = _salesSortDir === 'desc' ? 'asc' : 'desc';
    } else {
        _salesSortCol = col;
        _salesSortDir = 'desc';
    }
    if (_salesScorecardData) renderSalespersonScorecard(_salesScorecardData);
}

function renderSalespersonScorecard(data) {
    const el = document.getElementById('perfSalesPanel');
    const entries = data.entries || [];

    const now = new Date();
    let monthSelector = `<select class="tb-select" onchange="loadSalespersonScorecard(this.value)">`;
    for (let i = 0; i < 12; i++) {
        const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
        const val = d.toISOString().slice(0,7);
        const label = d.toLocaleDateString('en-US', {month:'long', year:'numeric'});
        monthSelector += `<option value="${val}" ${val === _salesScorecardMonth ? 'selected' : ''}>${label}</option>`;
    }
    monthSelector += '</select>';

    const totalRev = entries.reduce((s, e) => s + (e.monthly.won_revenue || 0), 0);
    const totalOrders = entries.reduce((s, e) => s + (e.monthly.orders_won || 0), 0);
    const totalQuotes = entries.reduce((s, e) => s + (e.monthly.quotes_sent || 0), 0);
    const ytdRev = entries.reduce((s, e) => s + (e.ytd.won_revenue || 0), 0);

    let html = `<div style="display:flex;align-items:center;gap:10px;margin:0 16px 12px;flex-wrap:wrap">
        <div>${monthSelector}</div>
    </div>`;

    html += `<div class="perf-summary" style="padding:0 16px">
        <div class="perf-card"><div class="perf-card-num">$${totalRev.toLocaleString()}</div><div class="perf-card-label">Monthly Revenue</div></div>
        <div class="perf-card"><div class="perf-card-num">${totalOrders}</div><div class="perf-card-label">Orders Won</div></div>
        <div class="perf-card"><div class="perf-card-num">${totalQuotes}</div><div class="perf-card-label">Quotes Sent</div></div>
        <div class="perf-card"><div class="perf-card-num">$${ytdRev.toLocaleString()}</div><div class="perf-card-label">YTD Revenue</div></div>
    </div>`;

    if (!entries.length) {
        html += '<p class="empty">No data for this month</p>';
        el.innerHTML = html;
        return;
    }

    const sorted = _sortSalesEntries(entries, _salesSortCol, _salesSortDir);

    const byRev = entries.slice().sort((a, b) => (b.monthly.won_revenue || 0) - (a.monthly.won_revenue || 0));
    const gold_id = byRev[0] && byRev[0].monthly.won_revenue > 0 ? byRev[0].user_id : null;
    const silver_id = byRev[1] && byRev[1].monthly.won_revenue > 0 ? byRev[1].user_id : null;

    const cols = [
        {key:'new_accounts', label:'Accounts'},
        {key:'new_contacts', label:'Contacts'},
        {key:'calls_made', label:'Calls'},
        {key:'emails_sent', label:'Emails/RFQs'},
        {key:'requisitions_entered', label:'Reqs'},
        {key:'quotes_sent', label:'Quotes Sent'},
        {key:'orders_won', label:'Orders Won'},
        {key:'won_revenue', label:'Revenue', fmt:'$'},
        {key:'proactive_sent', label:'Proactive Sent'},
        {key:'proactive_converted', label:'Proactive Conv.'},
        {key:'proactive_revenue', label:'Proactive Rev.', fmt:'$'},
        {key:'boms_uploaded', label:'Excess Lists'},
    ];

    const ytdCols = [
        {key:'orders_won', label:'YTD Orders'},
        {key:'won_revenue', label:'YTD Revenue', fmt:'$'},
        {key:'proactive_revenue', label:'YTD Proactive Rev.', fmt:'$'},
    ];

    function sa(key) {
        if (_salesSortCol !== key) return '<span class="sort-arrow">\u21c5</span>';
        return `<span class="sort-arrow">${_salesSortDir === 'asc' ? '\u25b2' : '\u25bc'}</span>`;
    }
    function thC(key) { return _salesSortCol === key ? ' class="sorted"' : ''; }

    html += `<div style="overflow-x:auto;padding:0 16px"><table class="tbl"><thead><tr>
        <th>#</th><th>Salesperson</th>`;
    for (const c of cols) {
        html += `<th${thC(c.key)} onclick="sortSalesScorecard('${c.key}')">${c.label} ${sa(c.key)}</th>`;
    }
    for (const c of ytdCols) {
        html += `<th style="border-left:2px solid var(--border)"${thC('ytd_'+c.key)} onclick="sortSalesScorecard('ytd_${c.key}')">${c.label} ${sa('ytd_'+c.key)}</th>`;
    }
    html += '</tr></thead><tbody>';

    for (let i = 0; i < sorted.length; i++) {
        const e = sorted[i];
        const rank = i + 1;
        let rowCls = '';
        let medal = '';
        if (e.user_id === gold_id) { rowCls = 'class="sc-gold"'; medal = ' \ud83e\udd47'; }
        else if (e.user_id === silver_id) { rowCls = 'class="sc-silver"'; medal = ' \ud83e\udd48'; }

        html += `<tr ${rowCls}><td><strong>${rank}${medal}</strong></td><td>${e.user_name || 'Unknown'}</td>`;
        for (const c of cols) {
            const v = e.monthly[c.key] ?? 0;
            html += `<td>${c.fmt === '$' ? '$' + Number(v).toLocaleString() : v}</td>`;
        }
        for (const c of ytdCols) {
            const v = e.ytd[c.key] ?? 0;
            html += `<td style="border-left:2px solid var(--border)">${c.fmt === '$' ? '$' + Number(v).toLocaleString() : v}</td>`;
        }
        html += '</tr>';
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
}

function openSettingsTab(panel) {
    showView('view-settings');
    document.querySelectorAll('.sidebar-nav button').forEach(b => b.classList.remove('active'));
    const navBtn = document.getElementById('navSettings');
    if (navBtn) navBtn.classList.add('active');
    switchSettingsTab(panel || safeGet('settings_active_tab', 'profile'));
}

function switchSettingsTab(name, btn) {
    safeSet('settings_active_tab', name);
    document.querySelectorAll('.settings-panel').forEach(p => { p.classList.add('hidden'); p.style.display = 'none'; });
    document.querySelectorAll('#settingsTabs .tab').forEach(t => t.classList.remove('on'));
    const target = document.getElementById('settings-' + name);
    if (target) { target.classList.remove('hidden'); target.style.display = ''; }
    if (btn) btn.classList.add('on');
    else {
        const tabBtn = document.querySelector(`#settingsTabs .tab[onclick*="${name}"]`);
        if (tabBtn) tabBtn.classList.add('on');
    }
    // Lazy-load data
    if (name === 'profile') loadSettingsProfile();
    else if (name === 'users') loadAdminUsers();
    else if (name === 'health') loadSettingsHealth();
    else if (name === 'config') loadSettingsConfig();
    else if (name === 'sources') loadSettingsSources();
    else if (name === 'teams') loadTeamsConfig();
    else if (name === 'enrichment') { loadEnrichmentQueue(); loadEnrichmentStats(); loadCreditUsage(); }
    else if (name === 'tickets') { if (typeof window.showTickets === 'function') window.showTickets(); }
    else if (name === 'apihealth') loadApiHealthDashboard();
    else if (name === 'transfer') loadTransferPanel();
}

// Keep backward compat for dropdown links
function showSettings(panel) { openSettingsTab(panel); }

// ── My Profile tab ──────────────────────────────────────────────────
function loadSettingsProfile() {
    const container = document.getElementById('settings-profile');
    if (!container) return;
    container.textContent = '';

    const card = document.createElement('div');
    card.className = 'card s-card';
    card.style.maxWidth = '500px';
    card.style.padding = '24px';

    const heading = document.createElement('h3');
    heading.textContent = 'My Profile';
    heading.style.marginBottom = '16px';
    card.appendChild(heading);

    const fields = [
        { label: 'Name', value: window.__userName || window.userName || '—' },
        { label: 'Email', value: window.__userEmail || window.userEmail || '—' },
        { label: 'Role', value: (window.userRole || '—').charAt(0).toUpperCase() + (window.userRole || '—').slice(1) },
    ];

    fields.forEach(f => {
        const row = document.createElement('div');
        row.style.cssText = 'margin-bottom:12px;';

        const lbl = document.createElement('label');
        lbl.style.cssText = 'display:block;font-size:11px;font-weight:600;color:var(--muted);margin-bottom:4px;';
        lbl.textContent = f.label;
        row.appendChild(lbl);

        const val = document.createElement('div');
        val.style.cssText = 'padding:8px 12px;background:var(--bg-alt);border:1px solid var(--border);border-radius:6px;font-size:13px;color:var(--fg);';
        val.textContent = f.value;
        row.appendChild(val);

        card.appendChild(row);
    });

    const note = document.createElement('p');
    note.style.cssText = 'font-size:11px;color:var(--muted);margin-top:16px;';
    note.textContent = 'Profile is managed via Azure AD.';
    card.appendChild(note);

    container.appendChild(card);
}

let _sourcesData = [];
let _sourcesFilter = 'all';
let _sourcesQuery = '';
let _sourcesSearchTimer = null;
let _sourcesShowPlanned = false;

function _isPlannedSource(s) {
    // Use is_active flag from API if available, fall back to env_vars check
    if (typeof s.is_active === 'boolean') return !s.is_active;
    return !(s.env_vars && s.env_vars.length);
}

function _statusBadge(status, isPlanned) {
    if (isPlanned) return '<span class="s-badge s-badge-planned">Planned</span>';
    const cls = status === 'live' ? 's-badge-live' : status === 'error' ? 's-badge-error' : status === 'disabled' ? 's-badge-disabled' : 's-badge-pending';
    const label = status === 'live' ? 'Live' : status === 'error' ? 'Error' : status === 'disabled' ? 'Disabled' : 'Pending';
    return `<span class="s-badge ${cls}">${label}</span>`;
}

function _renderSourceCards() {
    const container = document.getElementById('sourcesCardsContainer');
    if (!container) return;

    // Split configurable vs planned
    let configurable = _sourcesData.filter(s => !_isPlannedSource(s));
    let planned = _sourcesData.filter(s => _isPlannedSource(s));

    // Apply status filter (only to configurable)
    let filtered = configurable;
    if (_sourcesFilter !== 'all') {
        filtered = filtered.filter(s => s.status === _sourcesFilter);
    }
    // Apply search across both lists
    if (_sourcesQuery) {
        const q = _sourcesQuery.toLowerCase();
        const matchFn = s =>
            (s.display_name || '').toLowerCase().includes(q) ||
            (s.description || '').toLowerCase().includes(q) ||
            (s.source_type || '').toLowerCase().includes(q);
        filtered = filtered.filter(matchFn);
        planned = planned.filter(matchFn);
    }

    if (!filtered.length && !planned.length) {
        container.innerHTML = '<p class="empty">No matching sources</p>';
        return;
    }

    const categoryOrder = ['api', 'platform', 'enrichment', 'email', 'scraper', 'manual'];
    const categoryLabels = {
        api: 'Part Search APIs',
        platform: 'Platform Services',
        enrichment: 'Enrichment APIs',
        email: 'Email Intelligence',
        scraper: 'Web Scrapers',
        manual: 'Manual Import',
    };

    const canToggle = window.__isAdmin;

    // --- Render configurable sources ---
    const grouped = {};
    for (const s of filtered) {
        const cat = s.category || 'api';
        if (!grouped[cat]) grouped[cat] = [];
        grouped[cat].push(s);
    }
    const order = {live: 0, pending: 1, error: 2, disabled: 3};
    for (const cat of Object.keys(grouped)) {
        grouped[cat].sort((a, b) => (order[a.status] || 9) - (order[b.status] || 9));
    }

    let html = '';
    for (const cat of categoryOrder) {
        const group = grouped[cat];
        if (!group || !group.length) continue;
        html += `<h3 class="s-cat-heading">${categoryLabels[cat] || cat}</h3>`;
        for (const s of group) html += _renderSourceCard(s, canToggle, false);
    }

    // --- Render planned sources (collapsible) ---
    if (planned.length && (!_sourcesQuery || planned.length)) {
        const plannedGrouped = {};
        for (const s of planned) {
            const cat = s.category || 'api';
            if (!plannedGrouped[cat]) plannedGrouped[cat] = [];
            plannedGrouped[cat].push(s);
        }
        html += `<div style="margin-top:24px;border-top:2px solid var(--border);padding-top:16px">
            <div style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:12px" onclick="togglePlannedSources()">
                <h3 style="margin:0;font-size:14px;color:var(--text2)">Planned / Coming Soon</h3>
                <span class="s-badge s-badge-planned">${planned.length}</span>
                <span id="plannedArrow" style="font-size:12px;color:var(--muted);transition:transform .2s">${_sourcesShowPlanned ? '▼' : '▶'}</span>
            </div>
            <div id="plannedSourcesContainer" style="display:${_sourcesShowPlanned ? 'block' : 'none'}">`;
        for (const cat of categoryOrder) {
            const group = plannedGrouped[cat];
            if (!group || !group.length) continue;
            html += `<h3 class="s-cat-heading" style="opacity:.7">${categoryLabels[cat] || cat}</h3>`;
            for (const s of group) html += _renderSourceCard(s, false, true);
        }
        html += '</div></div>';
    }

    container.innerHTML = html;
}

function _renderSourceCard(s, canToggle, isPlanned) {
    const envVars = s.env_vars || [];
    const envStatus = s.env_status || {};
    const credMasked = s.credentials_masked || {};

    let credsHtml = '';
    if (envVars.length && !isPlanned) {
        for (const v of envVars) {
            const isSet = envStatus[v];
            const masked = credMasked[v] || '';
            let badge;
            if (isSet && masked) {
                badge = `<span class="s-cred-masked">${masked}</span>`;
            } else if (isSet) {
                badge = '<span style="color:var(--teal);font-size:11px;font-weight:600">Configured</span>';
            } else {
                badge = '<span style="color:#d97706;font-size:11px">Not configured</span>';
            }
            credsHtml += `
                <div class="s-cred-row" id="cred-row-${s.id}-${v}">
                    <code>${v}</code>
                    <span id="cred-status-${s.id}-${v}">${badge}</span>
                    <div style="flex:1"></div>
                    ${isSet ? `<button class="btn btn-ghost btn-sm" style="color:var(--red);font-size:10px" onclick="deleteCredential(${s.id},'${v}')" title="Remove credential">Remove</button>` : ''}
                    <button class="btn btn-ghost btn-sm" onclick="editCredential(${s.id},'${v}')">${isSet ? 'Update' : 'Set'}</button>
                </div>
                <div id="cred-edit-${s.id}-${v}" style="display:none;padding:6px 0 10px">
                    <div class="s-row">
                        <input type="password" id="cred-input-${s.id}-${v}" placeholder="Enter value..." class="s-input" style="flex:1"
                               onkeydown="if(event.key==='Enter')saveCredential(${s.id},'${v}')">
                        <button class="btn btn-primary btn-sm" onclick="saveCredential(${s.id},'${v}')">Save</button>
                        <button class="btn btn-ghost btn-sm" onclick="cancelCredEdit(${s.id},'${v}')">Cancel</button>
                    </div>
                </div>`;
        }
    }

    let statsHtml = '';
    if (s.total_searches) {
        statsHtml = `<div class="s-stats-row">
            <span>${s.total_searches.toLocaleString()} searches</span>
            <span>${(s.total_results || 0).toLocaleString()} results</span>
            <span>${s.avg_response_ms || 0}ms avg</span>
            ${s.last_success ? `<span>Last: ${new Date(s.last_success).toLocaleDateString()}</span>` : ''}
        </div>`;
    }
    // Health monitoring metadata
    let healthHtml = '';
    const healthParts = [];
    if (s.last_ping_at) healthParts.push('Checked: ' + _timeAgo(s.last_ping_at));
    if (s.error_count_24h > 0) healthParts.push('<span style="color:var(--red)">' + s.error_count_24h + ' errors (24h)</span>');
    if (s.monthly_quota) healthParts.push(Number(s.calls_this_month || 0) + '/' + Number(s.monthly_quota) + ' calls');
    if (healthParts.length) {
        healthHtml = '<div style="font-size:11px;color:var(--muted);margin-top:4px">' + healthParts.join(' · ') + '</div>';
    }

    const errorHtml = s.last_error
        ? `<div class="s-test-result s-test-err" style="margin-top:6px">Last error: ${s.last_error}</div>`
        : '';

    const toggleHtml = canToggle && envVars.length && !isPlanned
        ? `<button class="btn btn-ghost btn-sm" onclick="toggleSourceStatus(${s.id},'${s.status}')"
                  style="${s.status === 'disabled' ? 'opacity:0.7' : ''}">${s.status === 'disabled' ? 'Enable' : 'Disable'}</button>`
        : '';

    const testHtml = !isPlanned && (envVars.length || s.status === 'live')
        ? `<button class="btn btn-ghost btn-sm" id="test-btn-${s.id}" onclick="testSourceCred(${s.id})">Test</button>`
        : '';

    const activeToggleHtml = canToggle
        ? `<button class="btn btn-ghost btn-sm" onclick="toggleSourceActive(${s.id})" style="font-size:10px;color:${s.is_active?'var(--green)':'var(--amber)'}">${s.is_active ? '● Active' : '○ Planned'}</button>`
        : '';

    const cardCls = isPlanned ? 'card s-card s-card-planned' : 'card s-card';

    return `<div class="${cardCls}" style="max-width:none">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <div style="display:flex;align-items:center;gap:10px">
                <strong style="font-size:14px">${s.display_name}</strong>
                ${_statusBadge(s.status, isPlanned)}
                <span class="s-hint">${s.source_type}</span>
            </div>
            <div class="s-row" style="gap:8px">
                ${activeToggleHtml}
                ${toggleHtml}
                ${testHtml}
            </div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:10px">${s.description || ''}</div>
        ${!isPlanned && s.setup_notes ? '<div class="s-hint" style="margin-bottom:8px;padding:6px 10px;background:var(--bg);border-radius:4px">' + s.setup_notes + '</div>' : ''}
        ${isPlanned && s.setup_notes ? '<div class="s-hint" style="margin-bottom:4px">' + s.setup_notes + '</div>' : ''}
        ${s.signup_url ? '<a href="' + s.signup_url + '" target="_blank" style="font-size:11px;color:var(--teal);text-decoration:none">' + (isPlanned ? 'More info' : 'Get API credentials') + ' ↗</a>' : ''}
        ${credsHtml ? '<div style="margin-top:10px">' + credsHtml + '</div>' : ''}
        <div id="test-result-${s.id}"></div>
        ${statsHtml}${healthHtml}${errorHtml}
    </div>`;
}

function togglePlannedSources() {
    _sourcesShowPlanned = !_sourcesShowPlanned;
    const container = document.getElementById('plannedSourcesContainer');
    const arrow = document.getElementById('plannedArrow');
    if (container) container.style.display = _sourcesShowPlanned ? 'block' : 'none';
    if (arrow) arrow.textContent = _sourcesShowPlanned ? '▼' : '▶';
}

async function loadSettingsSources() {
    const el = document.getElementById('settingsSourcesList');
    if (!el) return;
    el.innerHTML = '<p class="empty">Loading data sources...</p>';
    try {
        const res = await apiFetch('/api/sources');
        const sources = res.sources || [];
        if (!sources.length) { el.innerHTML = '<p class="empty">No data sources configured</p>'; return; }

        _sourcesData = sources;
        // Preserve filter unless first load
        if (!el.querySelector('#sourcesCardsContainer')) {
            _sourcesFilter = 'all';
            _sourcesQuery = '';
        }

        // Compute summary counts (only configurable sources)
        const configurable = sources.filter(s => !_isPlannedSource(s));
        const counts = {live: 0, pending: 0, error: 0, disabled: 0};
        for (const s of configurable) counts[s.status] = (counts[s.status] || 0) + 1;
        const total = configurable.length;
        const planned = sources.length - total;

        el.innerHTML = `
            <div style="margin-bottom:16px;padding:12px 16px;background:var(--bg);border-radius:8px;border:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--text2)">
                <span class="src-status-pill${_sourcesFilter === 'all' ? ' on' : ''}" onclick="setSourcesFilter('all')" style="cursor:pointer;font-weight:600">${total} Configurable</span>
                <span class="src-status-pill${_sourcesFilter === 'live' ? ' on' : ''}" onclick="setSourcesFilter('live')" style="cursor:pointer">${counts.live} Live</span>
                <span class="src-status-pill${_sourcesFilter === 'pending' ? ' on' : ''}" onclick="setSourcesFilter('pending')" style="cursor:pointer">${counts.pending} Pending</span>
                ${counts.error ? `<span class="src-status-pill${_sourcesFilter === 'error' ? ' on' : ''}" onclick="setSourcesFilter('error')" style="cursor:pointer;color:var(--red)">${counts.error} Error</span>` : ''}
                ${counts.disabled ? `<span class="src-status-pill${_sourcesFilter === 'disabled' ? ' on' : ''}" onclick="setSourcesFilter('disabled')" style="cursor:pointer">${counts.disabled} Disabled</span>` : ''}
                <span style="color:var(--muted);font-size:11px">${planned} planned</span>
                <input class="req-search" id="sourcesSearchInput" type="text" placeholder="Search sources…"
                       style="flex:1;min-width:160px;margin-left:auto" oninput="onSourcesSearch(this.value)" value="${_sourcesQuery}" aria-label="Search sources">
            </div>
            <div id="sourcesCardsContainer"></div>`;

        _renderSourceCards();
    } catch (e) {
        el.innerHTML = '<p class="empty">Failed to load sources</p>';
    }
}

function setSourcesFilter(mode) {
    _sourcesFilter = mode;
    document.querySelectorAll('.src-status-pill').forEach(p => p.classList.remove('on'));
    const clicked = event && event.target.closest('.src-status-pill');
    if (clicked) clicked.classList.add('on');
    _renderSourceCards();
}

function onSourcesSearch(val) {
    clearTimeout(_sourcesSearchTimer);
    _sourcesSearchTimer = setTimeout(() => {
        _sourcesQuery = val.trim();
        _renderSourceCards();
    }, 200);
}

function editCredential(sourceId, varName) {
    const editEl = document.getElementById(`cred-edit-${sourceId}-${varName}`); if (editEl) editEl.style.display = '';
    const input = document.getElementById(`cred-input-${sourceId}-${varName}`);
    if (input) { input.value = ''; input.focus(); }
}

function cancelCredEdit(sourceId, varName) {
    const editEl = document.getElementById(`cred-edit-${sourceId}-${varName}`); if (editEl) editEl.style.display = 'none';
}

async function saveCredential(sourceId, varName) {
    const input = document.getElementById(`cred-input-${sourceId}-${varName}`);
    const value = input.value.trim();
    if (!value) { showToast('Please enter a value', 'error'); return; }
    try {
        const body = {};
        body[varName] = value;
        await apiFetch(`/api/admin/sources/${sourceId}/credentials`, {
            method: 'PUT',
            body: body,
        });
        showToast('Credential saved', 'success');
        cancelCredEdit(sourceId, varName);
        loadSettingsSources();
    } catch (e) {
        showToast('Failed to save credential: ' + (e.message || e), 'error');
    }
}

async function deleteCredential(sourceId, varName) {
    if (!confirm(`Remove ${varName}? The source may stop working.`)) return;
    try {
        await apiFetch(`/api/admin/sources/${sourceId}/credentials/${varName}`, {
            method: 'DELETE',
        });
        showToast('Credential removed', 'success');
        loadSettingsSources();
    } catch (e) {
        showToast('Failed to remove credential: ' + (e.message || e), 'error');
    }
}

async function testSourceCred(sourceId) {
    const btn = document.getElementById(`test-btn-${sourceId}`);
    const resultEl = document.getElementById(`test-result-${sourceId}`);
    if (!btn || !resultEl) return;
    btn.disabled = true;
    btn.textContent = 'Testing...';
    resultEl.innerHTML = '<div class="s-test-result" style="background:var(--bg);color:var(--muted);border:1px solid var(--border)">Running connection test...</div>';
    try {
        const data = await apiFetch(`/api/sources/${sourceId}/test`, {method: 'POST'});
        if (data.status === 'ok') {
            resultEl.innerHTML = `<div class="s-test-result s-test-ok">Test passed — ${data.results_count} result(s) in ${data.elapsed_ms}ms</div>`;
        } else if (data.status === 'no_results') {
            resultEl.innerHTML = '<div class="s-test-result s-test-warn">Connected successfully, but no results for test MPN (LM358N)</div>';
        } else {
            resultEl.innerHTML = `<div class="s-test-result s-test-err">Test failed: ${data.error || 'Unknown error'}</div>`;
        }
        // Update local data without full rebuild (preserves test result)
        const src = _sourcesData.find(s => s.id === sourceId);
        if (src) {
            src.status = data.status === 'ok' ? 'live' : data.status === 'no_results' ? 'live' : 'error';
            if (data.error) src.last_error = data.error;
            else src.last_error = null;
        }
    } catch (e) {
        resultEl.innerHTML = `<div class="s-test-result s-test-err">Test error: ${e.message || e}</div>`;
    }
    btn.disabled = false;
    btn.textContent = 'Test';
}

async function toggleSourceStatus(sourceId, currentStatus) {
    const newStatus = currentStatus === 'disabled' ? 'live' : 'disabled';
    try {
        await apiFetch(`/api/sources/${sourceId}/toggle`, {
            method: 'PUT',
            body: {status: newStatus},
        });
        showToast(`Source ${newStatus === 'disabled' ? 'disabled' : 'enabled'}`, 'success');
        loadSettingsSources();
    } catch (e) {
        showToast('Failed to toggle source: ' + (e.message || e), 'error');
    }
}

async function toggleSourceActive(sourceId) {
    try {
        const data = await apiFetch(`/api/sources/${sourceId}/activate`, { method: 'PUT' });
        showToast(`Source marked ${data.is_active ? 'Active' : 'Planned'}`, 'success');
        loadSettingsSources();
    } catch (e) {
        showToast('Failed to toggle active state: ' + (e.message || e), 'error');
    }
}

// ── System Health ──

async function loadSettingsHealth() {
    const el = document.getElementById('settingsHealthContent');
    if (!el) return;
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const data = await apiFetch('/api/admin/health');
        let html = '';

        // Version
        html += `<div class="card s-card"><strong>Version:</strong> ${data.version}</div>`;

        // DB stats
        html += '<div class="card s-card" style="max-width:none"><h3>Database Statistics</h3>';
        html += '<table class="tbl"><thead><tr><th>Table</th><th>Rows</th></tr></thead><tbody>';
        for (const [k, v] of Object.entries(data.db_stats || {})) {
            html += `<tr><td>${k}</td><td>${v.toLocaleString()}</td></tr>`;
        }
        html += '</tbody></table></div>';

        // Scheduler status
        html += '<div class="card s-card" style="max-width:none"><h3>M365 Scheduler Status</h3>';
        html += '<table class="tbl"><thead><tr><th>User</th><th>M365</th><th>Token</th><th>Last Inbox Scan</th></tr></thead><tbody>';
        for (const u of data.scheduler || []) {
            const dot = u.m365_connected ? '<span style="color:var(--teal)">Connected</span>' : '<span style="color:var(--muted)">Disconnected</span>';
            const scan = u.last_inbox_scan ? new Date(u.last_inbox_scan).toLocaleString() : '—';
            html += `<tr><td>${u.email}</td><td>${dot}</td><td>${u.has_refresh_token ? 'Yes' : 'No'}</td><td>${scan}</td></tr>`;
        }
        html += '</tbody></table></div>';

        // Connector health
        html += '<div class="card s-card" style="max-width:none"><h3>Connector Health</h3>';
        html += '<table class="tbl"><thead><tr><th>Name</th><th>Status</th><th>Searches</th><th>Results</th><th>Last Success</th></tr></thead><tbody>';
        for (const c of data.connectors || []) {
            const dot = c.status === 'live' ? '🟢' : c.status === 'error' ? '🔴' : '🟡';
            const last = c.last_success ? new Date(c.last_success).toLocaleString() : '—';
            html += `<tr><td>${c.display_name}</td><td>${dot} ${c.status}</td><td>${c.total_searches}</td><td>${c.total_results}</td><td>${last}</td></tr>`;
        }
        html += '</tbody></table></div>';

        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<p class="empty">Error loading health data</p>';
    }
}


// ── Scoring Weights ──



// ── Configuration ──

async function loadSettingsConfig() {
    const el = document.getElementById('settingsConfigContent');
    if (!el) return;
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const configs = await apiFetch('/api/admin/config');
        const nonWeights = configs.filter(c => !c.key.startsWith('weight_'));
        let html = '<div class="card s-card">';
        html += '<h3>System Configuration</h3>';
        html += '<div class="s-form">';
        for (const c of nonWeights) {
            const isBool = c.value === 'true' || c.value === 'false';
            if (isBool) {
                const checked = c.value === 'true' ? 'checked' : '';
                html += `<div class="s-row">
                    <label style="flex:1;font-size:13px">${c.key.replace(/_/g, ' ')}<br><span class="s-hint">${c.description || ''}</span></label>
                    <label style="display:flex;align-items:center;gap:4px;cursor:pointer">
                        <input type="checkbox" ${checked} onchange="saveConfig('${c.key}', this.checked ? 'true' : 'false')">
                        <span style="font-size:12px">${c.value === 'true' ? 'On' : 'Off'}</span>
                    </label>
                </div>`;
            } else {
                html += `<div class="s-row">
                    <label style="flex:1;font-size:13px">${c.key.replace(/_/g, ' ')}<br><span class="s-hint">${c.description || ''}</span></label>
                    <input type="text" value="${c.value}" id="cfg_${c.key}" class="s-input-num">
                    <button class="btn btn-ghost btn-sm" onclick="saveConfig('${c.key}', document.getElementById('cfg_${c.key}').value)">Save</button>
                </div>`;
            }
        }
        html += '</div>';
        if (nonWeights.length) {
            const lastUpdate = nonWeights.find(c => c.updated_by);
            if (lastUpdate) html += `<p class="s-hint" style="margin-top:12px">Last updated by ${lastUpdate.updated_by}</p>`;
        }
        html += '</div>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<p class="empty">Error loading configuration</p>';
    }
}

async function saveConfig(key, value) {
    try {
        await apiFetch(`/api/admin/config/${key}`, {
            method: 'PUT',
            body: {value: String(value)}
        });
        loadSettingsConfig();
    } catch (e) {
        showToast('Error saving: ' + (e.message || e), 'error');
    }
}


// ── Manage Users ──

let _adminUsers = [];

async function loadAdminUsers() {
    const el = document.getElementById('adminUsersList');
    if (!el) return;
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        _adminUsers = await apiFetch('/api/admin/users');
        renderAdminUsers();
    } catch (e) {
        el.innerHTML = '<p class="empty">Error loading users</p>';
    }
}

function renderAdminUsers() {
    const el = document.getElementById('adminUsersList');
    if (!_adminUsers.length) { el.innerHTML = '<p class="empty">No users</p>'; return; }
    let html = `<table class="tbl"><thead><tr>
        <th>Name</th><th>Email</th><th>Role</th><th>Active</th><th>M365</th><th>Actions</th>
    </tr></thead><tbody>`;
    for (const u of _adminUsers) {
        const activeChecked = u.is_active !== false ? 'checked' : '';
        html += `<tr>
            <td>${u.name || '—'}</td>
            <td>${u.email}</td>
            <td><select onchange="updateUserField(${u.id}, 'role', this.value)" class="s-select" style="padding:4px 8px;font-size:12px">
                <option value="buyer" ${u.role==='buyer'?'selected':''}>Buyer</option>
                <option value="trader" ${u.role==='trader'?'selected':''}>Trader</option>
                <option value="sales" ${u.role==='sales'?'selected':''}>Sales</option>
                <option value="manager" ${u.role==='manager'?'selected':''}>Manager</option>
                <option value="admin" ${u.role==='admin'?'selected':''}>Admin</option>
            </select></td>
            <td><input type="checkbox" ${activeChecked} onchange="updateUserField(${u.id}, 'is_active', this.checked)"></td>
            <td>${u.m365_connected ? '<span style="color:var(--teal)">Connected</span>' : '<span style="color:var(--muted)">—</span>'}</td>
            <td><button class="btn btn-ghost btn-sm" onclick="deleteAdminUser(${u.id}, '${(u.name||u.email).replace(/'/g,"\\'")}')">Delete</button></td>
        </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
}

async function updateUserField(userId, field, value) {
    try {
        const body = {};
        body[field] = value;
        await apiFetch(`/api/admin/users/${userId}`, {method:'PUT', body:body});
    } catch (e) {
        showToast('Error: ' + (e.message || e), 'error');
        loadAdminUsers();
    }
}

async function deleteAdminUser(userId, name) {
    if (!confirm(`Delete user "${name}"? This cannot be undone.`)) return;
    try {
        await apiFetch(`/api/admin/users/${userId}`, {method:'DELETE'});
        _userListCache = null;  // Invalidate cache
        loadAdminUsers();
    } catch (e) {
        showToast('Error: ' + (e.message || e), 'error');
    }
}

async function createUser() {
    const _v = id => document.getElementById(id)?.value || '';
    const name = _v('newUserName').trim();
    const email = _v('newUserEmail').trim();
    const role = _v('newUserRole');
    if (!name || !email) { showToast('Name and email are required', 'error'); return; }
    try {
        await apiFetch('/api/admin/users', {method:'POST', body:{name, email, role}});
        const nuN = document.getElementById('newUserName'); if (nuN) nuN.value = '';
        const nuE = document.getElementById('newUserEmail'); if (nuE) nuE.value = '';
        _userListCache = null;  // Invalidate cache
        showToast('User created successfully', 'success');
        if (typeof loadAdminUsers === 'function') loadAdminUsers();
    } catch (e) {
        showToast('Error: ' + (e.message || e), 'error');
    }
}





// ═══════════════════════════════════════════════════════════════════════
//  TEAMS INTEGRATION CONFIG
// ═══════════════════════════════════════════════════════════════════════

async function loadTeamsConfig() {
    const el = document.getElementById('teamsConfigContent');
    if (!el) return;
    el.innerHTML = '<p class="empty">Loading Teams configuration...</p>';
    try {
        const config = await apiFetch('/api/admin/teams/config');
        let html = `
            <div class="card s-card">
                <h3>Teams Channel Notifications</h3>
                <p class="s-desc">
                    Post critical AVAIL events (hot requirements, competitive quotes, ownership warnings, stock matches) to a Teams channel.
                </p>
                <div class="s-form">
                    <div class="s-row">
                        <label class="s-label" style="width:100px">Enabled</label>
                        <input type="checkbox" id="teamsEnabled" ${config.enabled ? 'checked' : ''} style="width:16px;height:16px">
                    </div>
                    <div>
                        <label class="s-label" style="display:block;margin-bottom:4px">Teams Channel</label>
                        <select id="teamsChannelSelect" class="s-select" style="width:100%">
                            <option value="">— Select a channel —</option>
                        </select>
                        <button class="btn btn-ghost btn-sm" onclick="refreshTeamsChannels()" style="margin-top:6px;font-size:11px">Refresh Channels</button>
                    </div>
                    <div>
                        <label class="s-label" style="display:block;margin-bottom:4px">Hot Requirement Threshold ($)</label>
                        <input id="teamsHotThreshold" type="number" value="${config.hot_threshold || 10000}" min="0" step="500" class="s-input" style="width:160px">
                        <span class="s-hint" style="margin-left:6px">Notify when requirement value exceeds this</span>
                    </div>
                    <div class="s-row" style="margin-top:8px">
                        <button class="btn btn-primary" onclick="saveTeamsConfig()">Save Configuration</button>
                        <button class="btn btn-ghost" onclick="testTeamsPost()">Send Test Card</button>
                    </div>
                    <div id="teamsStatus" class="s-status"></div>
                </div>
            </div>`;
        el.innerHTML = html;

        // If we have a saved config, load channels to populate the dropdown
        if (config.team_id && config.channel_id) {
            _populateChannelDropdown(config.team_id, config.channel_id, config.channel_name);
        }
        refreshTeamsChannels();
    } catch (e) {
        el.innerHTML = `<p class="empty" style="color:var(--red)">Error loading Teams config: ${e.message || e}</p>`;
    }
}

function _populateChannelDropdown(teamId, channelId, channelName) {
    const sel = document.getElementById('teamsChannelSelect');
    if (!sel) return;
    // Add current selection as an option so it's visible immediately
    const opt = document.createElement('option');
    opt.value = `${teamId}|${channelId}`;
    opt.textContent = channelName || `${teamId} / ${channelId}`;
    opt.selected = true;
    sel.appendChild(opt);
}

async function refreshTeamsChannels() {
    const sel = document.getElementById('teamsChannelSelect');
    if (!sel) return;
    const currentVal = sel.value;

    try {
        const data = await apiFetch('/api/admin/teams/channels');
        const channels = data.channels || [];
        sel.innerHTML = '<option value="">— Select a channel —</option>';
        for (const ch of channels) {
            const val = `${ch.team_id}|${ch.channel_id}`;
            const opt = document.createElement('option');
            opt.value = val;
            opt.textContent = `${ch.team_name} → ${ch.channel_name}`;
            if (val === currentVal) opt.selected = true;
            sel.appendChild(opt);
        }
        if (!channels.length) {
            sel.innerHTML = '<option value="">No channels found (connect M365 first)</option>';
        }
    } catch (e) {
        const status = document.getElementById('teamsStatus');
        if (status) status.innerHTML = `<span style="color:var(--red)">Could not load channels: ${e.message || e}</span>`;
    }
}

async function saveTeamsConfig() {
    const status = document.getElementById('teamsStatus');
    const sel = document.getElementById('teamsChannelSelect');
    const val = sel ? sel.value : '';
    if (!val) {
        if (status) status.innerHTML = '<span style="color:var(--red)">Please select a channel.</span>';
        return;
    }
    const [teamId, channelId] = val.split('|');
    const channelName = sel.options[sel.selectedIndex]?.textContent || '';
    const enabled = document.getElementById('teamsEnabled')?.checked ?? true;
    const hotThreshold = parseFloat(document.getElementById('teamsHotThreshold')?.value) || 10000;

    try {
        await apiFetch('/api/admin/teams/config', {
            method: 'POST',
            body: {
                team_id: teamId,
                channel_id: channelId,
                channel_name: channelName,
                enabled: enabled,
                hot_threshold: hotThreshold,
            },
        });
        if (status) status.innerHTML = '<span style="color:var(--green)">Configuration saved.</span>';
    } catch (e) {
        if (status) status.innerHTML = `<span style="color:var(--red)">Save failed: ${e.message || e}</span>`;
    }
}

async function testTeamsPost() {
    const status = document.getElementById('teamsStatus');
    if (status) status.innerHTML = '<span style="color:var(--muted)">Sending test card...</span>';
    try {
        const res = await apiFetch('/api/admin/teams/test', {method: 'POST'});
        if (status) status.innerHTML = `<span style="color:var(--green)">${res.message || 'Test card sent!'}</span>`;
    } catch (e) {
        if (status) status.innerHTML = `<span style="color:var(--red)">Test failed: ${e.message || e}</span>`;
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  Deep Enrichment UI
// ═══════════════════════════════════════════════════════════════════════

let _eqSelectedIds = new Set();
let _bfPollInterval = null;

window.addEventListener('beforeunload', () => {
    if (_bfPollInterval) { clearInterval(_bfPollInterval); _bfPollInterval = null; }
});

function switchEnrichTab(tab, btn) {
    document.querySelectorAll('#enrichTabs .tab').forEach(t => t.classList.remove('on'));
    btn.classList.add('on');
    const _p = (id, v) => { const el = document.getElementById(id); if (el) el.style.display = v; };
    _p('enrichQueuePanel', tab === 'queue' ? '' : 'none');
    _p('enrichBackfillPanel', tab === 'backfill' ? '' : 'none');
    _p('enrichJobsPanel', tab === 'jobs' ? '' : 'none');
    const m365Panel = document.getElementById('enrichM365Panel');
    if (m365Panel) m365Panel.style.display = tab === 'm365' ? '' : 'none';
    _p('enrichCreditsPanel', tab === 'credits' ? '' : 'none');
    _p('enrichGapsPanel', tab === 'gaps' ? '' : 'none');

    if (tab === 'queue') loadEnrichmentQueue();
    if (tab === 'backfill') loadEnrichmentJobs();
    if (tab === 'jobs') loadEnrichmentJobs();
    if (tab === 'm365') loadM365Status();
    if (tab === 'credits') loadCreditUsage();
    if (tab === 'gaps') loadCustomerGaps();
}

async function loadEnrichmentQueue() {
    const list = document.getElementById('enrichQueueList');
    const statusFilter = document.getElementById('eqStatusFilter')?.value || 'pending';
    const entityFilter = document.getElementById('eqEntityFilter')?.value || '';
    _eqSelectedIds.clear();
    updateBulkApproveBtn();

    try {
        let url = `/api/enrichment/queue?status=${statusFilter}&limit=100`;
        if (entityFilter) url += `&entity_type=${entityFilter}`;
        const data = await apiFetch(url);
        const items = data.items || [];
        const countEl = document.getElementById('eqCount');
        if (countEl) countEl.textContent = `${data.total || items.length} items`;

        if (!items.length) {
            list.innerHTML = '<p class="empty">No enrichment items found.</p>';
            return;
        }

        let html = '<table class="tbl"><thead><tr>';
        if (statusFilter === 'pending') html += '<th><input type="checkbox" onchange="eqToggleAll(this)"></th>';
        html += '<th>Entity</th><th>Field</th><th>Current</th><th>Proposed</th><th>Confidence</th><th>Source</th><th>Status</th>';
        if (statusFilter === 'pending') html += '<th>Actions</th>';
        html += '</tr></thead><tbody>';

        for (const item of items) {
            const confPct = Math.round(item.confidence * 100);
            const confClass = confPct >= 80 ? 'color:var(--green)' : confPct >= 50 ? 'color:var(--yellow,#e6a817)' : 'color:var(--red)';
            const currentDisp = item.current_value ? esc(String(item.current_value).substring(0, 40)) : '<span style="color:var(--muted)">—</span>';
            const proposedDisp = esc(String(item.proposed_value).substring(0, 60));

            html += '<tr>';
            if (statusFilter === 'pending') {
                html += `<td><input type="checkbox" data-eqid="${item.id}" onchange="eqToggleItem(${item.id}, this.checked)"></td>`;
            }
            html += `<td><strong>${esc(item.entity_name || '?')}</strong><br><small style="color:var(--muted)">${esc(item.entity_type || '')}</small></td>`;
            html += `<td>${esc(item.field_name)}</td>`;
            html += `<td>${currentDisp}</td>`;
            html += `<td style="font-weight:500">${proposedDisp}</td>`;
            html += `<td><span style="${confClass};font-weight:600">${confPct}%</span></td>`;
            html += `<td><span class="badge badge-${item.source}">${esc(item.source)}</span></td>`;
            html += `<td><span class="status-${item.status}">${esc(item.status)}</span></td>`;
            if (statusFilter === 'pending') {
                html += `<td>
                    <button class="btn btn-sm" onclick="approveEnrichItem(${item.id})" title="Approve">✓</button>
                    <button class="btn btn-sm btn-outline" onclick="rejectEnrichItem(${item.id})" title="Reject">✗</button>
                </td>`;
            }
            html += '</tr>';
        }
        html += '</tbody></table>';
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = `<p class="empty" style="color:var(--red)">Error: ${esc(e.message || String(e))}</p>`;
    }
}

function eqToggleAll(checkbox) {
    document.querySelectorAll('[data-eqid]').forEach(cb => {
        cb.checked = checkbox.checked;
        eqToggleItem(parseInt(cb.dataset.eqid), cb.checked);
    });
}

function eqToggleItem(id, checked) {
    if (checked) _eqSelectedIds.add(id);
    else _eqSelectedIds.delete(id);
    updateBulkApproveBtn();
}

function updateBulkApproveBtn() {
    const btn = document.getElementById('eqBulkApproveBtn');
    if (btn) {
        btn.style.display = _eqSelectedIds.size > 0 ? '' : 'none';
        btn.textContent = `Approve Selected (${_eqSelectedIds.size})`;
    }
}

async function approveEnrichItem(id) {
    try {
        await apiFetch(`/api/enrichment/queue/${id}/approve`, {method: 'POST'});
        showToast('Approved');
        loadEnrichmentQueue();
        loadEnrichmentStats();
    } catch (e) {
        showToast('Approve failed: ' + (e.message || e), 'error');
    }
}

async function rejectEnrichItem(id) {
    try {
        await apiFetch(`/api/enrichment/queue/${id}/reject`, {method: 'POST'});
        showToast('Rejected');
        loadEnrichmentQueue();
    } catch (e) {
        showToast('Reject failed: ' + (e.message || e), 'error');
    }
}

async function bulkApproveSelected() {
    if (!_eqSelectedIds.size) return;
    try {
        const res = await apiFetch('/api/enrichment/queue/bulk-approve', {
            method: 'POST',
            body: {ids: Array.from(_eqSelectedIds)},
        });
        showToast(`Approved ${res.approved} items`);
        _eqSelectedIds.clear();
        loadEnrichmentQueue();
        loadEnrichmentStats();
    } catch (e) {
        showToast('Bulk approve failed: ' + (e.message || e), 'error');
    }
}

async function startBackfill() {
    const statusEl = document.getElementById('bfStatus');
    const types = [];
    if (document.getElementById('bfVendors')?.checked) types.push('vendor');
    if (document.getElementById('bfCompanies')?.checked) types.push('company');
    if (!types.length) {
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--red)">Select at least one entity type</span>';
        return;
    }
    const maxItems = parseInt(document.getElementById('bfMaxItems')?.value) || 500;
    const includeEmail = document.getElementById('bfDeepEmail')?.checked || false;

    try {
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--muted)">Starting...</span>';
        const res = await apiFetch('/api/enrichment/backfill', {
            method: 'POST',
            body: {entity_types: types, max_items: maxItems, include_deep_email: includeEmail},
        });
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--green)">Job #${res.job_id} started</span>`;
        pollBackfillProgress(res.job_id);
    } catch (e) {
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--red)">${esc(e.message || String(e))}</span>`;
    }
}

function pollBackfillProgress(jobId) {
    const box = document.getElementById('bfProgressBox');
    const bar = document.getElementById('bfProgressBar');
    const label = document.getElementById('bfProgressLabel');
    if (box) box.style.display = '';

    if (_bfPollInterval) clearInterval(_bfPollInterval);
    _bfPollInterval = setInterval(async () => {
        try {
            const job = await apiFetch(`/api/enrichment/jobs/${jobId}`);
            if (bar) bar.style.width = job.progress_pct + '%';
            if (label) label.textContent = `${job.processed_items}/${job.total_items} processed, ${job.enriched_items} enriched, ${job.error_count} errors (${job.progress_pct}%)`;

            if (['completed','failed','cancelled'].includes(job.status)) {
                clearInterval(_bfPollInterval);
                _bfPollInterval = null;
                if (label) label.textContent += ` — ${job.status}`;
                loadEnrichmentStats();
            }
        } catch (e) {
            clearInterval(_bfPollInterval);
            _bfPollInterval = null;
        }
    }, 5000);
}

async function loadEnrichmentJobs() {
    const list = document.getElementById('enrichJobsList');
    try {
        const data = await apiFetch('/api/enrichment/jobs?limit=20');
        const jobs = data.jobs || [];
        if (!jobs.length) {
            list.innerHTML = '<p class="empty">No enrichment jobs yet.</p>';
            return;
        }

        let html = '<table class="tbl"><thead><tr><th>ID</th><th>Type</th><th>Status</th><th>Progress</th><th>Enriched</th><th>Errors</th><th>Started By</th><th>Started</th><th>Completed</th><th>Actions</th></tr></thead><tbody>';
        for (const job of jobs) {
            const statusClass = job.status === 'completed' ? 'color:var(--green)' :
                                job.status === 'running' ? 'color:var(--teal)' :
                                job.status === 'failed' ? 'color:var(--red)' : '';
            html += `<tr>
                <td>#${job.id}</td>
                <td>${esc(job.job_type)}</td>
                <td style="${statusClass};font-weight:600">${esc(job.status)}</td>
                <td>${job.progress_pct}% (${job.processed_items}/${job.total_items})</td>
                <td>${job.enriched_items}</td>
                <td>${job.error_count}</td>
                <td>${esc(job.started_by || '—')}</td>
                <td>${job.started_at ? fmtDateTime(job.started_at) : '—'}</td>
                <td>${job.completed_at ? fmtDateTime(job.completed_at) : '—'}</td>
                <td>${job.status === 'running' ? `<button class="btn btn-sm btn-outline" onclick="cancelEnrichJob(${job.id})">Cancel</button>` : ''}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = `<p class="empty" style="color:var(--red)">Error: ${esc(e.message || String(e))}</p>`;
    }
}

async function cancelEnrichJob(jobId) {
    try {
        await apiFetch(`/api/enrichment/jobs/${jobId}/cancel`, {method: 'POST'});
        showToast('Job cancelled');
        loadEnrichmentJobs();
    } catch (e) {
        showToast('Cancel failed: ' + (e.message || e), 'error');
    }
}

async function loadEnrichmentStats() {
    try {
        const s = await apiFetch('/api/enrichment/stats');
        const ve = document.getElementById('esVendors');
        const ce = document.getElementById('esCompanies');
        const pe = document.getElementById('esPending');
        const aa = document.getElementById('esAutoApplied');
        const aj = document.getElementById('esActiveJobs');
        if (ve) ve.textContent = `${s.vendors_enriched}/${s.vendors_total}`;
        if (ce) ce.textContent = `${s.companies_enriched}/${s.companies_total}`;
        if (pe) pe.textContent = s.queue_pending;
        if (aa) aa.textContent = s.queue_auto_applied;
        if (aj) aj.textContent = s.active_jobs;
        const em = document.getElementById('esVendorEmails');
        if (em) em.textContent = s.vendor_emails || 0;
    } catch (e) {
        console.error('enrichment stats error:', e);
    }
}

async function refreshEnrichmentBadge() {
    try {
        const s = await apiFetch('/api/enrichment/stats');
        const badge = document.getElementById('enrichmentBadge');
        if (badge && s.queue_pending > 0) {
            badge.textContent = s.queue_pending;
            badge.style.display = '';
        } else if (badge) {
            badge.style.display = 'none';
        }
    } catch (e) { logCatchError('backfillPoll', e); }
}

// deepEnrichVendor and deepEnrichCompany replaced by unifiedEnrichVendor/unifiedEnrichCompany above

// ═══════════════════════════════════════════════════════════════════════
//  Email Backfill & Website Scraping
// ═══════════════════════════════════════════════════════════════════════

async function startEmailBackfill() {
    const statusEl = document.getElementById('emailBfStatus');
    try {
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--muted)">Running...</span>';
        const res = await apiFetch('/api/enrichment/backfill-emails', {method: 'POST'});
        const parts = [];
        if (res.activity_log_created) parts.push(`${res.activity_log_created} from activity log`);
        if (res.vendor_card_created) parts.push(`${res.vendor_card_created} from vendor cards`);
        if (res.brokerbin_created) parts.push(`${res.brokerbin_created} from BrokerBin`);
        const msg = parts.length ? parts.join(', ') : 'No new emails found';
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--green)">${esc(msg)}</span>`;
        loadEnrichmentStats();
    } catch (e) {
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--red)">${esc(e.message || String(e))}</span>`;
    }
}

async function startWebsiteScrape() {
    const statusEl = document.getElementById('scrapeStatus');
    const maxVendors = parseInt(document.getElementById('scrapeMaxVendors')?.value) || 500;
    try {
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--muted)">Scraping... this may take a few minutes.</span>';
        const res = await apiFetch('/api/enrichment/scrape-websites', {
            method: 'POST',
            body: {max_vendors: maxVendors},
        });
        const msg = `Scraped ${res.vendors_scraped || 0} vendors, found ${res.emails_found || 0} emails`;
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--green)">${esc(msg)}</span>`;
        loadEnrichmentStats();
    } catch (e) {
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--red)">${esc(e.message || String(e))}</span>`;
    }
}

// ═══════════════════════════════════════════════════════════════════════
//  M365 Inbox Mining Status
// ═══════════════════════════════════════════════════════════════════════

async function loadM365Status() {
    const list = document.getElementById('m365UserList');
    if (!list) return;
    try {
        const data = await apiFetch('/api/enrichment/m365-status');
        const users = data.users || [];
        if (!users.length) {
            list.innerHTML = '<p class="empty">No users found.</p>';
            return;
        }
        let html = '<table class="tbl"><thead><tr><th>User</th><th>M365 Status</th><th>Last Inbox Scan</th><th>Last Deep Scan</th><th>Actions</th></tr></thead><tbody>';
        for (const u of users) {
            const connected = u.m365_connected;
            const statusHtml = connected
                ? '<span style="color:var(--green);font-weight:600">Connected</span>'
                : `<span style="color:var(--red)">Not Connected</span>${u.error_reason ? `<br><small style="color:var(--muted)">${esc(u.error_reason)}</small>` : ''}`;
            const lastScan = u.last_inbox_scan ? fmtDateTime(u.last_inbox_scan) : '—';
            const lastDeep = u.last_deep_scan ? fmtDateTime(u.last_deep_scan) : '—';
            const actions = connected
                ? `<button class="btn btn-sm" onclick="triggerDeepScan(${u.id})">Deep Scan</button>`
                : '<small style="color:var(--muted)">Must log in via Azure AD</small>';
            html += `<tr><td><strong>${esc(u.name)}</strong><br><small style="color:var(--muted)">${esc(u.email)}</small></td><td>${statusHtml}</td><td>${lastScan}</td><td>${lastDeep}</td><td>${actions}</td></tr>`;
        }
        html += '</tbody></table>';
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = `<p class="empty" style="color:var(--red)">Error: ${esc(e.message || String(e))}</p>`;
    }
}

async function triggerDeepScan(userId) {
    try {
        showToast('Starting deep inbox scan...');
        const res = await apiFetch(`/api/enrichment/deep-email-scan/${userId}`, {method: 'POST'});
        showToast(`Deep scan complete: ${res.contacts_created || 0} new contacts found`);
        loadM365Status();
        loadEnrichmentStats();
    } catch (e) {
        showToast('Deep scan failed: ' + (e.message || e), 'error');
    }
}

// ── Trouble Tickets (Settings Tab) — REMOVED ───────────────────────
// All ticket management is now in the unified tickets.js sidebar view.
// The Settings > "Trouble Tickets" tab has been removed from index.html.
// Old error-report functions (loadTroubleTickets, viewTicketDetail,
// updateTicketStatus, exportTicketsXlsx, copyPromptToClipboard,
// regeneratePrompt) have been deleted. See tickets.js for the unified UI.

// ── Vendor Dedup ────────────────────────────────────────────────────────

/// ── Mass Account Transfer ───────────────────────────────────────────────

const _transferSelected = new Set();

async function loadTransferPanel() {
    const src = document.getElementById('transferSourceUser');
    const tgt = document.getElementById('transferTargetUser');
    if (!src || !tgt) return;
    try {
        const users = await apiFetch('/api/admin/users');
        const opts = users.map(u => `<option value="${u.id}">${u.name} (${u.email})</option>`).join('');
        src.innerHTML = '<option value="">Select user...</option>' + opts;
        tgt.innerHTML = '<option value="">Select user...</option>' + opts;
    } catch (e) { showToast('Failed to load users', 'error'); }
}

async function loadTransferPreview() {
    const sourceId = document.getElementById('transferSourceUser')?.value;
    const container = document.getElementById('transferSitesContainer');
    const empty = document.getElementById('transferEmpty');
    _transferSelected.clear();
    if (!sourceId) {
        if (container) container.style.display = 'none';
        if (empty) empty.style.display = '';
        return;
    }
    try {
        const data = await apiFetch(`/api/admin/transfer/preview?source_user_id=${sourceId}`);
        const tbody = document.getElementById('transferSitesBody');
        if (!data.sites.length) {
            if (container) container.style.display = 'none';
            if (empty) { empty.style.display = ''; empty.innerHTML = '<p class="empty">This user has no assigned sites</p>'; }
            return;
        }
        if (empty) empty.style.display = 'none';
        if (container) container.style.display = '';
        tbody.innerHTML = data.sites.map(s => `<tr>
            <td><input type="checkbox" onchange="toggleTransferSite(${s.id},this.checked)"></td>
            <td>${esc(s.site_name)}</td><td>${esc(s.company_name)}</td>
            <td>${esc(s.city||'')}</td><td>${esc(s.state||'')}</td>
            <td>${s.is_active ? 'Yes' : 'No'}</td></tr>`).join('');
        updateTransferSelectedCount();
    } catch (e) { showToast('Failed to load preview', 'error'); }
}

function toggleTransferSite(id, checked) {
    if (checked) _transferSelected.add(id); else _transferSelected.delete(id);
    updateTransferSelectedCount();
}

function toggleTransferSelectAll(checked) {
    _transferSelected.clear();
    document.querySelectorAll('#transferSitesBody input[type=checkbox]').forEach(cb => {
        cb.checked = checked;
        if (checked) {
            const row = cb.closest('tr');
            const id = parseInt(cb.getAttribute('onchange').match(/\d+/)[0]);
            _transferSelected.add(id);
        }
    });
    updateTransferSelectedCount();
}

function updateTransferSelectedCount() {
    const lbl = document.getElementById('transferSelectedCount');
    if (lbl) lbl.textContent = _transferSelected.size + ' selected';
    const btn = document.getElementById('transferBtn');
    if (btn) btn.disabled = _transferSelected.size === 0;
}

async function executeTransfer() {
    const sourceId = parseInt(document.getElementById('transferSourceUser')?.value);
    const targetId = parseInt(document.getElementById('transferTargetUser')?.value);
    if (!sourceId || !targetId) return showToast('Select both source and target users', 'error');
    if (sourceId === targetId) return showToast('Source and target must be different', 'error');
    if (!_transferSelected.size) return showToast('No sites selected', 'error');
    if (!confirm(`Transfer ${_transferSelected.size} site(s)?`)) return;
    try {
        const result = await apiFetch('/api/admin/transfer/execute', {
            method: 'POST',
            body: { source_user_id: sourceId, target_user_id: targetId, site_ids: [..._transferSelected] },
        });
        showToast(`Transferred ${result.transferred} site(s)${result.skipped ? `, ${result.skipped} skipped` : ''}`, 'success');
        loadTransferPreview();
    } catch (e) { showToast('Transfer failed: ' + (e.message || e), 'error'); }
}


/// ── Suggested Accounts (Prospect Pool from prospect_accounts) ───────────

let _suggestedPage = 1;
let _suggestedReadiness = '';
let _suggestedAbort = null;

const debouncedLoadSuggested = debounce(() => { _suggestedPage = 1; loadSuggested(); }, 300);

function setSuggestedReadiness(val, btn) {
    _suggestedReadiness = val;
    document.querySelectorAll('#suggestedPills .chip').forEach(c => c.classList.toggle('on', c.dataset.value === val));
    _suggestedPage = 1;
    loadSuggested();
}

async function showSuggested() {
    showView('view-suggested');
    const viewEl = document.getElementById('view-suggested');
    if (viewEl) { viewEl.classList.remove('u-hidden'); viewEl.style.display = 'flex'; }
    setCurrentReqId(null);
    _suggestedPage = 1;
    const search = document.getElementById('suggestedSearch');
    if (search) search.value = '';
    const size = document.getElementById('suggestedSize');
    if (size) size.value = '';
    const fit = document.getElementById('suggestedFitScore');
    if (fit) fit.value = '0';
    const readiness = document.getElementById('suggestedReadinessScore');
    if (readiness) readiness.value = '0';
    const sort = document.getElementById('suggestedSort');
    if (sort) sort.value = 'fit_desc';
    const industryEl = document.getElementById('suggestedIndustry');
    if (industryEl) industryEl.value = '';
    const revenueEl = document.getElementById('suggestedRevenue');
    if (revenueEl) revenueEl.value = '';
    const regionEl = document.getElementById('suggestedRegion');
    if (regionEl) regionEl.value = '';
    const sourceEl = document.getElementById('suggestedSource');
    if (sourceEl) sourceEl.value = '';
    _setTopViewLabel('Prospecting');
    await loadSuggested();
    _populateSuggestedFilters();
}

async function loadSuggested() {
    if (_suggestedAbort) { try { _suggestedAbort.abort(); } catch(e){} }
    _suggestedAbort = new AbortController();
    const grid = document.getElementById('suggestedGrid');
    if (grid) grid.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div> Loading…</div>';

    const search = (document.getElementById('suggestedSearch')?.value || '').trim();
    const size = document.getElementById('suggestedSize')?.value || '';
    const fitScore = document.getElementById('suggestedFitScore')?.value || '0';
    const readinessScore = document.getElementById('suggestedReadinessScore')?.value || '0';
    const industry = document.getElementById('suggestedIndustry')?.value || '';
    const revenue = document.getElementById('suggestedRevenue')?.value || '';
    const region = document.getElementById('suggestedRegion')?.value || '';
    const source = document.getElementById('suggestedSource')?.value || '';
    const sort = document.getElementById('suggestedSort')?.value || 'fit_desc';
    const params = new URLSearchParams({ page: _suggestedPage, per_page: 100, sort });
    if (search) params.set('search', search);
    if (size) params.set('employee_size', size);
    if (parseInt(fitScore) > 0) params.set('min_fit_score', fitScore);
    if (parseInt(readinessScore) > 0) params.set('min_readiness_score', readinessScore);
    if (industry) params.set('industry', industry);
    if (revenue) params.set('revenue_range', revenue);
    if (region) params.set('region', region);
    if (source) params.set('discovery_source', source);

    try {
        const [data, stats] = await Promise.all([
            apiFetch('/api/prospects/suggested?' + params, { signal: _suggestedAbort.signal }),
            apiFetch('/api/prospects/suggested/stats', { signal: _suggestedAbort.signal }),
        ]);
        renderSuggestedStats(stats);
        renderSuggestedGrid(data);
    } catch (e) {
        if (e.name === 'AbortError') return;
        showToast('Failed to load suggested accounts', 'error');
        console.error(e);
    }
}

async function _populateSuggestedFilters() {
    try {
        const stats = await apiFetch('/api/prospects/suggested/stats');
        const industryEl = document.getElementById('suggestedIndustry');
        if (industryEl && stats.industries) {
            const curVal = industryEl.value;
            industryEl.textContent = '';
            const allOpt = document.createElement('option');
            allOpt.value = '';
            allOpt.textContent = 'All Industries';
            industryEl.appendChild(allOpt);
            for (const ind of stats.industries) {
                const opt = document.createElement('option');
                opt.value = ind;
                opt.textContent = ind;
                industryEl.appendChild(opt);
            }
            if (curVal) industryEl.value = curVal;
        }
        const regionEl = document.getElementById('suggestedRegion');
        if (regionEl && stats.regions) {
            const curVal = regionEl.value;
            regionEl.textContent = '';
            const allOpt = document.createElement('option');
            allOpt.value = '';
            allOpt.textContent = 'All Regions';
            regionEl.appendChild(allOpt);
            for (const reg of stats.regions) {
                const opt = document.createElement('option');
                opt.value = reg;
                opt.textContent = reg;
                regionEl.appendChild(opt);
            }
            if (curVal) regionEl.value = curVal;
        }
    } catch(e) { console.warn('Failed to load filter options', e); }
}

function renderSuggestedStats(stats) {
    const el = document.getElementById('suggestedStats');
    if (!el) return;
    el.innerHTML = `
        <span class="stat-item"><span class="stat-val">${stats.total_available}</span> available</span>
        <span class="stat-item"><span class="stat-val">${stats.call_now_count}</span> call now</span>
        <span class="stat-item"><span class="stat-val">${stats.nurture_count}</span> nurture</span>
        <span class="stat-item"><span class="stat-val">${stats.high_fit_count}</span> high fit</span>
        <span class="stat-item"><span class="stat-val">${stats.claimed_this_month}</span> claimed this month</span>
        <span class="stat-item" style="margin-left:auto;cursor:pointer;color:var(--blue);font-weight:600" onclick="toggleScoringGuide()">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            Scoring Guide
        </span>
    `;
}

function renderSuggestedGrid(data) {
    const grid = document.getElementById('suggestedGrid');
    const pager = document.getElementById('suggestedPager');
    if (!grid) return;

    if (!data.items.length) {
        grid.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">No accounts match your filters</div>';
        if (pager) pager.innerHTML = '';
        return;
    }

    grid.innerHTML = data.items.map(a => {
        // Readiness tier badge
        const tierLabels = { call_now: 'Call Now', nurture: 'Nurture', monitor: 'Monitor' };
        const tierBadge = `<span class="suggested-badge ${a.readiness_tier}">${tierLabels[a.readiness_tier] || a.readiness_tier}</span>`;
        const sfBadge = a.company_id ? '<span class="suggested-badge sf">SF</span>' : '';
        const sourceBadge = a.discovery_source
            ? '<span class="suggested-badge" style="background:var(--border);color:var(--text);font-size:9px">' + esc(a.discovery_source) + '</span>'
            : '';

        // Domain link
        const domainLink = a.domain
            ? `<a class="suggested-card-domain" href="https://${escAttr(a.domain)}" target="_blank" rel="noopener">${esc(a.domain)}</a>`
            : '';

        // Meta info
        const meta = [];
        if (a.industry) meta.push('<span>' + esc(a.industry) + '</span>');
        if (a.employee_count_range) meta.push('<span>' + esc(a.employee_count_range) + ' emp</span>');
        if (a.hq_location) meta.push('<span>' + esc(a.hq_location) + '</span>');
        if (a.revenue_range) meta.push('<span>' + esc(a.revenue_range) + '</span>');

        // Score bars
        const fitPct = Math.min(a.fit_score, 100);
        const readPct = Math.min(a.readiness_score, 100);
        const scoreBars = `<div class="suggested-scores">
            <div class="score-bar"><div class="score-bar-label"><span>Fit</span><span>${a.fit_score}</span></div><div class="score-bar-track"><div class="score-bar-fill fit" style="width:${fitPct}%"></div></div></div>
            <div class="score-bar"><div class="score-bar-label"><span>Readiness</span><span>${a.readiness_score}</span></div><div class="score-bar-track"><div class="score-bar-fill readiness" style="width:${readPct}%"></div></div></div>
        </div>`;

        // Signal tags
        let signalHtml = '';
        if (a.signal_tags && a.signal_tags.length) {
            signalHtml = '<div class="suggested-signals">' +
                a.signal_tags.map(s => `<span class="signal-tag ${s.type}">${esc(s.label)}</span>`).join('') +
                '</div>';
        }

        // Contacts preview
        let contactsHtml = '';
        if (a.contacts_count > 0) {
            const avatars = (a.contacts_preview || []).slice(0, 3).map(c => {
                const initials = (c.name || '?').split(' ').map(w => w[0]).join('').substring(0, 2).toUpperCase();
                return `<span class="contact-avatar" title="${escAttr(c.name || '')}">${initials}</span>`;
            }).join('');
            contactsHtml = `<div class="suggested-contacts">
                <div class="contacts-avatars">${avatars}</div>
                <span class="contacts-summary">${a.contacts_count} contacts &middot; ${a.contacts_verified} verified &middot; ${a.contacts_decision_makers} DMs</span>
            </div>`;
        }

        // Similar customers
        let similarHtml = '';
        if (a.similar_customers && a.similar_customers.length) {
            const names = a.similar_customers.map(s => esc(s.name || s)).join(', ');
            similarHtml = `<div class="suggested-similar">Similar to: <strong>${names}</strong></div>`;
        }

        // AI writeup snippet
        const writeupHtml = a.ai_writeup
            ? `<div class="suggested-writeup">${esc(a.ai_writeup)}</div>`
            : '';

        // One-liner
        const oneLinerHtml = a.one_liner
            ? `<div style="font-size:10px;color:var(--text);padding:2px 0;font-style:italic;border-left:2px solid var(--blue);padding-left:6px;margin:3px 0;line-height:1.3">${esc(a.one_liner)}</div>`
            : '';

        // Warm intro badge
        const warmBadge = a.has_warm_intro
            ? `<span class="suggested-badge" style="background:${a.warm_intro_warmth === 'hot' ? '#dcfce7;color:#166534' : '#fef9c3;color:#854d0e'}">${a.warm_intro_warmth === 'hot' ? '🔥 Warm Intro' : '👋 Prior Contact'}</span>`
            : '';

        return `<div class="suggested-card" id="sg-card-${a.id}">
            <div class="suggested-card-header">
                <div>
                    <div class="suggested-card-name" style="cursor:pointer" onclick="openSuggestedDetail(${a.id})">${esc(a.name)}</div>
                    ${domainLink}
                </div>
                <div class="suggested-card-badges">${warmBadge}${tierBadge}${sfBadge}${sourceBadge}</div>
            </div>
            ${oneLinerHtml}
            ${meta.length ? '<div class="suggested-card-meta">' + meta.join(' &middot; ') + '</div>' : ''}
            ${scoreBars}
            ${signalHtml}
            ${contactsHtml}
            ${similarHtml}
            ${writeupHtml}
            <div class="suggested-card-actions">
                <button class="btn-claim" onclick="claimSuggestedAccount(${a.id},'${escAttr(a.name)}')">Claim</button>
                <select class="dismiss-select" onchange="dismissSuggestedAccount(${a.id},'${escAttr(a.name)}',this.value);this.selectedIndex=0">
                    <option value="">Dismiss…</option>
                    <option value="not_relevant">Not relevant</option>
                    <option value="competitor">Competitor</option>
                    <option value="too_small">Too small</option>
                    <option value="too_large">Too large</option>
                    <option value="duplicate">Duplicate</option>
                    <option value="other">Other</option>
                </select>
            </div>
        </div>`;
    }).join('');

    // Pagination — inline in toolbar
    if (pager) {
        const totalPages = Math.ceil(data.total / data.per_page);
        let html = '';
        if (_suggestedPage > 1) html += `<button class="btn btn-ghost btn-sm" style="padding:2px 6px;font-size:11px" onclick="suggestedGoPage(${_suggestedPage - 1})">&laquo;</button>`;
        html += `<span>${data.total} accounts &middot; ${data.page}/${totalPages}</span>`;
        if (_suggestedPage < totalPages) html += `<button class="btn btn-ghost btn-sm" style="padding:2px 6px;font-size:11px" onclick="suggestedGoPage(${_suggestedPage + 1})">&raquo;</button>`;
        pager.innerHTML = html;
    }
}

function suggestedGoPage(page) {
    _suggestedPage = page;
    loadSuggested();
    const wrap = document.querySelector('.suggested-grid-wrap');
    if (wrap) wrap.scrollTop = 0;
}

function toggleScoringGuide() {
    const el = document.getElementById('suggestedLegend');
    if (!el) return;
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function claimSuggestedAccount(id, name) {
    if (!confirm('Claim "' + name + '"? It will be added to your Accounts list.')) return;
    try {
        const result = await apiFetch('/api/prospects/suggested/' + id + '/claim', { method: 'POST' });
        const companyName = esc(result.company_name || name);
        const crmLink = result.company_id
            ? ' <a href="#" onclick="event.preventDefault();sidebarNav(\'customers\');setTimeout(function(){openCompanyDrawer(' + result.company_id + ')},300)" style="color:var(--blue);text-decoration:underline">Go to Account</a>'
            : '';
        showToast('Claimed: ' + companyName + crmLink, 'success', 5000);
        const card = document.getElementById('sg-card-' + id);
        if (card) { card.style.opacity = '0'; card.style.transition = 'opacity .3s'; setTimeout(() => card.remove(), 300); }
        try {
            const stats = await apiFetch('/api/prospects/suggested/stats');
            renderSuggestedStats(stats);
        } catch(e) {}
    } catch (e) {
        if (e.message && e.message.includes('409')) showToast('Already claimed by another user', 'error');
        else showToast(e.message || 'Failed to claim account', 'error');
    }
}

async function dismissSuggestedAccount(id, name, reason) {
    if (!reason) return;
    try {
        await apiFetch('/api/prospects/suggested/' + id + '/dismiss', { method: 'POST', body: { reason: reason } });
        showToast('Dismissed: ' + name, 'info');
        const card = document.getElementById('sg-card-' + id);
        if (card) { card.style.opacity = '0'; card.style.transition = 'opacity .3s'; setTimeout(() => card.remove(), 300); }
        try {
            const stats = await apiFetch('/api/prospects/suggested/stats');
            renderSuggestedStats(stats);
        } catch(e) {}
    } catch (e) {
        showToast(e.message || 'Failed to dismiss account', 'error');
    }
}

async function openSuggestedDetail(prospectId) {
    const backdrop = document.getElementById('suggestedDetailBackdrop');
    const drawer = document.getElementById('suggestedDetailDrawer');
    const body = document.getElementById('suggestedDetailBody');
    const title = document.getElementById('suggestedDetailTitle');
    if (backdrop) backdrop.classList.add('open');
    if (drawer) drawer.classList.add('open');
    if (body) body.innerHTML = '<div class="spinner-row"><div class="spinner"></div> Loading account intelligence…</div>';

    try {
        const detail = await apiFetch('/api/prospects/suggested/' + prospectId);
        if (title) title.textContent = detail.name || 'Account Detail';
        const mSugTitle = document.getElementById('suggestedDetailMobileTitle');
        if (mSugTitle) mSugTitle.textContent = detail.name || 'Account Detail';

        // Build the AI intelligence view
        let html = '';

        // Health badge
        const tier = detail.readiness_tier;
        const tierLabels = { call_now: 'Call Now', nurture: 'Nurture', monitor: 'Monitor' };
        const tierColor = tier === 'call_now' ? '#22c55e' : tier === 'nurture' ? '#eab308' : '#9ca3af';
        html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
            <span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:${tierColor}"></span>
            <span style="font-weight:600;font-size:14px">${tierLabels[tier] || tier}</span>
            <span style="color:var(--muted);font-size:12px">Fit: ${detail.fit_score}/100 · Readiness: ${detail.readiness_score}/100</span>
        </div>`;

        // Score bars
        html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
            <div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:2px"><span>Fit Score</span><span>${detail.fit_score}</span></div><div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden"><div style="width:${detail.fit_score}%;height:100%;background:var(--blue);border-radius:3px"></div></div></div>
            <div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:2px"><span>Readiness</span><span>${detail.readiness_score}</span></div><div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden"><div style="width:${detail.readiness_score}%;height:100%;background:${tierColor};border-radius:3px"></div></div></div>
        </div>`;

        // Company info
        html += '<div style="border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:12px">';
        const fields = [
            ['Industry', detail.industry],
            ['Size', detail.employee_count_range],
            ['Revenue', detail.revenue_range],
            ['Location', detail.hq_location],
            ['Region', detail.region],
            ['Domain', detail.domain ? `<a href="https://${detail.domain}" target="_blank" rel="noopener">${detail.domain}</a>` : null],
        ];
        for (const [label, val] of fields) {
            if (val) html += `<div style="display:flex;gap:8px;font-size:12px;padding:3px 0"><span style="color:var(--muted);min-width:80px">${label}</span><span>${val}</span></div>`;
        }
        html += '</div>';

        // Fit reasoning
        if (detail.fit_reasoning) {
            html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:12px;margin-bottom:4px">Fit Breakdown</div>';
            const parts = detail.fit_reasoning.split(';').map(p => p.trim()).filter(Boolean);
            html += '<div style="font-size:11px;color:var(--text)">' + parts.map(p => '<div style="padding:2px 0">' + esc(p) + '</div>').join('') + '</div></div>';
        }

        // AI Writeup
        if (detail.ai_writeup) {
            html += `<div style="background:var(--bg-alt,#f0f4ff);border-radius:var(--radius-sm);padding:12px;margin-bottom:12px;border-left:3px solid var(--blue)">
                <div style="font-weight:600;font-size:12px;margin-bottom:6px;color:var(--blue)">AI Analysis</div>
                <div style="font-size:12px;line-height:1.5">${esc(detail.ai_writeup)}</div>
            </div>`;
        }

        // Signals
        if (detail.signal_tags && detail.signal_tags.length) {
            html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:12px;margin-bottom:4px">Signals</div><div style="display:flex;flex-wrap:wrap;gap:4px">';
            for (const s of detail.signal_tags) {
                html += `<span class="signal-tag ${s.type}">${esc(s.label)}</span>`;
            }
            html += '</div></div>';
        }

        // Enrichment data
        const ed = detail.enrichment_data || {};

        // Warm intro
        const warmIntro = ed.warm_intro || {};
        if (warmIntro.has_warm_intro) {
            const warmColor = warmIntro.warmth === 'hot' ? '#166534' : '#854d0e';
            const warmBg = warmIntro.warmth === 'hot' ? '#dcfce7' : '#fef9c3';
            html += `<div style="background:${warmBg};border-radius:var(--radius-sm);padding:10px 12px;margin-bottom:12px;border-left:3px solid ${warmColor}">
                <div style="font-weight:600;font-size:12px;color:${warmColor};margin-bottom:4px">${warmIntro.warmth === 'hot' ? '🔥 Warm Introduction Available' : '👋 Prior Contact Detected'}</div>`;
            if (warmIntro.contacts && warmIntro.contacts.length) {
                for (const c of warmIntro.contacts.slice(0, 3)) {
                    html += `<div style="font-size:11px;padding:2px 0">${esc(c.name || '')} — ${esc(c.title || '')}${c.relationship_score ? ' (score: ' + c.relationship_score + ')' : ''}</div>`;
                }
            }
            if (warmIntro.sighting_count > 0) {
                html += `<div style="font-size:11px;padding:2px 0">${warmIntro.sighting_count} stock offers received from this domain</div>`;
            }
            if (warmIntro.engagement_score) {
                html += `<div style="font-size:11px;padding:2px 0">Engagement score: ${warmIntro.engagement_score}/100</div>`;
            }
            html += '</div>';
        }

        // SAM.gov data
        const samGov = ed.sam_gov || null;
        if (samGov) {
            html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:12px;margin-bottom:4px">Government Registration (SAM.gov)</div>';
            if (samGov.cage_code) html += '<div style="font-size:11px;padding:2px 0"><strong>CAGE Code:</strong> ' + esc(samGov.cage_code) + '</div>';
            if (samGov.uei) html += '<div style="font-size:11px;padding:2px 0"><strong>UEI:</strong> ' + esc(samGov.uei) + '</div>';
            if (samGov.entity_type) html += '<div style="font-size:11px;padding:2px 0"><strong>Type:</strong> ' + esc(samGov.entity_type) + '</div>';
            if (samGov.purpose) html += '<div style="font-size:11px;padding:2px 0"><strong>Purpose:</strong> ' + esc(samGov.purpose) + '</div>';
            if (samGov.naics_codes && samGov.naics_codes.length) {
                const naicsStr = samGov.naics_codes.map(n => n.code + (n.primary ? ' (primary)' : '')).join(', ');
                html += '<div style="font-size:11px;padding:2px 0"><strong>NAICS:</strong> ' + esc(naicsStr) + '</div>';
            }
            html += '</div>';
        }

        // Recent news
        const news = ed.recent_news || [];
        if (news.length) {
            html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:12px;margin-bottom:4px">Recent News</div>';
            for (const n of news.slice(0, 5)) {
                const typeColor = n.signal_type === 'funding' ? '#22c55e' : n.signal_type === 'acquisition' ? '#8b5cf6' : n.signal_type === 'expansion' ? '#3b82f6' : n.signal_type === 'contract' ? '#059669' : 'var(--muted)';
                const typeBadge = n.signal_type !== 'general' ? `<span style="font-size:9px;padding:1px 4px;border-radius:4px;background:${typeColor};color:white;margin-right:4px">${esc(n.signal_type)}</span>` : '';
                html += `<div style="padding:4px 0;border-top:1px solid var(--border);font-size:11px">
                    ${typeBadge}<a href="${esc(n.link)}" target="_blank" rel="noopener" style="color:var(--text)">${esc(n.title)}</a>
                    ${n.source ? '<div style="font-size:10px;color:var(--muted)">' + esc(n.source) + '</div>' : ''}
                </div>`;
            }
            html += '</div>';
        }

        // Historical context
        if (ed.historical_context || detail.historical_context) {
            const hc = detail.historical_context || {};
            if (Object.keys(hc).length) {
                html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:12px;margin-bottom:4px">Historical Context</div>';
                if (hc.bought_before) html += '<div style="font-size:11px;padding:2px 0;color:#22c55e">Previously purchased from Trio</div>';
                if (hc.quoted_before || hc.quote_count > 0) html += '<div style="font-size:11px;padding:2px 0">' + (hc.quote_count || 0) + ' previous quotes</div>';
                if (hc.last_activity) html += '<div style="font-size:11px;padding:2px 0">Last activity: ' + esc(String(hc.last_activity)) + '</div>';
                html += '</div>';
            }
        }

        // Contacts preview
        if (detail.contacts_count > 0) {
            html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:12px;margin-bottom:6px">Contacts (' + detail.contacts_count + ')</div>';
            html += '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">' + detail.contacts_verified + ' verified · ' + detail.contacts_decision_makers + ' decision makers</div>';
            for (const c of (detail.contacts_preview || [])) {
                const senBadge = c.seniority === 'decision_maker' ? ' <span style="color:var(--blue);font-size:9px;font-weight:600">DM</span>' : '';
                html += `<div style="padding:4px 0;border-top:1px solid var(--border);font-size:12px">
                    <strong>${esc(c.name || '—')}</strong>${senBadge}
                    ${c.title ? '<div style="color:var(--muted);font-size:11px">' + esc(c.title) + '</div>' : ''}
                    ${c.email_masked ? '<div style="font-size:10px;color:var(--muted)">' + esc(c.email_masked) + '</div>' : ''}
                </div>`;
            }
            html += '</div>';
        }

        // Similar customers
        if (detail.similar_customers && detail.similar_customers.length) {
            html += '<div style="margin-bottom:12px"><div style="font-weight:600;font-size:12px;margin-bottom:4px">Similar Trio Customers</div>';
            for (const s of detail.similar_customers) {
                html += `<div style="padding:4px 0;font-size:12px;border-top:1px solid var(--border)">
                    <strong>${esc(s.name || '')}</strong> <span style="font-size:10px;padding:2px 6px;border-radius:8px;background:${s.match_strength === 'strong' ? '#dcfce7' : s.match_strength === 'moderate' ? '#fef9c3' : '#f3f4f6'};color:var(--text)">${esc(s.match_strength || '')}</span>
                    <div style="font-size:11px;color:var(--muted)">${esc(s.match_reason || '')}</div>
                </div>`;
            }
            html += '</div>';
        }

        // Actions
        html += `<div style="display:flex;gap:8px;padding-top:12px;border-top:1px solid var(--border);flex-wrap:wrap">
            <button class="btn btn-primary" onclick="claimSuggestedAccount(${detail.id},'${escAttr(detail.name)}')">Claim Account</button>
            <button class="btn btn-ghost" onclick="enrichProspectFree(${detail.id})" id="enrichBtn-${detail.id}" title="SAM.gov + Google News (free)">Enrich</button>
            <select class="dismiss-select" onchange="dismissSuggestedAccount(${detail.id},'${escAttr(detail.name)}',this.value);if(this.value)closeSuggestedDetail();this.selectedIndex=0" style="font-size:12px">
                <option value="">Dismiss…</option>
                <option value="not_relevant">Not relevant</option>
                <option value="competitor">Competitor</option>
                <option value="too_small">Too small</option>
                <option value="too_large">Too large</option>
                <option value="other">Other</option>
            </select>
        </div>`;

        if (body) body.innerHTML = html;
    } catch (e) {
        if (body) body.innerHTML = '<div style="padding:20px;color:var(--red)">Failed to load account details</div>';
    }
}

function closeSuggestedDetail() {
    const backdrop = document.getElementById('suggestedDetailBackdrop');
    const drawer = document.getElementById('suggestedDetailDrawer');
    if (backdrop) backdrop.classList.remove('open');
    if (drawer) drawer.classList.remove('open');
}

async function enrichProspectFree(prospectId) {
    const btn = document.getElementById('enrichBtn-' + prospectId);
    if (btn) { btn.disabled = true; btn.textContent = 'Enriching…'; }
    try {
        const result = await apiFetch('/api/prospects/suggested/' + prospectId + '/enrich-free', { method: 'POST' });
        let msg = 'Enrichment complete';
        const parts = [];
        if (result.sam_gov) parts.push('SAM.gov data found');
        if (result.news_count > 0) parts.push(result.news_count + ' news articles');
        if (result.has_warm_intro) parts.push('warm intro detected');
        if (parts.length) msg += ': ' + parts.join(', ');
        showToast(msg, 'success');
        // Refresh the detail view
        openSuggestedDetail(prospectId);
    } catch (e) {
        showToast(e.message || 'Enrichment failed', 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Enrich'; }
    }
}


function _refreshCustPipeline() {
    if (_selectedCustId) _renderCustDrawerPipeline(_selectedCustId);
}

// ── API Health Dashboard ──────────────────────────────────────────────

function _timeAgo(iso) {
    if (!iso) return 'Never';
    const ms = Date.now() - new Date(iso).getTime();
    if (ms < 60000) return 'Just now';
    if (ms < 3600000) return Math.floor(ms / 60000) + 'm ago';
    if (ms < 86400000) return Math.floor(ms / 3600000) + 'h ago';
    return Math.floor(ms / 86400000) + 'd ago';
}

function showApiHealth() {
    openSettingsTab('apihealth');
}

async function loadApiHealthDashboard() {
    const container = document.getElementById('apiHealthDashboard');
    if (!container) return;
    container.textContent = '';
    const loading = document.createElement('p');
    loading.className = 'empty';
    loading.textContent = 'Loading...';
    container.appendChild(loading);
    try {
        const data = await apiFetch('/api/admin/api-health/dashboard');
        renderApiHealthDashboard(container, data.sources || []);
    } catch (e) {
        container.textContent = '';
        const errP = document.createElement('p');
        errP.className = 'empty';
        errP.textContent = 'Failed to load dashboard: ' + e.message;
        container.appendChild(errP);
    }
}

function refreshApiHealthDashboard() { loadApiHealthDashboard(); }

function renderApiHealthDashboard(container, sources) {
    const active = sources.filter(s => s.is_active && s.status !== 'disabled');
    const inactive = sources.filter(s => !s.is_active || s.status === 'disabled');
    const live = active.filter(s => s.status === 'live').length;
    const errors = active.filter(s => s.status === 'error').length;
    const degraded = active.filter(s => s.status === 'degraded').length;

    // All data values are escaped via esc() and numeric values are coerced to numbers.
    // This follows the established innerHTML pattern used throughout crm.js (e.g. renderCompanyCards,
    // _renderSourceCards) where esc() sanitizes all external strings.
    let html = '<div class="ahd-summary">';
    html += '<div class="ahd-stat live"><div class="ahd-stat-value">' + live + '</div><div class="ahd-stat-label">Live</div></div>';
    html += '<div class="ahd-stat degraded"><div class="ahd-stat-value">' + degraded + '</div><div class="ahd-stat-label">Degraded</div></div>';
    html += '<div class="ahd-stat error"><div class="ahd-stat-value">' + errors + '</div><div class="ahd-stat-label">Error</div></div>';
    html += '<div class="ahd-stat"><div class="ahd-stat-value">' + active.length + '</div><div class="ahd-stat-label">Total Active</div></div>';
    html += '</div>';

    html += '<div class="ahd-grid">';
    for (const src of active) {
        html += _renderHealthCard(src);
    }
    html += '</div>';

    const withQuota = active.filter(s => s.monthly_quota);
    if (withQuota.length > 0) {
        html += '<h3 style="font-size:14px;margin-bottom:8px">Usage Overview</h3>';
        html += '<div class="ahd-usage-section">';
        for (const src of withQuota) {
            const pct = src.usage_pct || 0;
            const cls = pct >= 90 ? 'critical' : pct >= 70 ? 'warn' : '';
            html += '<div class="ahd-usage-row">';
            html += '<span class="ahd-usage-name">' + esc(src.display_name) + '</span>';
            html += '<div class="ahd-usage-bar"><div class="ahd-usage-fill ' + cls + '" style="width:' + Math.min(Number(pct), 100) + '%"></div></div>';
            html += '<span class="ahd-usage-pct">' + Number(src.calls_this_month || 0) + '/' + Number(src.monthly_quota) + '</span>';
            html += '</div>';
        }
        html += '</div>';
    }

    if (inactive.length > 0) {
        html += '<details class="ahd-inactive"><summary>' + inactive.length + ' inactive sources</summary>';
        html += '<div class="ahd-inactive-list">';
        for (const src of inactive) {
            html += '<span class="ahd-inactive-chip">' + esc(src.display_name) + ' (' + esc(src.status) + ')</span>';
        }
        html += '</div></details>';
    }

    container.innerHTML = html;  // nosec: all dynamic values escaped via esc() or coerced to Number
}

function _renderHealthCard(src) {
    // All dynamic string values escaped via esc(); numeric values coerced to Number
    let html = '<div class="ahd-card">';
    html += '<div class="ahd-card-header">';
    html += '<span class="ahd-dot ' + esc(src.status) + '"></span>';
    html += '<span class="ahd-card-name">' + esc(src.display_name) + '</span>';
    html += '<span class="ahd-card-status">' + esc(src.status) + '</span>';
    html += '</div>';
    html += '<div class="ahd-card-meta">';
    if (src.last_success) html += '<div>Last success: <strong>' + esc(_timeAgo(src.last_success)) + '</strong></div>';
    if (src.last_error) html += '<div>Last error: <strong>' + esc(String(src.last_error).substring(0, 80)) + '</strong></div>';
    if (src.avg_response_ms) html += '<div>Avg response: <strong>' + Number(src.avg_response_ms) + 'ms</strong></div>';
    if (src.last_ping_at) html += '<div>Last check: <strong>' + esc(_timeAgo(src.last_ping_at)) + '</strong></div>';
    if (src.recent_checks > 0) {
        html += '<div>24h checks: ';
        const successes = Number(src.recent_checks) - Number(src.recent_failures || 0);
        for (let i = 0; i < Math.min(Number(src.recent_checks), 20); i++) {
            html += '<span class="ahd-mini-dot ' + (i < successes ? 'ok' : 'fail') + '"></span>';
        }
        html += '</div>';
    }
    html += '</div>';
    html += '<div class="ahd-card-actions">';
    html += '<button class="btn btn-xs" onclick="testSourceNow(' + Number(src.id) + ',this)">Test Now</button>';
    html += '</div>';
    html += '</div>';
    return html;
}

async function testSourceNow(sourceId, btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Testing...'; }
    try {
        const data = await apiFetch('/api/sources/' + Number(sourceId) + '/test', { method: 'POST' });
        showToast(data.status === 'live' ? 'API is live!' : 'Test result: ' + (data.error || data.status), data.status === 'live' ? 'success' : 'error');
        loadApiHealthDashboard();
    } catch (e) {
        showToast('Test failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Test Now'; }
    }
}

// ── Apollo Integration (company drawer tab) ──────────────────────────

let _apolloDiscoverResults = [];

async function _renderCustDrawerApollo(companyId) {
    const body = document.getElementById('custDrawerBody');
    if (!body) return;
    const c = crmCustomers.find(x => x.id === companyId);
    if (!c) { body.innerHTML = '<p class="crm-empty">Account not found</p>'; return; }

    if (!c.domain) {
        body.innerHTML = '<div class="drawer-section"><p class="crm-empty">No domain set for this company — add a domain in the Overview tab to use Apollo discovery.</p></div>';
        return;
    }

    _apolloDiscoverResults = [];

    let html = `<div class="drawer-section">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
            <div class="drawer-section-title" style="margin:0">Apollo Contact Discovery</div>
            <span id="apolloCreditsBadge" style="font-size:11px;color:var(--muted)">Credits: --</span>
        </div>
        <div style="display:flex;gap:8px;margin-bottom:12px">
            <button class="btn btn-primary btn-sm" onclick="apolloDiscover()">Find Procurement Contacts</button>
        </div>
        <div id="apolloDiscoverResults"></div>
        <div id="apolloEnrichResults"></div>
    </div>`;
    body.innerHTML = html;

    // Load credits in background
    _apolloLoadCredits();
}

async function _apolloLoadCredits() {
    try {
        const data = await apiFetch('/api/apollo/credits');
        const badge = document.getElementById('apolloCreditsBadge');
        if (!badge) return;
        if (data.note) {
            badge.textContent = 'Credits: unavailable';
            badge.title = data.note;
            badge.style.cursor = 'help';
        } else {
            badge.textContent = `Lead credits: ${data.lead_credits_remaining ?? '--'}`;
        }
    } catch (_) { /* ignore */ }
}

async function apolloDiscover() {
    const c = crmCustomers.find(x => x.id === _selectedCustId);
    if (!c || !c.domain) return;

    const container = document.getElementById('apolloDiscoverResults');
    if (!container) return;
    container.innerHTML = '<p class="crm-empty">Searching Apollo...</p>';

    try {
        const data = await apiFetch('/api/apollo/discover/' + encodeURIComponent(c.domain));
        _apolloDiscoverResults = data.contacts || [];

        if (!_apolloDiscoverResults.length) {
            container.innerHTML = '<p class="crm-empty">No procurement contacts found at ' + esc(c.domain) + '</p>';
            return;
        }

        let html = `<div style="margin-bottom:8px;display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:12px;color:var(--muted)">${data.total_found} contact${data.total_found !== 1 ? 's' : ''} found</span>
            <div style="display:flex;gap:8px">
                <label style="font-size:12px;display:flex;align-items:center;gap:4px;cursor:pointer">
                    <input type="checkbox" onchange="apolloToggleAll(this)" checked> Select all
                </label>
                <button class="btn btn-sm btn-primary" onclick="apolloEnrichSelected()">Enrich Selected</button>
            </div>
        </div>`;
        html += '<table class="crm-table" style="font-size:12px"><thead><tr>';
        html += '<th style="width:30px"></th><th>Name</th><th>Title</th><th>Seniority</th><th>Email (masked)</th>';
        html += '</tr></thead><tbody>';

        for (const ct of _apolloDiscoverResults) {
            html += `<tr>
                <td><input type="checkbox" class="apollo-cb" data-id="${esc(ct.apollo_id || '')}" checked></td>
                <td>${esc(ct.full_name || '')}</td>
                <td>${esc(ct.title || '')}</td>
                <td>${esc(ct.seniority || '')}</td>
                <td style="color:var(--muted)">${esc(ct.email_masked || 'N/A')}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        container.innerHTML = html;

    } catch (e) {
        container.innerHTML = '<p class="crm-empty" style="color:var(--red)">Apollo search failed: ' + esc(String(e.message || e)) + '</p>';
    }
}

function apolloToggleAll(master) {
    const checked = master.checked;
    document.querySelectorAll('.apollo-cb').forEach(cb => { cb.checked = checked; });
}

async function apolloEnrichSelected() {
    const checked = Array.from(document.querySelectorAll('.apollo-cb:checked'));
    const ids = checked.map(cb => cb.dataset.id).filter(Boolean);
    if (!ids.length) { alert('Select at least one contact to enrich.'); return; }

    const c = crmCustomers.find(x => x.id === _selectedCustId);
    if (!c) return;

    // Find vendor card by domain
    let vendorCardId = null;
    try {
        const vendors = await apiFetch('/api/vendor-cards?domain=' + encodeURIComponent(c.domain));
        if (vendors && vendors.length) vendorCardId = vendors[0].id;
    } catch (_) { /* no vendor card lookup available */ }

    if (!vendorCardId) {
        alert('No vendor card found for domain ' + (c.domain || '') + '. Create a vendor card first.');
        return;
    }

    if (!confirm(`Enrich ${ids.length} contact${ids.length !== 1 ? 's' : ''}? This will use ${ids.length} lead credit${ids.length !== 1 ? 's' : ''}.`)) return;

    const container = document.getElementById('apolloEnrichResults');
    if (!container) return;
    container.style.display = '';
    container.innerHTML = '<p class="crm-empty">Enriching contacts...</p>';

    try {
        const data = await apiFetch('/api/apollo/enrich', {
            method: 'POST',
            body: { apollo_ids: ids, vendor_card_id: vendorCardId },
        });

        let html = `<div style="margin-top:12px;padding:12px;background:var(--surface);border-radius:8px">
            <div style="font-weight:600;margin-bottom:8px">Enrichment Results</div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:8px">
                ${data.enriched} enriched &middot; ${data.verified} verified &middot; ${data.credits_used} credits used &middot; ${data.credits_remaining} remaining
            </div>`;

        if (data.contacts && data.contacts.length) {
            html += '<table class="crm-table" style="font-size:12px"><thead><tr>';
            html += '<th>Name</th><th>Title</th><th>Email</th><th>Phone</th><th>Verified</th>';
            html += '</tr></thead><tbody>';
            for (const ct of data.contacts) {
                html += `<tr>
                    <td>${esc(ct.full_name || '')}</td>
                    <td>${esc(ct.title || '')}</td>
                    <td>${esc(ct.email || 'N/A')}</td>
                    <td>${esc(ct.phone || '')}</td>
                    <td>${ct.is_verified ? '<span style="color:var(--green)">Yes</span>' : 'No'}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }
        html += '</div>';
        container.innerHTML = html;

        // Refresh credits badge
        _apolloLoadCredits();

    } catch (e) {
        container.innerHTML = '<p class="crm-empty" style="color:var(--red)">Enrichment failed: ' + esc(String(e.message || e)) + '</p>';
    }
}

// ── Mobile Offer Feed ─────────────────────────────────────────────────
// Cross-requisition offer feed for the mobile bottom-nav "Offers" tab.
// Fetches requisitions, then batch-loads offers for reqs that have them.

let _offerFeedData = [];       // Flat array of offer objects with req metadata
let _offerFeedFilter = 'pending'; // 'pending' | 'all' | 'accepted'
let _offerFeedLoading = false;

async function loadOfferFeed() {
    if (_offerFeedLoading) return;
    _offerFeedLoading = true;
    var listEl = document.getElementById('offerFeedList');
    var summaryEl = document.getElementById('offerFeedSummary');
    if (listEl) listEl.innerHTML = '<div class="spinner-row"><div class="spinner"></div>Loading offers\u2026</div>';
    if (summaryEl) summaryEl.innerHTML = '';
    try {
        // 1. Fetch requisitions to find which ones have offers
        var reqResp = await apiFetch('/api/requisitions?limit=200');
        var reqs = reqResp.requisitions || reqResp || [];
        var withOffers = reqs.filter(function(r) { return (r.offer_count || 0) > 0; });
        // 2. Batch-fetch offers for up to 20 reqs with most offers (sorted desc)
        var topReqs = withOffers
            .sort(function(a, b) { return (b.offer_count || 0) - (a.offer_count || 0); })
            .slice(0, 20);
        // Fetch in parallel with concurrency limit of 6
        var allOffers = [];
        var fetchChunks = [];
        for (var i = 0; i < topReqs.length; i += 6) fetchChunks.push(topReqs.slice(i, i + 6));
        for (var ci = 0; ci < fetchChunks.length; ci++) {
            var chunk = fetchChunks[ci];
            var results = await Promise.allSettled(
                chunk.map(function(r) {
                    return apiFetch('/api/requisitions/' + r.id + '/offers')
                        .then(function(data) { return { reqId: r.id, reqName: r.name, customer: r.customer_display, data: data }; });
                })
            );
            for (var ri = 0; ri < results.length; ri++) {
                if (results[ri].status !== 'fulfilled') continue;
                var val = results[ri].value;
                var groups = val.data.groups || [];
                for (var gi = 0; gi < groups.length; gi++) {
                    var grp = groups[gi];
                    var grpOffers = grp.offers || [];
                    for (var oi = 0; oi < grpOffers.length; oi++) {
                        allOffers.push(Object.assign({}, grpOffers[oi], {
                            _reqId: val.reqId,
                            _reqName: val.reqName || 'Untitled',
                            _customer: val.customer || '',
                            _targetQty: grp.target_qty,
                            _reqMpn: grp.mpn,
                        }));
                    }
                }
            }
        }
        // Sort by created_at descending (newest first)
        allOffers.sort(function(a, b) {
            var da = a.created_at ? new Date(a.created_at).getTime() : 0;
            var db2 = b.created_at ? new Date(b.created_at).getTime() : 0;
            return db2 - da;
        });
        _offerFeedData = allOffers;
        _renderOfferFeed();
    } catch (e) {
        logCatchError('loadOfferFeed', e);
        if (listEl) listEl.innerHTML = '<p class="empty" style="color:var(--red)">Failed to load offers</p>';
    } finally {
        _offerFeedLoading = false;
    }
}

function _renderOfferFeed(filter) {
    if (filter) _offerFeedFilter = filter;
    var listEl = document.getElementById('offerFeedList');
    var summaryEl = document.getElementById('offerFeedSummary');
    if (!listEl) return;

    var all = _offerFeedData;
    // Count by status category
    var pendingStatuses = ['active', 'pending_review'];
    var acceptedStatuses = ['won', 'sold'];
    var pendingCount = all.filter(function(o) { return pendingStatuses.indexOf(o.status || 'active') !== -1; }).length;
    var acceptedCount = all.filter(function(o) { return acceptedStatuses.indexOf(o.status) !== -1; }).length;
    var totalCount = all.length;

    // Update summary
    if (summaryEl) {
        summaryEl.innerHTML = '<div style="display:flex;gap:12px;padding:8px 0;font-size:12px;color:var(--muted)">'
            + '<span><b style="color:var(--amber)">' + pendingCount + '</b> pending</span>'
            + '<span><b style="color:var(--green)">' + acceptedCount + '</b> accepted</span>'
            + '<span><b>' + totalCount + '</b> total</span>'
            + '</div>';
    }

    // Update bottom nav badge
    var badge = document.getElementById('bnBadgeOffers');
    if (badge) {
        badge.textContent = pendingCount > 0 ? String(pendingCount) : '';
        badge.style.display = pendingCount > 0 ? '' : 'none';
    }

    // Apply filter
    var filtered = all;
    if (_offerFeedFilter === 'pending') {
        filtered = all.filter(function(o) { return pendingStatuses.indexOf(o.status || 'active') !== -1; });
    } else if (_offerFeedFilter === 'accepted') {
        filtered = all.filter(function(o) { return acceptedStatuses.indexOf(o.status) !== -1; });
    }
    // else 'all' — show everything

    if (!filtered.length) {
        var msg = _offerFeedFilter === 'pending' ? 'No pending offers'
            : _offerFeedFilter === 'accepted' ? 'No accepted offers'
            : 'No offers yet';
        listEl.innerHTML = '<p class="empty" style="padding:32px 16px;text-align:center;color:var(--muted)">' + esc(msg) + '</p>';
        return;
    }

    // Render offer cards
    var cards = filtered.map(function(o) {
        var price = o.unit_price != null ? '$' + Number(o.unit_price).toFixed(4) : '\u2014';
        var qty = o.qty_available != null ? Number(o.qty_available).toLocaleString() : '\u2014';
        var total = (o.unit_price != null && o.qty_available != null)
            ? '$' + (Number(o.unit_price) * Number(o.qty_available)).toFixed(2)
            : '\u2014';
        var dateStr = o.created_at ? fmtRelative(o.created_at) : '';
        var statusCls = acceptedStatuses.indexOf(o.status) !== -1 ? 'color:var(--green)'
            : o.status === 'expired' ? 'color:var(--muted)'
            : o.status === 'rejected' ? 'color:var(--red)'
            : 'color:var(--amber)';
        var statusLabel = (o.status || 'active').replace('_', ' ');

        return '<div class="m-card" style="margin-bottom:8px;padding:12px;cursor:pointer" '
            + 'onclick="sidebarNav(\'reqs\');setTimeout(function(){toggleDrillDown(' + o._reqId + ')},300)">'
            + '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">'
            + '<span style="font-weight:600;font-size:13px">' + esc(o.vendor_name || 'Unknown') + '</span>'
            + '<span style="font-size:10px;' + statusCls + ';text-transform:uppercase;font-weight:600">' + esc(statusLabel) + '</span>'
            + '</div>'
            + '<div style="font-size:12px;color:var(--text);margin-bottom:4px">'
            + '<span style="font-weight:500">' + esc(o.mpn || '') + '</span>'
            + '</div>'
            + '<div style="display:flex;gap:12px;font-size:11px;color:var(--muted);margin-bottom:6px">'
            + '<span>Qty: <b style="color:var(--text)">' + qty + '</b></span>'
            + '<span>Unit: <b style="color:var(--text)">' + price + '</b></span>'
            + '<span>Total: <b style="color:var(--text)">' + total + '</b></span>'
            + '</div>'
            + '<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted)">'
            + '<span title="' + escAttr(o._reqName) + '">' + esc(o._customer ? o._customer + ' \u2014 ' : '') + esc(o._reqName) + '</span>'
            + '<span>' + esc(dateStr) + '</span>'
            + '</div>'
            + '</div>';
    });

    listEl.innerHTML = cards.join('');
}

function _setOfferFeedFilterCrm(filter, btn) {
    // Update pill active states
    document.querySelectorAll('#offerFeedTabs .m-tab-pill').forEach(function(b) { b.classList.remove('active'); });
    if (btn) btn.classList.add('active');
    // Re-render with new filter
    _renderOfferFeed(filter);
}

// ── Mobile Bottom Sheet Helpers ───────────────────────────────────────
// Shared close helper — removes any open mobile bottom sheet from the DOM.

function _closeMobileSheet() {
    var bg = document.querySelector('.m-bottom-sheet-bg');
    if (bg) bg.remove();
}

// ── Mobile Quote Form ─────────────────────────────────────────────────
// Bottom sheet for creating/editing a quote on mobile.
// Shows accepted offers as checkboxes, markup %, live total, save/submit.

async function _openMobileQuoteForm(reqId) {
    _closeMobileSheet();
    if (!reqId) return;

    // Fetch offers for the requisition
    var offersData;
    try {
        offersData = await apiFetch('/api/requisitions/' + reqId + '/offers');
    } catch (e) {
        logCatchError('_openMobileQuoteForm', e);
        showToast('Failed to load offers', 'error');
        return;
    }

    var groups = offersData.groups || [];
    var allOffers = [];
    for (var gi = 0; gi < groups.length; gi++) {
        var grp = groups[gi];
        var offers = grp.offers || [];
        for (var oi = 0; oi < offers.length; oi++) {
            var o = offers[oi];
            if (o.status === 'expired' || o.status === 'rejected') continue;
            allOffers.push({
                id: o.id,
                mpn: o.mpn || grp.mpn || '',
                vendor_name: o.vendor_name || '',
                unit_price: o.unit_price,
                qty_available: o.qty_available || 0,
                manufacturer: o.manufacturer || '',
                lead_time: o.lead_time || ''
            });
        }
    }

    // Try to get customer name from the requisition
    var customerName = '';
    try {
        var reqInfo = await apiFetch('/api/requisitions/' + reqId);
        customerName = reqInfo.customer_display || reqInfo.customer_name || '';
    } catch (_e) { /* ignore */ }

    // Build offer checkboxes
    var offersHtml = '';
    if (!allOffers.length) {
        offersHtml = '<p style="color:var(--muted);text-align:center;padding:16px 0">No active offers available</p>';
    } else {
        for (var i = 0; i < allOffers.length; i++) {
            var of2 = allOffers[i];
            var priceStr = of2.unit_price != null ? '$' + Number(of2.unit_price).toFixed(4) : '\u2014';
            var qtyStr = of2.qty_available ? Number(of2.qty_available).toLocaleString() : '\u2014';
            offersHtml += '<label style="display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)">'
                + '<input type="checkbox" class="mq-offer-check" data-offer-id="' + of2.id + '" '
                + 'data-unit-price="' + (of2.unit_price || 0) + '" data-qty="' + (of2.qty_available || 0) + '" '
                + 'checked onchange="_mqUpdateTotals()" '
                + 'style="width:20px;height:20px;margin-top:2px;flex-shrink:0">'
                + '<div style="flex:1;min-width:0">'
                + '<div style="font-weight:600;font-size:13px">' + esc(of2.mpn) + '</div>'
                + '<div style="font-size:12px;color:var(--muted)">' + esc(of2.vendor_name) + (of2.manufacturer ? ' \u2014 ' + esc(of2.manufacturer) : '') + '</div>'
                + '<div style="font-size:12px;color:var(--text2)">Qty: ' + qtyStr + ' \u00b7 ' + priceStr + (of2.lead_time ? ' \u00b7 ' + esc(of2.lead_time) : '') + '</div>'
                + '</div>'
                + '</label>';
        }
    }

    // Build sheet HTML
    var bg = document.createElement('div');
    bg.className = 'm-bottom-sheet-bg';
    bg.addEventListener('click', function(e) { if (e.target === bg) _closeMobileSheet(); });

    var sheet = document.createElement('div');
    sheet.className = 'm-bottom-sheet';
    sheet.innerHTML = '<div class="m-swipe-handle" style="text-align:center;padding:8px 0">'
        + '<div style="width:36px;height:4px;background:var(--border);border-radius:2px;margin:0 auto"></div>'
        + '</div>'
        + '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0 12px">'
        + '<h3 style="margin:0;font-size:16px;font-weight:700">Build Quote</h3>'
        + '<button onclick="_closeMobileSheet()" style="background:none;border:none;font-size:22px;color:var(--muted);cursor:pointer;padding:4px 8px;line-height:1">\u00d7</button>'
        + '</div>'
        + '<div style="margin-bottom:12px">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Customer</label>'
        + '<input type="text" id="mqCustomerName" value="' + escAttr(customerName) + '" '
        + 'style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box" '
        + 'placeholder="Customer name" readonly>'
        + '</div>'
        + '<div style="margin-bottom:12px">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Select Offers</label>'
        + '<div id="mqOfferList" style="max-height:200px;overflow-y:auto;-webkit-overflow-scrolling:touch">'
        + offersHtml
        + '</div>'
        + '</div>'
        + '<div style="margin-bottom:12px">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Markup %</label>'
        + '<input type="number" id="mqMarkup" value="20" min="0" max="99" step="1" '
        + 'style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box" '
        + 'oninput="_mqUpdateTotals()">'
        + '</div>'
        + '<div id="mqTotalsRow" style="display:flex;justify-content:space-between;padding:10px 0;border-top:1px solid var(--border);margin-bottom:16px;font-size:13px">'
        + '<div>Cost: <strong id="mqCostTotal">$0.00</strong></div>'
        + '<div>Sell: <strong id="mqSellTotal">$0.00</strong></div>'
        + '<div>Margin: <strong id="mqMarginPct">0.0%</strong></div>'
        + '</div>'
        + '<button class="m-action-btn m-action-btn-ghost" onclick="_mqSaveDraft()" style="min-height:44px;font-size:16px">Save Draft</button>'
        + '<button class="m-action-btn m-action-btn-primary" onclick="_mqSubmitQuote()" style="min-height:44px;font-size:16px">Submit Quote</button>';

    bg.appendChild(sheet);
    document.body.appendChild(bg);

    // Trigger initial total calculation
    _mqUpdateTotals();
}

// Live total calculation for mobile quote sheet
function _mqUpdateTotals() {
    var checks = document.querySelectorAll('.mq-offer-check:checked');
    var markup = parseFloat(document.getElementById('mqMarkup')?.value) || 0;
    var totalCost = 0;
    for (var i = 0; i < checks.length; i++) {
        var price = parseFloat(checks[i].dataset.unitPrice) || 0;
        var qty = parseInt(checks[i].dataset.qty) || 0;
        totalCost += price * qty;
    }
    var totalSell = totalCost * (1 + markup / 100);
    var marginPct = totalSell > 0 ? ((totalSell - totalCost) / totalSell * 100) : 0;

    var costEl = document.getElementById('mqCostTotal');
    var sellEl = document.getElementById('mqSellTotal');
    var marginEl = document.getElementById('mqMarginPct');
    if (costEl) costEl.textContent = '$' + totalCost.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (sellEl) sellEl.textContent = '$' + totalSell.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (marginEl) marginEl.textContent = marginPct.toFixed(1) + '%';
}

// Save draft for mobile quote
async function _mqSaveDraft() {
    var checks = document.querySelectorAll('.mq-offer-check:checked');
    var offerIds = [];
    for (var i = 0; i < checks.length; i++) {
        offerIds.push(parseInt(checks[i].dataset.offerId));
    }
    if (!offerIds.length) { showToast('Select at least one offer', 'error'); return; }
    try {
        crmQuote = await apiFetch('/api/requisitions/' + currentReqId + '/quote', {
            method: 'POST', body: { offer_ids: offerIds }
        });
        // Apply markup to all line items
        var markup = parseFloat(document.getElementById('mqMarkup')?.value) || 0;
        if (crmQuote && crmQuote.line_items) {
            crmQuote.line_items.forEach(function(item) {
                item.sell_price = Number(item.cost_price || 0) * (1 + markup / 100);
                item.margin_pct = markup > 0 ? (markup / (100 + markup)) * 100 : 0;
            });
        }
        // Save the draft with updated prices
        await apiFetch('/api/quotes/' + crmQuote.id + '/draft', {
            method: 'PUT', body: { line_items: crmQuote.line_items }
        });
        showToast('Quote draft saved', 'success');
        _closeMobileSheet();
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) {
        logCatchError('_mqSaveDraft', e);
        showToast('Error saving draft: ' + (e.message || 'unknown'), 'error');
    }
}

// Submit quote for mobile
async function _mqSubmitQuote() {
    var checks = document.querySelectorAll('.mq-offer-check:checked');
    var offerIds = [];
    for (var i = 0; i < checks.length; i++) {
        offerIds.push(parseInt(checks[i].dataset.offerId));
    }
    if (!offerIds.length) { showToast('Select at least one offer', 'error'); return; }
    try {
        crmQuote = await apiFetch('/api/requisitions/' + currentReqId + '/quote', {
            method: 'POST', body: { offer_ids: offerIds }
        });
        // Apply markup
        var markup = parseFloat(document.getElementById('mqMarkup')?.value) || 0;
        if (crmQuote && crmQuote.line_items) {
            crmQuote.line_items.forEach(function(item) {
                item.sell_price = Number(item.cost_price || 0) * (1 + markup / 100);
                item.margin_pct = markup > 0 ? (markup / (100 + markup)) * 100 : 0;
            });
        }
        // Save draft first, then send
        await apiFetch('/api/quotes/' + crmQuote.id + '/draft', {
            method: 'PUT', body: { line_items: crmQuote.line_items }
        });
        showToast('Quote built \u2014 review and send from the Quote tab', 'success');
        notifyStatusChange(crmQuote);
        _closeMobileSheet();
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) {
        logCatchError('_mqSubmitQuote', e);
        showToast('Error building quote: ' + (e.message || 'unknown'), 'error');
    }
}

// ── Mobile Buy Plan Form ──────────────────────────────────────────────
// Bottom sheet for reviewing and submitting a buy plan on mobile.
// Shows selected offers summary with vendor/price/qty per line and total cost.

async function _openMobileBuyPlanForm(reqId) {
    _closeMobileSheet();
    if (!reqId) return;

    // Need a quote to build a buy plan
    if (!crmQuote) {
        showToast('Build a quote first before creating a buy plan', 'error');
        return;
    }
    if (crmQuote.status !== 'won') {
        showToast('Quote must be marked as Won to create a buy plan', 'error');
        return;
    }

    // Check for existing V3 buy plan
    var existingPlan = _currentBuyPlanV3;
    if (!existingPlan) {
        try {
            var resp = await apiFetch('/api/buy-plans-v3?quote_id=' + crmQuote.id);
            var plans = (resp.items || []);
            if (plans.length > 0) {
                existingPlan = await apiFetch('/api/buy-plans-v3/' + plans[0].id);
                _currentBuyPlanV3 = existingPlan;
            }
        } catch (_e) { /* ignore */ }
    }

    // If no existing plan, try to build one
    if (!existingPlan) {
        try {
            existingPlan = await apiFetch('/api/quotes/' + crmQuote.id + '/buy-plan-v3/build', { method: 'POST' });
            _currentBuyPlanV3 = existingPlan;
        } catch (e) {
            logCatchError('_openMobileBuyPlanForm', e);
            showToast('Failed to build buy plan: ' + (e.message || ''), 'error');
            return;
        }
    }

    var bp = existingPlan;
    var lines = bp.lines || [];

    // Build lines summary
    var linesHtml = '';
    var totalCost = 0;
    for (var i = 0; i < lines.length; i++) {
        var l = lines[i];
        var lineCost = (l.quantity || 0) * Number(l.unit_cost || 0);
        totalCost += lineCost;
        linesHtml += '<div style="display:flex;justify-content:space-between;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--border)">'
            + '<div style="flex:1;min-width:0">'
            + '<div style="font-weight:600;font-size:13px">' + esc(l.mpn || '') + '</div>'
            + '<div style="font-size:12px;color:var(--muted)">' + esc(l.vendor_name || '\u2014') + '</div>'
            + '</div>'
            + '<div style="text-align:right;flex-shrink:0;margin-left:12px">'
            + '<div style="font-weight:600;font-size:13px">$' + Number(l.unit_cost || 0).toFixed(4) + '</div>'
            + '<div style="font-size:12px;color:var(--muted)">Qty ' + (l.quantity || 0).toLocaleString() + '</div>'
            + '<div style="font-size:11px;color:var(--text2)">= $' + lineCost.toFixed(2) + '</div>'
            + '</div>'
            + '</div>';
    }

    if (!lines.length) {
        linesHtml = '<p style="color:var(--muted);text-align:center;padding:16px 0">No line items in this buy plan</p>';
    }

    // Financial summary
    var revenue = bp.total_revenue || 0;
    var profit = revenue - totalCost;
    var marginPct = revenue > 0 ? (profit / revenue * 100) : 0;

    // Build sheet
    var bg = document.createElement('div');
    bg.className = 'm-bottom-sheet-bg';
    bg.addEventListener('click', function(e) { if (e.target === bg) _closeMobileSheet(); });

    var sheet = document.createElement('div');
    sheet.className = 'm-bottom-sheet';
    sheet.innerHTML = '<div class="m-swipe-handle" style="text-align:center;padding:8px 0">'
        + '<div style="width:36px;height:4px;background:var(--border);border-radius:2px;margin:0 auto"></div>'
        + '</div>'
        + '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0 12px">'
        + '<h3 style="margin:0;font-size:16px;font-weight:700">Buy Plan</h3>'
        + '<button onclick="_closeMobileSheet()" style="background:none;border:none;font-size:22px;color:var(--muted);cursor:pointer;padding:4px 8px;line-height:1">\u00d7</button>'
        + '</div>'
        + (bp.customer_name ? '<div style="font-size:12px;color:var(--muted);margin-bottom:8px">Customer: <strong>' + esc(bp.customer_name) + '</strong></div>' : '')
        + (bp.quote_number ? '<div style="font-size:12px;color:var(--muted);margin-bottom:12px">Quote: <strong>' + esc(bp.quote_number) + '</strong></div>' : '')
        + (bp.ai_summary ? '<div style="background:#f0f9ff;padding:10px 12px;border-left:3px solid #2563eb;border-radius:4px;margin-bottom:12px;font-size:12px;line-height:1.5">'
            + '<strong style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#2563eb">AI Analysis</strong><br>'
            + esc(bp.ai_summary) + '</div>' : '')
        + '<div style="max-height:250px;overflow-y:auto;-webkit-overflow-scrolling:touch;margin-bottom:12px">'
        + linesHtml
        + '</div>'
        + '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:16px">'
        + '<div style="display:flex;justify-content:space-between;margin-bottom:4px;font-size:13px"><span>Total Cost</span><strong>$' + totalCost.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</strong></div>'
        + (revenue > 0 ? '<div style="display:flex;justify-content:space-between;margin-bottom:4px;font-size:13px"><span>Revenue</span><strong>$' + revenue.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</strong></div>'
            + '<div style="display:flex;justify-content:space-between;margin-bottom:4px;font-size:13px"><span>Profit</span><strong style="color:' + (profit >= 0 ? 'var(--green)' : 'var(--red)') + '">$' + profit.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</strong></div>'
            + '<div style="display:flex;justify-content:space-between;font-size:13px"><span>Margin</span><strong>' + marginPct.toFixed(1) + '%</strong></div>'
            : '')
        + '</div>'
        + (bp.status === 'draft'
            ? '<div style="margin-bottom:12px">'
              + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Acctivate SO# <span style="color:var(--red)">*</span></label>'
              + '<input type="text" id="mbpSoNum" placeholder="Enter SO#" '
              + 'style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box">'
              + '</div>'
              + '<div style="margin-bottom:16px">'
              + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Notes for Buyers</label>'
              + '<textarea id="mbpNotes" rows="2" placeholder="Special instructions\u2026" '
              + 'style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box"></textarea>'
              + '</div>'
              + '<button class="m-action-btn m-action-btn-primary" onclick="_mbpSubmit()" style="min-height:44px;font-size:16px">Submit Buy Plan</button>'
            : '<div style="text-align:center;padding:8px 0;font-size:13px;color:var(--muted)">Status: <strong style="color:' + _bpV3StatusColor(bp.status) + '">' + esc(_bpV3StatusLabel(bp.status)) + '</strong></div>');

    bg.appendChild(sheet);
    document.body.appendChild(bg);
}

// Submit buy plan from mobile sheet
async function _mbpSubmit() {
    if (!_currentBuyPlanV3 || _currentBuyPlanV3.status !== 'draft') return;

    var soNum = (document.getElementById('mbpSoNum')?.value || '').trim();
    if (!soNum) {
        showToast('Acctivate SO# is required', 'error');
        document.getElementById('mbpSoNum')?.focus();
        return;
    }

    var notes = (document.getElementById('mbpNotes')?.value || '').trim() || null;
    var btn = document.querySelector('.m-bottom-sheet .m-action-btn-primary');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting\u2026'; }

    try {
        var body = { sales_order_number: soNum, salesperson_notes: notes };
        var res = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id + '/submit', { method: 'POST', body: body });
        var msg = res.auto_approved ? 'Buy plan auto-approved \u2014 buyers notified!' : 'Buy plan submitted for approval';
        showToast(msg, 'success');
        _currentBuyPlanV3 = await apiFetch('/api/buy-plans-v3/' + _currentBuyPlanV3.id);
        _closeMobileSheet();
        renderBuyPlanV3Status();
    } catch (e) {
        logCatchError('_mbpSubmit', e);
        showToast('Failed to submit: ' + (e.message || e), 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Submit Buy Plan'; }
    }
}

// ── Mobile Log Offer — Bottom Sheet Form ──────────────────────────────
// Opens a mobile-optimised bottom sheet for logging an offer against a requisition.
// Called from the mobile drill-down offers tab (app.js) via window._openMobileOfferForm.

let _mobileOfferVendorCardId = null;
let _mobileOfferVendorDebounce = null;

async function _openMobileOfferForm(reqId) {
    _closeMobileSheet();
    if (!reqId) return;

    // Fetch requirements for part picker
    let reqs = [];
    try {
        reqs = await apiFetch('/api/requisitions/' + reqId + '/requirements');
    } catch (e) {
        logCatchError('_openMobileOfferForm', e);
        showToast('Failed to load parts', 'error');
    }

    _mobileOfferVendorCardId = null;

    // Build part options
    let partOptions = '<option value="">Select part...</option>';
    for (const r of (reqs || [])) {
        const mpn = r.primary_mpn || 'Part #' + r.id;
        const qty = r.target_qty ? ' (qty ' + Number(r.target_qty).toLocaleString() + ')' : '';
        partOptions += '<option value="' + r.id + '" data-mpn="' + escAttr(mpn) + '">'
            + esc(mpn) + qty + '</option>';
    }
    // Auto-select if only one part
    const autoSelect = reqs && reqs.length === 1 ? reqs[0].id : '';

    var bg = document.createElement('div');
    bg.className = 'm-bottom-sheet-bg';
    bg.addEventListener('click', function(e) { if (e.target === bg) _closeMobileOfferForm(); });

    var sheet = document.createElement('div');
    sheet.className = 'm-bottom-sheet';
    sheet.innerHTML = '<div style="text-align:center;padding:8px 0">'
        + '<div style="width:36px;height:4px;background:var(--border);border-radius:2px;margin:0 auto"></div>'
        + '</div>'
        + '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0 8px">'
        + '<h3 style="margin:0;font-size:16px;font-weight:700">Log Offer</h3>'
        + '<button onclick="_closeMobileOfferForm()" style="background:none;border:none;font-size:22px;color:var(--muted);cursor:pointer;padding:4px 8px;line-height:1">&times;</button>'
        + '</div>'
        + '<div style="font-size:12px;color:var(--muted);margin-bottom:12px">REQ-' + String(reqId).padStart(3, '0') + '</div>'
        + '<form id="mOfferForm" onsubmit="event.preventDefault();_submitMobileOffer(' + reqId + ')" autocomplete="off">'
        // Vendor
        + '<div style="margin-bottom:12px">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Vendor *</label>'
        + '<div style="position:relative">'
        + '<input id="moVendor" type="text" placeholder="Vendor name"'
        + ' style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box;font-family:inherit">'
        + '<div id="moVendorSuggestions" style="position:absolute;left:0;right:0;top:100%;z-index:310;background:var(--white);border:1px solid var(--border);border-radius:0 0 8px 8px;max-height:180px;overflow-y:auto;display:none;box-shadow:0 4px 12px rgba(0,0,0,.15)"></div>'
        + '</div>'
        + '</div>'
        // Part
        + '<div style="margin-bottom:12px">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Part *</label>'
        + '<select id="moPartSelect"'
        + ' style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box;font-family:inherit">'
        + partOptions + '</select>'
        + '</div>'
        // Qty + Price row
        + '<div style="display:flex;gap:12px;margin-bottom:12px">'
        + '<div style="flex:1">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Qty</label>'
        + '<input id="moQty" type="number" inputmode="numeric" placeholder="0"'
        + ' style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box;font-family:inherit">'
        + '</div>'
        + '<div style="flex:1">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Unit Price</label>'
        + '<input id="moPrice" type="number" inputmode="decimal" step="0.0001" placeholder="0.00"'
        + ' style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box;font-family:inherit">'
        + '</div>'
        + '</div>'
        // Lead Time
        + '<div style="margin-bottom:12px">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Lead Time</label>'
        + '<input id="moLead" type="text" placeholder="e.g. 2-3 weeks"'
        + ' style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box;font-family:inherit">'
        + '</div>'
        // Notes
        + '<div style="margin-bottom:16px">'
        + '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Notes</label>'
        + '<textarea id="moNotes" rows="2" placeholder="Optional notes"'
        + ' style="width:100%;font-size:16px;min-height:44px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input);box-sizing:border-box;font-family:inherit;resize:vertical"></textarea>'
        + '</div>'
        // Buttons
        + '<button id="moSubmitBtn" type="submit" class="m-action-btn m-action-btn-primary" style="min-height:44px;font-size:16px">Save Offer</button>'
        + '<button type="button" class="m-action-btn m-action-btn-ghost" onclick="_closeMobileOfferForm()" style="min-height:44px;font-size:16px">Cancel</button>'
        + '</form>';

    bg.appendChild(sheet);
    document.body.appendChild(bg);

    // Auto-select single part
    if (autoSelect) {
        var sel = document.getElementById('moPartSelect');
        if (sel) sel.value = String(autoSelect);
    }

    // Init vendor autocomplete
    _initMoVendorAutocomplete();

    // Focus vendor input after animation
    setTimeout(function() {
        var inp = document.getElementById('moVendor');
        if (inp) inp.focus();
    }, 300);
}

function _closeMobileOfferForm() {
    var bg = document.querySelector('.m-bottom-sheet-bg');
    if (!bg) return;
    var sheet = bg.querySelector('.m-bottom-sheet');
    if (sheet) {
        sheet.style.transform = 'translateY(100%)';
        sheet.style.transition = 'transform .2s ease-in';
        setTimeout(function() { bg.remove(); }, 200);
    } else {
        bg.remove();
    }
}

function _initMoVendorAutocomplete() {
    var input = document.getElementById('moVendor');
    var dropdown = document.getElementById('moVendorSuggestions');
    if (!input || !dropdown) return;

    input.addEventListener('input', function() {
        clearTimeout(_mobileOfferVendorDebounce);
        _mobileOfferVendorCardId = null;
        var q = input.value.trim();
        if (q.length < 2) { dropdown.style.display = 'none'; return; }
        _mobileOfferVendorDebounce = setTimeout(function() {
            _moVendorSearch(q);
        }, 250);
    });

    // Dismiss dropdown on outside tap
    document.addEventListener('click', function _moDocClick(e) {
        if (!dropdown.contains(e.target) && e.target !== input) {
            dropdown.style.display = 'none';
        }
        // Clean up listener when sheet is gone
        if (!document.getElementById('moVendor')) {
            document.removeEventListener('click', _moDocClick);
        }
    });
}

async function _moVendorSearch(q) {
    var dropdown = document.getElementById('moVendorSuggestions');
    var input = document.getElementById('moVendor');
    if (!dropdown || !input) return;
    try {
        var data = await apiFetch('/api/autocomplete/names?q=' + encodeURIComponent(q) + '&limit=8');
        var vendors = (data || []).filter(function(r) { return r.type === 'vendor' && r.id && r.name; });
        if (!vendors.length) {
            dropdown.innerHTML = '<div style="padding:10px 12px;font-size:13px;color:var(--muted)">No vendors found</div>';
            dropdown.style.display = 'block';
            return;
        }
        dropdown.innerHTML = vendors.map(function(v) {
            return '<div data-id="' + v.id + '" style="padding:12px;font-size:14px;cursor:pointer;border-bottom:1px solid var(--border);-webkit-tap-highlight-color:transparent">'
                + esc(v.name) + '</div>';
        }).join('');
        dropdown.style.display = 'block';
        dropdown.querySelectorAll('[data-id]').forEach(function(item) {
            item.addEventListener('click', function() {
                input.value = item.textContent;
                _mobileOfferVendorCardId = parseInt(item.dataset.id) || null;
                dropdown.style.display = 'none';
            });
        });
    } catch (e) {
        dropdown.style.display = 'none';
    }
}

async function _submitMobileOffer(reqId) {
    var _v = function(id) { return (document.getElementById(id) || {}).value || ''; };
    var vendor = _v('moVendor').trim();
    if (!vendor) { showToast('Vendor name is required', 'error'); return; }

    var partSel = document.getElementById('moPartSelect');
    var reqPartId = partSel && partSel.value ? parseInt(partSel.value) : null;
    var mpn = partSel && partSel.selectedOptions[0]
        ? (partSel.selectedOptions[0].dataset.mpn || partSel.selectedOptions[0].textContent || '')
        : '';
    if (!mpn || !reqPartId) { showToast('Select a part', 'error'); return; }

    var btn = document.getElementById('moSubmitBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }

    try {
        var body = {
            mpn: mpn,
            vendor_name: vendor,
            vendor_card_id: _mobileOfferVendorCardId || null,
            requirement_id: reqPartId,
            qty_available: parseInt(_v('moQty')) || null,
            unit_price: parseFloat(_v('moPrice')) || null,
            lead_time: _v('moLead').trim() || null,
            notes: _v('moNotes').trim() || null,
            source: 'manual',
            status: 'active',
        };

        await apiFetch('/api/requisitions/' + reqId + '/offers', { method: 'POST', body: body });

        _closeMobileOfferForm();
        showToast('Offer logged', 'success');

        // Invalidate caches so offers tab refreshes
        if (window._ddTabCache && window._ddTabCache[reqId]) {
            delete window._ddTabCache[reqId].offers;
        }

        // Refresh the mobile drill-down offers tab if visible
        var panel = document.getElementById('mobileDdPanel');
        if (panel && typeof window._loadDdSubTab === 'function') {
            window._loadDdSubTab(reqId, 'offers', panel);
        }
    } catch (e) {
        logCatchError('_submitMobileOffer', e);
        showToast('Failed to log offer: ' + (e.message || e), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Save Offer'; }
    }
}

// ── ESM: expose all inline-handler functions to window ────────────────
Object.assign(window, {
    _refreshCustPipeline,
    _debouncedFilterSiteContacts,
    _debouncedFilterDrawerContacts, _debouncedLoadVendorScorecards,
    _debouncedUpdateBpTotals, _debouncedUpdateProactivePreview,
    _toggleActivityDetail,
    applyMarkup, approveBuyPlan, approveBuyPlanV3, approveEnrichItem,
    autoCreateSiteAndSelect, autoLogCrmCall,
    browseOneDrive, cancelBuyPlan, cancelCredEdit, cancelEnrichJob, confirmPOV3,
    completeBuyPlan, convertProactiveOffer,
    copyQuoteTable, deleteAIContact, deleteAdminUser, deleteCredential,
    deleteOffer, deleteOfferAttachment, deleteSiteContact,
    dismissProactiveGroup, editCredential,
    eqToggleAll, eqToggleItem, loadBuyPlans, loadBuyPlanV3, loadBuyerLeaderboard, toggleBpMyOnly,
    loadQuote, loadSpecificQuote, loadSalespersonScorecard,
    markQuoteResult, onSourcesSearch,
    openAddSiteContact, openAddSiteModal, openBuyPlanDetailV3,
    openFlagIssueV3, openHaltSOV3,
    openOfferComparisonV3, openRejectBuyPlanV3, openRejectPOV3, openRejectSOV3,
    openEditCompany, openEditOffer, openEditSiteContact,
    openEditSiteModal, openLogNoteModal, openLostModal,
    openOfferGallery, openPricingHistory, openProactiveSendModal,
    openRejectBuyPlanModal, quickCreateCompany,
    refreshBuyerLeaderboard, refreshTeamsChannels,
    refreshVendorScorecards, rejectBuyPlan, rejectBuyPlanV3, resubmitBuyPlanV3,
    rejectEnrichItem, reopenQuote, resubmitBuyPlan, reviseQuote,
    saveAIContact, saveBuyPlanPOs, saveConfig, saveCredential,
    saveParsedOffers, saveQuoteDraft, saveTeamsConfig, scToggle,
    selectOneDriveFile, selectSite, sendQuoteEmail, setOfferFilter,
    setOfferSort, setSourcesFilter, showView, sortBpList, sortCustList,
    sortSalesScorecard, testSourceCred, testTeamsPost,
    toggleOfferSelect, togglePlannedSources, toggleSiteDetail,
    toggleSourceStatus, tokenApprovePlan, tokenRejectPlan,
    triggerDeepScan, unifiedEnrichCompany, updateQuoteLine, verifyContactEmail,
    loadCreditUsage, loadCustomerGaps, startCustomerBackfill,
    updateQuoteLineField, updateUserField,
    verifyBuyPlanPOs, verifyPOV3, verifySOV3,
    openSuggestedContacts,
    // HTML template inline handlers
    addSelectedSuggestedContacts, addSite, bulkApproveSelected,
    confirmSendQuote, createCompany, createUser,
    filterSiteTypeahead,
    loadCustomers, loadEnrichmentQueue, onSqContactChange,
    debouncedCheckDupCompany, openNewCompanyModal, openNewVendorModal, renderBuyPlansList, saveEditCompany,
    saveLogCall, saveLogNote, saveSiteContact, searchSuggestedContacts,
    sendProactiveOffer, setBpFilter, startBackfill, startEmailBackfill,
    startWebsiteScrape, submitBuyPlan, submitBuyPlanV3, submitFlagIssueV3, submitLost, swapLineOfferV3, switchEnrichTab,
    switchProactiveTab, switchSettingsTab,
    toggleCustUnassigned, updateOffer,
    selectCustomer, renderCustomerDetail, saveCustNotes,
    logCustNote, saveContactNotes, logContactNote, _loadContactRecentNotes, toggleContactArchive,
    toggleStrategic, _renderMiniList, _renderMiniListFromSearch, _miniListKeyNav,
    _setTopViewLabel, _setTopDrillLabel, _abortAllCrmFetches,
    analyzeCustomerTags, setCustFilter, setCustOwnerFilter, openCustDrawer, closeCustDrawer, switchCustDrawerTab,
    toggleCustCheckbox, toggleAllCustCheckboxes, clearCustSelection,
    bulkAssignOwner, bulkExportAccounts, toggleSiteAccordion,
    // Suggested accounts (company-level pool)
    showSuggested, loadSuggested, debouncedLoadSuggested, setSuggestedReadiness,
    claimSuggestedAccount, dismissSuggestedAccount, suggestedGoPage, toggleScoringGuide,
    openSuggestedDetail, closeSuggestedDetail, enrichProspectFree,
    // Account transfer
    loadTransferPanel, loadTransferPreview, toggleTransferSite,
    toggleTransferSelectAll, updateTransferSelectedCount, executeTransfer,
    // Cross-file calls from app.js
    goToCompany, showBuyPlans, showCustomers, showPerformance,
    showProactiveOffers, showSettings, loadSettingsProfile,
    // Proactive UI functions called from HTML onclick
    switchProactiveTab, openProactiveSendModal, dismissProactiveGroup,
    dismissSingleMatch, toggleProactiveGroup, refreshProactiveMatches,
    toggleProactiveSelect, toggleAllProactiveInGroup, doNotOfferMatch, doNotOfferSelected,
    sendProactiveOffer, convertProactiveOffer, updateProactivePreview, generateProactiveDraft,
    loadProactiveScorecard,
    loadAvailScores,
    invalidateCompanyCache,
    // API Health Dashboard
    showApiHealth, loadApiHealthDashboard, refreshApiHealthDashboard,
    renderApiHealthDashboard, testSourceNow,
    // Apollo integration
    apolloDiscover, apolloEnrichSelected, apolloToggleAll,
    // Mobile account & contact rendering
    renderMobileAccountList, _renderMobileContact,
    // Mobile offer feed
    loadOfferFeed, _renderOfferFeed, _setOfferFeedFilter: _setOfferFeedFilterCrm,
    // Mobile quote & buy plan bottom sheets
    _openMobileQuoteForm, _openMobileBuyPlanForm, _closeMobileSheet,
    _mqUpdateTotals, _mqSaveDraft, _mqSubmitQuote, _mbpSubmit,
    // Mobile log offer bottom sheet
    _openMobileOfferForm, _submitMobileOffer, _closeMobileOfferForm,
});
