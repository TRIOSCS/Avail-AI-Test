/* AVAIL v1.2.0 — CRM Extension: Customers, Offers, Quotes */

import {
    apiFetch, debounce, esc, escAttr, logCatchError, showToast,
    fmtDate, fmtDateTime, fmtRelative, openModal, closeModal,
    showView, sidebarNav, navHighlight, autoLogEmail,
    initNameAutocomplete, notifyStatusChange, loadRequisitions,
    toggleDrillDown, guardBtn, openVendorPopup,
    loadVendorContacts, refreshProactiveBadge,
} from 'app';

// ── Debounced CRM Handlers ─────────────────────────────────────────────
const _debouncedFilterSiteContacts = debounce((input, siteId) => filterSiteContacts(input, siteId), 150);
const _debouncedUpdateBpTotals = debounce(() => updateBpTotals(), 150);
const _debouncedUpdateProactivePreview = debounce(() => updateProactivePreview(), 150);
const _debouncedLoadVendorScorecards = debounce(() => loadVendorScorecards(), 300);

// ── CRM State ──────────────────────────────────────────────────────────
let crmCustomers = [];
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

function autoLogCrmCall(phone) {
    apiFetch('/api/activities/call', {
        method: 'POST', body: { phone: phone, direction: 'outbound' }
    }).catch(function(e) { logCatchError('autoLogCrmCall', e); });
}

// ── Customer Filter / Sort Helpers ─────────────────────────────────────
function toggleCustUnassigned(btn) {
    _custUnassigned = !_custUnassigned;
    btn.classList.toggle('on', _custUnassigned);
    loadCustomers();
}

// sortCustomers removed — now handled by sortCustList() with column headers

// ── Customers View ─────────────────────────────────────────────────────

async function showCustomers() {
    showView('view-customers');
    currentReqId = null;
    // Role-based account filtering
    const isManagerOrAdmin = window.__isAdmin || ['manager','trader'].includes(window.userRole);
    const isSalesOnly = window.userRole === 'sales';
    const toggleLabel = document.getElementById('custMyOnlyLabel');
    const toggleInput = document.getElementById('custMyOnly');
    if (toggleLabel && toggleInput) {
        if (isManagerOrAdmin) {
            toggleLabel.style.display = '';  // Show toggle for managers/admins/traders
        } else {
            toggleLabel.style.display = 'none';  // Hide for sales — forced to my accounts
            toggleInput.checked = true;
        }
    }
    await loadCustomers();
}

let _custAbort = null;
async function loadCustomers() {
    if (_custAbort) { try { _custAbort.abort(); } catch(e){} }
    _custAbort = new AbortController();
    var cl = document.getElementById('custList');
    if (cl && (!crmCustomers || !crmCustomers.length)) cl.innerHTML = '<div class="spinner-row"><div class="spinner"></div>Loading companies…</div>';
    try {
        const filter = document.getElementById('custFilter')?.value || '';
        const isSalesOnly = window.userRole === 'sales';
        const myOnly = document.getElementById('custMyOnly')?.checked;
        let url = '/api/companies?search=' + encodeURIComponent(filter);
        if ((isSalesOnly || myOnly) && window.userId) url += '&owner_id=' + window.userId;
        if (_custUnassigned) url += '&unassigned=1';
        const result = await apiFetch(url, {signal: _custAbort.signal});
        crmCustomers = result;
        renderCustomers();
    } catch (e) { if (e.name === 'AbortError') return; showToast('Failed to load customers', 'error'); console.error(e); }
}

async function goToCompany(companyId) {
    if (!companyId) return;
    showView('view-customers');
    currentReqId = null;
    try {
        crmCustomers = await apiFetch('/api/companies');
        renderCustomers();
    } catch (e) { showToast('Failed to load customers', 'error'); return; }
    // Find and expand the target company drill-down row
    setTimeout(() => {
        const drow = document.getElementById('cd-' + companyId);
        if (drow) {
            const dataRow = drow.previousElementSibling;
            if (dataRow) dataRow.scrollIntoView({ behavior: 'smooth', block: 'start' });
            if (!drow.classList.contains('open')) {
                toggleCustDrill(companyId);
            }
        }
    }, 100);
    navHighlight(document.getElementById('navCustomers'));
}

function renderCustomers() {
    const el = document.getElementById('custList');
    if (!el) return;
    const countEl = document.getElementById('custFilterCount');
    if (!crmCustomers.length) {
        el.innerHTML = '<p class="empty">No customers yet \u2014 add a company to get started</p>';
        if (countEl) countEl.textContent = '';
        return;
    }
    // Sort
    const sorted = [...crmCustomers];
    if (_custSortCol) {
        sorted.sort((a, b) => {
            let va, vb;
            switch (_custSortCol) {
                case 'name': va = (a.name || ''); vb = (b.name || ''); break;
                case 'type': va = (a.account_type || ''); vb = (b.account_type || ''); break;
                case 'industry': va = (a.industry || ''); vb = (b.industry || ''); break;
                case 'sites': va = a.site_count || 0; vb = b.site_count || 0; break;
                case 'owner': va = (a.account_owner_name || ''); vb = (b.account_owner_name || ''); break;
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _custSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _custSortDir === 'asc' ? va - vb : vb - va;
        });
    } else {
        sorted.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    }

    if (countEl) countEl.textContent = sorted.length + ' companies';

    const thC = (col) => _custSortCol === col ? ' class="sorted"' : '';
    const sa = (col) => `<span class="sort-arrow">${_custSortArrow(col)}</span>`;

    let html = `<div style="padding:0 16px"><table class="tbl"><thead><tr>
        <th style="width:30px"></th>
        <th onclick="sortCustList('name')"${thC('name')}>Company ${sa('name')}</th>
        <th onclick="sortCustList('type')"${thC('type')}>Type ${sa('type')}</th>
        <th onclick="sortCustList('industry')"${thC('industry')}>Industry ${sa('industry')}</th>
        <th onclick="sortCustList('sites')"${thC('sites')}>Sites ${sa('sites')}</th>
        <th onclick="sortCustList('owner')"${thC('owner')}>Owner ${sa('owner')}</th>
        <th>Health</th>
        <th style="width:90px">Actions</th>
    </tr></thead><tbody>`;

    for (const c of sorted) {
        const displayName = c.name.replace(/\s*(bucket|pass)\s*$/i, '').trim();
        const domain = c.domain || (c.website ? c.website.replace(/https?:\/\/(www\.)?/, '').split('/')[0] : '');
        const ownerHtml = c.account_owner_name
            ? esc(c.account_owner_name)
            : '<span style="color:var(--red);font-weight:600">unassigned</span>';

        html += `<tr onclick="toggleCustDrill(${c.id})" data-company-id="${c.id}">
            <td><button class="ea" id="ca-${c.id}">\u25b6</button></td>
            <td><b class="cust-link">${esc(displayName)}</b>${domain ? '<br><span style="font-size:10px;color:var(--muted)">' + esc(domain) + '</span>' : ''}</td>
            <td>${c.account_type ? '<span class="badge b-src">' + esc(c.account_type) + '</span>' : '\u2014'}</td>
            <td style="font-size:11px">${esc(c.industry || '\u2014')}</td>
            <td class="mono">${c.site_count || 0}</td>
            <td>${ownerHtml}</td>
            <td><span id="actHealth-${c.id}"></span></td>
            <td style="white-space:nowrap" onclick="event.stopPropagation()">
                <button class="btn-enrich" onclick="openEditCompany(${c.id})">Edit</button>
                <button class="btn-enrich" onclick="unifiedEnrichCompany(${c.id})">Enrich</button>
            </td>
        </tr>
        <tr class="drow" id="cd-${c.id}"><td colspan="8">
            <div id="cdInner-${c.id}"><p class="empty" style="padding:4px;font-size:11px">Loading...</p></div>
        </td></tr>`;
    }

    html += '</tbody></table></div>';
    el.innerHTML = html;
}

function toggleCustDrill(companyId) {
    const drow = document.getElementById('cd-' + companyId);
    const arrow = document.getElementById('ca-' + companyId);
    if (!drow) return;
    const opening = !drow.classList.contains('open');
    drow.classList.toggle('open');
    if (arrow) arrow.classList.toggle('open');
    if (opening) {
        _loadCustDrillContent(companyId);
        loadCompanyActivityStatus(companyId);
    }
}

async function _loadCustDrillContent(companyId) {
    const inner = document.getElementById('cdInner-' + companyId);
    if (!inner || inner.dataset.loaded) return;
    const c = crmCustomers.find(x => x.id === companyId);
    if (!c) return;

    // Enrichment tags
    const acctTags = [
        c.employee_size ? '<span class="enrich-tag">\ud83d\udc65 ' + esc(c.employee_size) + '</span>' : '',
        c.hq_city ? '<span class="enrich-tag">\ud83d\udccd ' + esc(c.hq_city) + (c.hq_state ? ', ' + esc(c.hq_state) : '') + '</span>' : '',
        c.phone ? '<span class="enrich-tag">\ud83d\udcde ' + esc(c.phone) + '</span>' : '',
        c.credit_terms ? '<span class="enrich-tag">' + esc(c.credit_terms) + '</span>' : '',
        c.linkedin_url ? '<a href="' + escAttr(c.linkedin_url) + '" target="_blank" style="color:var(--teal);text-decoration:none;font-size:10px">LinkedIn \u2197</a>' : '',
    ].filter(Boolean).join('');
    const enrichHtml = acctTags ? '<div class="enrich-bar" style="margin-bottom:8px">' + acctTags + '</div>' : '';

    // Sites sub-table
    const sites = c.sites || [];
    let sitesHtml = '';
    if (sites.length) {
        sitesHtml = `<table class="dtbl"><thead><tr><th>Site</th><th>Owner</th><th>Open Reqs</th></tr></thead><tbody>`;
        for (const s of sites) {
            sitesHtml += `<tr style="cursor:pointer" onclick="toggleSiteDetail(${s.id})">
                <td style="font-weight:600">${esc(s.site_name)}</td>
                <td>${esc(s.owner_name || '\u2014')}</td>
                <td class="mono">${s.open_reqs || 0}</td>
            </tr>
            <tr><td colspan="3"><div id="siteDetail-${s.id}" class="site-detail-panel" style="display:none"></div></td></tr>`;
        }
        sitesHtml += '</tbody></table>';
    }
    sitesHtml += `<button class="btn btn-ghost btn-sm" style="margin-top:6px" onclick="event.stopPropagation();openAddSiteModal(${c.id},'${escAttr(c.name)}')">+ Add Site</button>`;

    // Activity section
    const actHtml = `<div class="cust-activity-section" style="margin-top:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <span class="si-contacts-title">Recent Activity</span>
            <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openLogNoteModal(${c.id},'${escAttr(c.name)}')">+ Note</button>
        </div>
        <div id="actList-${c.id}"><p class="empty" style="padding:4px;font-size:11px">Loading...</p></div>
    </div>`;

    inner.innerHTML = enrichHtml + sitesHtml + actHtml;
    inner.dataset.loaded = '1';
    loadCompanyActivities(companyId);
}

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
            const renderContact = c => `
                <div class="si-contact-card" data-contact-search="${escAttr((c.full_name + ' ' + (c.title || '') + ' ' + (c.email || '')).toLowerCase())}">
                    <div class="si-contact-left">
                        <div class="si-contact-avatar">${esc((c.full_name || '?')[0].toUpperCase())}</div>
                    </div>
                    <div class="si-contact-info">
                        <div class="si-contact-row1">
                            <span class="si-contact-name">${esc(c.full_name)}</span>
                            ${c.is_primary ? '<span class="si-contact-badge">Primary</span>' : ''}
                        </div>
                        ${c.title ? '<div class="si-contact-title">' + esc(c.title) + '</div>' : ''}
                        <div class="si-contact-meta">
                            ${c.email ? '<a href="mailto:'+esc(c.email)+'" title="'+escAttr(c.email)+'" onclick="autoLogEmail(\''+escAttr(c.email)+'\',\''+escAttr(c.full_name || '')+'\')">'+esc(c.email)+'</a>' : ''}
                            ${c.phone ? '<a href="tel:'+escAttr(c.phone)+'" class="si-contact-phone" onclick="autoLogCrmCall(\''+escAttr(c.phone)+'\')">'+esc(c.phone)+'</a>' : ''}
                        </div>
                        ${c.notes ? '<div class="si-contact-notes">'+esc(c.notes)+'</div>' : ''}
                    </div>
                    <div class="si-contact-actions">
                        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openEditSiteContact(${s.id},${c.id})">Edit</button>
                        <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteSiteContact(${s.id},${c.id},'${escAttr(c.full_name)}')">✕</button>
                    </div>
                </div>`;
            const searchBar = contacts.length > 5
                ? `<input class="si-contact-search" placeholder="Filter contacts…" oninput="_debouncedFilterSiteContacts(this,${s.id})">`
                : '';
            const contactsHtml = contacts.length
                ? `${searchBar}<div class="si-contact-grid" id="contactGrid-${s.id}">${sorted.map(renderContact).join('')}</div>`
                : '<p class="empty" style="padding:4px;font-size:11px">No contacts — add one below</p>';
            panel.innerHTML = `
            <div class="site-info">
                <div class="si-row"><span class="si-label">Owner</span><span>${esc(s.owner_name || '—')}</span></div>
                <div class="si-contacts">
                    <div style="display:flex;align-items:center;justify-content:space-between">
                        <div class="si-contacts-title">Contacts <span style="font-weight:400;color:var(--muted)">(${contacts.length})</span></div>
                        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openAddSiteContact(${s.id})">+ Add</button>
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
    openModal('newCompanyModal', 'ncName');
}

const debouncedCheckDupCompany = debounce(async (val) => {
    const warn = document.getElementById('ncDupWarning');
    if (!warn) return;
    const q = (val || '').trim();
    if (q.length < 3) { warn.style.display = 'none'; return; }
    try {
        const resp = await apiFetch('/api/companies/check-duplicate?name=' + encodeURIComponent(q));
        if (resp.matches && resp.matches.length > 0) {
            const names = resp.matches.map(m =>
                '<b>' + esc(m.name) + '</b>' + (m.match === 'exact' ? ' (exact match)' : '')
            ).join(', ');
            warn.innerHTML = 'Possible duplicate: ' + names;
            warn.style.display = '';
        } else {
            warn.style.display = 'none';
        }
    } catch (e) { warn.style.display = 'none'; }
}, 400);

async function createCompany() {
    const name = document.getElementById('ncName').value.trim();
    if (!name) return;
    const btn = document.querySelector('#newCompanyModal .btn-primary');
    await guardBtn(btn, 'Creating…', async () => {
        try {
            const data = await apiFetch('/api/companies', {
                method: 'POST', body: {
                    name, website: document.getElementById('ncWebsite').value.trim(),
                    linkedin_url: document.getElementById('ncLinkedin').value.trim() || null,
                    industry: document.getElementById('ncIndustry').value.trim(),
                }
            });
            closeModal('newCompanyModal');
            ['ncName','ncWebsite','ncLinkedin','ncIndustry'].forEach(id => document.getElementById(id).value = '');
            showToast('Company "' + data.name + '" created', 'success');
            await loadSiteOptions();
            loadCustomers();
            // If created from req modal, auto-select the new company and go back
            if (window._quickCreateFromReq && data.default_site_id) {
                window._quickCreateFromReq = false;
                selectSite(data.default_site_id, data.name);
                return;
            }
            openAddSiteModal(data.id, data.name);
        } catch (e) { showToast('Failed to create company', 'error'); }
    });
}

async function openEditCompany(companyId) {
    var c = crmCustomers.find(x => x.id === companyId);
    if (!c) return;
    document.getElementById('ecId').value = companyId;
    document.getElementById('ecName').value = c.name || '';
    document.getElementById('ecAccountType').value = c.account_type || '';
    document.getElementById('ecPhone').value = c.phone || '';
    document.getElementById('ecWebsite').value = c.website || '';
    document.getElementById('ecDomain').value = c.domain || '';
    document.getElementById('ecLinkedin').value = c.linkedin_url || '';
    document.getElementById('ecIndustry').value = c.industry || '';
    document.getElementById('ecLegalName').value = c.legal_name || '';
    document.getElementById('ecEmployeeSize').value = c.employee_size || '';
    document.getElementById('ecHqCity').value = c.hq_city || '';
    document.getElementById('ecHqState').value = c.hq_state || '';
    document.getElementById('ecHqCountry').value = c.hq_country || '';
    document.getElementById('ecCreditTerms').value = c.credit_terms || '';
    document.getElementById('ecTaxId').value = c.tax_id || '';
    document.getElementById('ecCurrency').value = c.currency || 'USD';
    document.getElementById('ecCarrier').value = c.preferred_carrier || '';
    document.getElementById('ecNotes').value = c.notes || '';
    document.getElementById('ecStrategic').checked = !!c.is_strategic;
    await loadUserOptions('ecOwner');
    if (c.account_owner_id) document.getElementById('ecOwner').value = c.account_owner_id;
    openModal('editCompanyModal', 'ecName');
}

async function saveEditCompany() {
    var id = document.getElementById('ecId').value;
    var name = document.getElementById('ecName').value.trim();
    if (!name) { showToast('Company name is required', 'error'); return; }
    var ownerVal = document.getElementById('ecOwner').value;
    try {
        await apiFetch('/api/companies/' + id, {
            method: 'PUT',
            body: {
                name: name,
                account_type: document.getElementById('ecAccountType').value || null,
                phone: document.getElementById('ecPhone').value.trim() || null,
                website: document.getElementById('ecWebsite').value.trim() || null,
                domain: document.getElementById('ecDomain').value.trim() || null,
                linkedin_url: document.getElementById('ecLinkedin').value.trim() || null,
                industry: document.getElementById('ecIndustry').value.trim() || null,
                legal_name: document.getElementById('ecLegalName').value.trim() || null,
                employee_size: document.getElementById('ecEmployeeSize').value.trim() || null,
                hq_city: document.getElementById('ecHqCity').value.trim() || null,
                hq_state: document.getElementById('ecHqState').value.trim() || null,
                hq_country: document.getElementById('ecHqCountry').value.trim() || null,
                credit_terms: document.getElementById('ecCreditTerms').value.trim() || null,
                tax_id: document.getElementById('ecTaxId').value.trim() || null,
                currency: document.getElementById('ecCurrency').value.trim() || null,
                preferred_carrier: document.getElementById('ecCarrier').value.trim() || null,
                notes: document.getElementById('ecNotes').value,
                is_strategic: document.getElementById('ecStrategic').checked,
                account_owner_id: ownerVal ? parseInt(ownerVal) : null,
            }
        });
        closeModal('editCompanyModal');
        showToast('Company updated', 'success');
        loadCustomers();
    } catch (e) { showToast('Failed to update company: ' + (e.message || ''), 'error'); }
}

function openAddSiteModal(companyId, companyName) {
    document.getElementById('asSiteCompanyId').value = companyId;
    delete document.getElementById('asSiteCompanyId').dataset.editSiteId;
    document.getElementById('asSiteCompanyName').textContent = companyName;
    document.querySelector('#addSiteModal h2').innerHTML = 'Add Site to <span id="asSiteCompanyName">' + esc(companyName) + '</span>';
    ['asSiteName','asSiteAddr1','asSiteAddr2','asSiteCity','asSiteState','asSiteZip','asSitePayTerms','asSiteShipTerms','asSiteTimezone','asSiteRecvHours','asSiteCarrierAcct'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('asSiteCountry').value = 'US';
    document.getElementById('asSiteType').value = '';
    document.getElementById('asSiteNotes').value = '';
    openModal('addSiteModal', 'asSiteName');
}

async function addSite() {
    const companyId = document.getElementById('asSiteCompanyId').value;
    const data = {
        site_name: document.getElementById('asSiteName').value.trim(),
        owner_id: document.getElementById('asSiteOwner').value || null,
        address_line1: document.getElementById('asSiteAddr1').value.trim() || null,
        address_line2: document.getElementById('asSiteAddr2').value.trim() || null,
        city: document.getElementById('asSiteCity').value.trim() || null,
        state: document.getElementById('asSiteState').value.trim() || null,
        zip: document.getElementById('asSiteZip').value.trim() || null,
        country: document.getElementById('asSiteCountry').value.trim() || 'US',
        payment_terms: document.getElementById('asSitePayTerms').value.trim(),
        shipping_terms: document.getElementById('asSiteShipTerms').value.trim(),
        site_type: document.getElementById('asSiteType').value || null,
        timezone: document.getElementById('asSiteTimezone').value.trim() || null,
        receiving_hours: document.getElementById('asSiteRecvHours').value.trim() || null,
        carrier_account: document.getElementById('asSiteCarrierAcct').value.trim() || null,
        notes: document.getElementById('asSiteNotes').value.trim() || null,
    };
    if (!data.site_name) return;
    try {
        const editId = document.getElementById('asSiteCompanyId').dataset.editSiteId;
        if (editId) {
            await apiFetch('/api/sites/' + editId, { method: 'PUT', body: data });
            delete document.getElementById('asSiteCompanyId').dataset.editSiteId;
            showToast('Site updated', 'success');
        } else {
            await apiFetch('/api/companies/' + companyId + '/sites', { method: 'POST', body: data });
            showToast('Site created', 'success');
        }
        closeModal('addSiteModal');
        ['asSiteName','asSiteAddr1','asSiteAddr2','asSiteCity','asSiteState','asSiteZip','asSitePayTerms','asSiteShipTerms'].forEach(id => document.getElementById(id).value = '');
        document.getElementById('asSiteCountry').value = 'US';
        document.getElementById('asSiteNotes').value = '';
        loadCustomers();
        loadSiteOptions();
    } catch (e) { showToast('Failed to save site', 'error'); }
}

async function openEditSiteModal(siteId) {
    try {
        const s = await apiFetch('/api/sites/' + siteId);
        document.getElementById('asSiteCompanyId').value = s.company_id;
        document.getElementById('asSiteCompanyId').dataset.editSiteId = siteId;
        document.getElementById('asSiteCompanyName').textContent = s.company_name || 'Unknown';
        document.getElementById('asSiteName').value = s.site_name || '';
        document.getElementById('asSiteOwner').value = s.owner_id || '';
        document.getElementById('asSiteAddr1').value = s.address_line1 || '';
        document.getElementById('asSiteAddr2').value = s.address_line2 || '';
        document.getElementById('asSiteCity').value = s.city || '';
        document.getElementById('asSiteState').value = s.state || '';
        document.getElementById('asSiteZip').value = s.zip || '';
        document.getElementById('asSiteCountry').value = s.country || 'US';
        document.getElementById('asSitePayTerms').value = s.payment_terms || '';
        document.getElementById('asSiteShipTerms').value = s.shipping_terms || '';
        document.getElementById('asSiteType').value = s.site_type || '';
        document.getElementById('asSiteTimezone').value = s.timezone || '';
        document.getElementById('asSiteRecvHours').value = s.receiving_hours || '';
        document.getElementById('asSiteCarrierAcct').value = s.carrier_account || '';
        document.getElementById('asSiteNotes').value = s.notes || '';
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
        el.innerHTML = '<p class="empty">No offers yet — log vendor offers as they come in</p>';
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
        const lastQ = group.last_quoted ? 'last: $' + Number(group.last_quoted.sell_price).toFixed(4) : '';
        let visibleOffers = group.offers;
        if (_offerStatusFilter !== 'all') {
            visibleOffers = visibleOffers.filter(o => (o.status || 'active') === _offerStatusFilter);
        }
        visibleOffers = _sortOffers(visibleOffers);
        const offersHtml = visibleOffers.length ? visibleOffers.map(o => {
            const checked = selectedOffers.has(o.id) ? 'checked' : '';
            const isRef = o.status === 'reference';
            const isExpired = o.status === 'expired';
            const rowCls = isRef ? 'offer-ref' : (isExpired ? 'offer-expired' : '');
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

            const enteredStr = o.entered_by ? '<span style="font-size:10px;color:var(--muted)">by '+esc(o.entered_by)+'</span>' : '';
            return `
            <tr class="${rowCls}">
                <td><input type="checkbox" ${checked} ${isRef ? 'disabled' : ''} onchange="toggleOfferSelect(${o.id},this.checked)"></td>
                <td>${esc(o.vendor_name)}${subDetails ? '<div class="sc-detail" style="font-size:10px;color:var(--muted)">'+subDetails+'</div>' : ''}${noteStr ? '<div>'+noteStr+'</div>' : ''}${photoHtml || fileHtml ? '<div style="margin-top:2px">'+photoHtml+(fileHtml?' '+fileHtml:'')+'</div>' : ''}</td>
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
        document.getElementById('galImg').src = img.onedrive_url || '';
        document.getElementById('galCaption').textContent = img.file_name + ' (' + (idx+1) + '/' + images.length + ')';
        document.getElementById('galPrev').style.visibility = images.length > 1 ? 'visible' : 'hidden';
        document.getElementById('galNext').style.visibility = images.length > 1 ? 'visible' : 'hidden';
    }
    show(0);

    function closeGallery() {
        document.removeEventListener('keydown', galKey);
        gal.remove();
    }
    document.getElementById('galClose').onclick = closeGallery;
    document.getElementById('galPrev').onclick = (e) => { e.stopPropagation(); show((idx - 1 + images.length) % images.length); };
    document.getElementById('galNext').onclick = (e) => { e.stopPropagation(); show((idx + 1) % images.length); };
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
    document.getElementById('eoOfferId').value = offerId;
    document.getElementById('eoVendor').value = offer.vendor_name || '';
    document.getElementById('eoQty').value = offer.qty_available || '';
    document.getElementById('eoPrice').value = offer.unit_price || '';
    document.getElementById('eoLead').value = offer.lead_time || '';
    document.getElementById('eoCond').value = offer.condition || 'New';
    document.getElementById('eoDC').value = offer.date_code || '';
    document.getElementById('eoFirmware').value = offer.firmware || '';
    document.getElementById('eoHardware').value = offer.hardware_code || '';
    document.getElementById('eoPackaging').value = offer.packaging || '';
    document.getElementById('eoMoq').value = offer.moq || '';
    document.getElementById('eoWarranty').value = offer.warranty || '';
    document.getElementById('eoCOO').value = offer.country_of_origin || '';
    document.getElementById('eoNotes').value = offer.notes || '';
    document.getElementById('eoStatus').value = offer.status || 'active';
    openModal('editOfferModal');
}

async function updateOffer() {
    const offerId = document.getElementById('eoOfferId').value;
    const data = {
        vendor_name: document.getElementById('eoVendor').value.trim(),
        qty_available: parseInt(document.getElementById('eoQty').value) || null,
        unit_price: parseFloat(document.getElementById('eoPrice').value) || null,
        lead_time: document.getElementById('eoLead').value.trim() || null,
        condition: document.getElementById('eoCond').value,
        date_code: document.getElementById('eoDC').value.trim() || null,
        firmware: document.getElementById('eoFirmware').value.trim() || null,
        hardware_code: document.getElementById('eoHardware').value.trim() || null,
        packaging: document.getElementById('eoPackaging').value.trim() || null,
        moq: parseInt(document.getElementById('eoMoq').value) || null,
        warranty: document.getElementById('eoWarranty').value.trim() || null,
        country_of_origin: document.getElementById('eoCOO').value.trim() || null,
        notes: document.getElementById('eoNotes').value.trim() || null,
        status: document.getElementById('eoStatus').value,
    };
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
        if (crmQuote && crmQuote.status === 'won') loadBuyPlan();
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
            showToast('Link this requisition to a customer site first (Customers tab)', 'error');
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
        if (!hasSite) steps.push('<li style="color:var(--red)">Link a customer site to this requisition (go to Customers)</li>');
        steps.push('<li>Log vendor offers on the <strong>Offers</strong> tab</li>');
        steps.push('<li>Select offers using the checkboxes, then click <strong>Build Quote from Selected</strong></li>');
        el.innerHTML = '<div class="empty" style="text-align:left;max-width:400px;margin:40px auto"><p style="font-weight:600;margin-bottom:8px">No quote yet</p><ol style="margin:0;padding-left:20px;line-height:1.8;font-size:12px">' + steps.join('') + '</ol></div>';
        return;
    }
    const q = crmQuote;
    const isDraft = q.status === 'draft';
    const lines = (q.line_items || []).map((item, i) => {
        const sellInput = isDraft ? `<input type="number" step="0.0001" class="quote-sell-input" value="${item.sell_price||0}" onchange="updateQuoteLine(${i},'sell_price',this.value)">` : '$'+Number(item.sell_price||0).toFixed(4);
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
            <td class="quote-margin" id="qm-${i}">${Number(item.margin_pct||0).toFixed(1)}%</td>
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
        won: '<p style="color:var(--green);font-weight:600">✓ Won — $' + Number(q.won_revenue||0).toLocaleString() + '</p>',
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
            <div><strong>${esc(q.quote_number)} Rev ${q.revision}</strong> <span class="status-badge status-${q.status}">${q.status}</span></div>
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
        Quick Markup: <input type="number" id="quickMarkup" value="20" style="width:50px" min="0" max="100">%
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

function updateQuoteLine(idx, field, value) {
    if (!crmQuote) return;
    const item = crmQuote.line_items[idx];
    if (field === 'sell_price') {
        item.sell_price = parseFloat(value) || 0;
        const cost = item.cost_price || 0;
        item.margin_pct = item.sell_price > 0 ? ((item.sell_price - cost) / item.sell_price * 100) : 0;
        const mEl = document.getElementById('qm-' + idx);
        if (mEl) mEl.textContent = item.margin_pct.toFixed(1) + '%';
        refreshQuoteTotals();
    }
}

function updateQuoteLineField(idx, field, value) {
    if (!crmQuote) return;
    crmQuote.line_items[idx][field] = value;
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
    const pct = parseFloat(document.getElementById('quickMarkup').value) || 0;
    crmQuote.line_items.forEach(item => {
        item.sell_price = Math.round((item.cost_price || 0) * (1 + pct / 100) * 10000) / 10000;
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
                payment_terms: document.getElementById('qtTerms').value,
                shipping_terms: document.getElementById('qtShip').value,
                validity_days: parseInt(document.getElementById('qtValid').value) || 7,
                notes: document.getElementById('qtNotes').value,
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

    document.getElementById('sqQuoteNum').textContent = q.quote_number + ' Rev ' + q.revision;
    document.getElementById('sqManualEmail').value = '';
    onSqContactChange();
    openModal('sendQuoteModal');
}

function onSqContactChange() {
    var sel = document.getElementById('sqContactSelect');
    var manual = sel.value === '__manual__';
    document.getElementById('sqManualRow').style.display = manual ? '' : 'none';
    if (manual) setTimeout(function() { document.getElementById('sqManualEmail').focus(); }, 50);
}

async function confirmSendQuote() {
    if (!crmQuote) return;
    var sel = document.getElementById('sqContactSelect');
    var toEmail, toName;
    if (sel.value === '__manual__') {
        toEmail = document.getElementById('sqManualEmail').value.trim();
        toName = '';
        if (!toEmail) { showToast('Enter an email address', 'error'); return; }
    } else {
        toEmail = sel.value;
        toName = sel.options[sel.selectedIndex].dataset.name || '';
    }
    var btn = document.querySelector('#sendQuoteModal .btn-primary');
    await guardBtn(btn, 'Sending…', async () => {
        closeModal('sendQuoteModal');
        try {
            await saveQuoteDraft();
            var sendData = await apiFetch('/api/quotes/' + crmQuote.id + '/send', {
                method: 'POST',
                body: { to_email: toEmail, to_name: toName }
            });
            showToast('Quote sent to ' + (sendData.sent_to || toEmail), 'success');
            notifyStatusChange(sendData);
            loadQuote();
        } catch (e) { logCatchError('sendQuoteEmail', e); showToast('Error sending quote: ' + (e.message||''), 'error'); }
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
            crmQuote = q;
            crmQuote._isHistorical = true;
            renderQuote();
        }
    } catch (e) { logCatchError('loadSpecificQuote', e); showToast('Failed to load quote', 'error'); }
}

async function markQuoteResult(result) {
    if (!crmQuote) return;
    if (result === 'won') { openBuyPlanModal(); return; }
    if (markQuoteResult._busy) return; markQuoteResult._busy = true;
    try {
        const resultData = await apiFetch('/api/quotes/' + crmQuote.id + '/result', {
            method: 'POST', body: { result }
        });
        showToast('Quote updated', 'info');
        notifyStatusChange(resultData);
        loadQuote();
    } catch (e) { console.error('markQuoteResult:', e); showToast('Error updating result', 'error'); }
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
            <td>${qty.toLocaleString()}</td>
            <td><input type="number" class="bp-plan-qty" data-idx="${i}" value="${qty}" min="1" style="width:70px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px;text-align:right" oninput="_debouncedUpdateBpTotals()"></td>
            <td>$${Number(item.cost_price||0).toFixed(4)}</td>
            <td class="bp-line-total">$${(qty * Number(item.cost_price||0)).toFixed(2)}</td>
            <td>${esc(item.lead_time || '\u2014')}</td>
        </tr>`;
    }).join('');
    document.getElementById('bpItems').innerHTML = items;
    document.getElementById('bpSalespersonNotes').value = '';
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
    document.getElementById('bpTotal').textContent = '$' + total.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
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

    const statusColors = {
        pending_approval: 'var(--amber)',
        approved: 'var(--green)',
        rejected: 'var(--red)',
        po_entered: 'var(--blue)',
        po_confirmed: 'var(--green)',
        complete: 'var(--green)',
        cancelled: 'var(--muted)',
    };
    const statusLabels = {
        pending_approval: 'Pending Approval',
        approved: 'Approved \u2014 Awaiting PO',
        rejected: 'Rejected',
        po_entered: 'PO Entered \u2014 Verifying',
        po_confirmed: 'PO Confirmed',
        complete: 'Complete',
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
            const poEditable = isBuyer && (bp.status === 'approved' || bp.status === 'po_entered');
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
    if (canApprove && bp.status === 'pending_approval') {
        actionsHtml = `
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
    // Cancel button for pending plans (submitter or admin/manager)
    if (bp.status === 'pending_approval') {
        const canCancel = canApprove || bp.submitted_by_id === window.__userId;
        if (canCancel) {
            actionsHtml += `<div style="margin-top:8px"><button class="btn btn-ghost" onclick="cancelBuyPlan()">Cancel Plan</button></div>`;
        }
    }
    if (!bp.is_stock_sale && isBuyer && (bp.status === 'approved' || bp.status === 'po_entered')) {
        actionsHtml += `
            <div style="margin-top:12px">
                <button class="btn btn-primary" onclick="saveBuyPlanPOs()">Save PO Numbers</button>
                <button class="btn btn-ghost" onclick="verifyBuyPlanPOs()">Verify PO Sent</button>
            </div>`;
    }
    // Cancel button for approved plans with no POs (admin/manager only)
    if (canApprove && bp.status === 'approved') {
        const hasPOs = (bp.line_items || []).some(li => li.po_number);
        if (!hasPOs) {
            actionsHtml += `<div style="margin-top:8px"><button class="btn btn-ghost" onclick="cancelBuyPlan()">Cancel Plan</button></div>`;
        }
    }
    // Complete button for po_confirmed (admin/manager)
    if (canApprove && bp.status === 'po_confirmed') {
        actionsHtml += `<div style="margin-top:12px"><button class="btn btn-success" onclick="completeBuyPlan()">Mark Complete</button></div>`;
    }
    // Resubmit button for rejected/cancelled
    if (bp.status === 'rejected' || bp.status === 'cancelled') {
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
                <span style="font-size:11px;color:var(--muted)">Submitted by ${esc(bp.submitted_by||'')} ${bp.submitted_at ? '\xB7 '+fmtDateTime(bp.submitted_at) : ''}</span>
            </div>
            ${contextHtml}
            ${marginHtml}
            ${notesHtml}
            <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
            <table class="tbl" style="margin-bottom:0">
                <thead><tr><th>MPN</th><th>Vendor</th><th>V.Score</th><th>Plan Qty</th><th>Cost</th><th>Lead</th>${hidePoCol ? '' : '<th>PO</th>'}</tr></thead>
                <tbody>${itemsHtml}</tbody>
            </table>
            </div>
            ${actionsHtml}
        </div>`;
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

// ── Buy Plans Admin List ──────────────────────────────────────────────

let _bpFilter = '';
let _buyPlans = [];

async function showBuyPlans() {
    showView('view-buyplans');
    currentReqId = null;
    await loadBuyPlans();
}

async function loadBuyPlans() {
    var bpl = document.getElementById('buyPlansList');
    if (bpl && !_buyPlans.length) bpl.innerHTML = '<div class="spinner-row"><div class="spinner"></div>Loading buy plans…</div>';
    try {
        let url = '/api/buy-plans';
        if (_bpFilter) url += '?status=' + encodeURIComponent(_bpFilter);
        _buyPlans = await apiFetch(url);
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

    // Search filter
    const q = (document.getElementById('bpSearch') || {}).value || '';
    if (q) {
        const lq = q.toLowerCase();
        data = data.filter(bp => (bp.requisition_name || '').toLowerCase().includes(lq)
            || (bp.customer_name || '').toLowerCase().includes(lq)
            || (bp.sales_order_number || '').toLowerCase().includes(lq)
            || (bp.submitted_by || '').toLowerCase().includes(lq));
    }

    if (!data.length) {
        el.innerHTML = '<p class="empty">No buy plans found</p>';
        return;
    }

    const statusLabels = {pending_approval:'Pending',approved:'Approved',rejected:'Rejected',po_entered:'PO Entered',po_confirmed:'Confirmed',complete:'Complete',cancelled:'Cancelled'};
    const statusBadge = {pending_approval:'b-pend',approved:'b-appr',rejected:'b-rej',po_entered:'b-po',po_confirmed:'b-conf',complete:'b-comp',cancelled:'b-canc'};

    // Sort
    if (_bpSortCol) {
        data.sort((a, b) => {
            let va, vb;
            switch (_bpSortCol) {
                case 'req': va = (a.requisition_name || ''); vb = (b.requisition_name || ''); break;
                case 'status': va = (a.status || ''); vb = (b.status || ''); break;
                case 'customer': va = (a.customer_name || ''); vb = (b.customer_name || ''); break;
                case 'items': va = (a.line_items || []).length; vb = (b.line_items || []).length; break;
                case 'total': va = (a.line_items || []).reduce((s, li) => s + (Number(li.plan_qty || li.qty)||0) * (Number(li.cost_price)||0), 0); vb = (b.line_items || []).reduce((s, li) => s + (Number(li.plan_qty || li.qty)||0) * (Number(li.cost_price)||0), 0); break;
                case 'so': va = (a.sales_order_number || ''); vb = (b.sales_order_number || ''); break;
                case 'by': va = (a.submitted_by || ''); vb = (b.submitted_by || ''); break;
                case 'date': va = (a.submitted_at || ''); vb = (b.submitted_at || ''); break;
                default: va = 0; vb = 0;
            }
            if (typeof va === 'string') return _bpSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
            return _bpSortDir === 'asc' ? va - vb : vb - va;
        });
    }

    const thC = (col) => _bpSortCol === col ? ' class="sorted"' : '';
    const sa = (col) => `<span class="sort-arrow">${_bpSortArrow(col)}</span>`;

    let html = `<div style="padding:0 16px"><table class="tbl"><thead><tr>
        <th onclick="sortBpList('req')"${thC('req')}>Requisition ${sa('req')}</th>
        <th onclick="sortBpList('status')"${thC('status')}>Status ${sa('status')}</th>
        <th onclick="sortBpList('customer')"${thC('customer')}>Customer ${sa('customer')}</th>
        <th onclick="sortBpList('items')"${thC('items')}>Items ${sa('items')}</th>
        <th onclick="sortBpList('total')"${thC('total')}>Total Cost ${sa('total')}</th>
        <th onclick="sortBpList('so')"${thC('so')}>SO# ${sa('so')}</th>
        <th onclick="sortBpList('by')"${thC('by')}>Submitted By ${sa('by')}</th>
        <th onclick="sortBpList('date')"${thC('date')}>Date ${sa('date')}</th>
    </tr></thead><tbody>`;

    for (const bp of data) {
        const label = statusLabels[bp.status] || bp.status;
        const bc = statusBadge[bp.status] || 'b-draft';
        const itemCount = (bp.line_items || []).length;
        const total = (bp.line_items || []).reduce((s, li) => s + (Number(li.plan_qty || li.qty)||0) * (Number(li.cost_price)||0), 0);
        html += `<tr onclick="openBuyPlanDetail(${bp.id})">
            <td><b class="cust-link">${esc(bp.requisition_name || 'Req #' + bp.requisition_id)}</b></td>
            <td><span class="badge ${bc}">${label}</span>${bp.is_stock_sale ? ' <span class="badge b-stock">Stock</span>' : ''}</td>
            <td>${esc(bp.customer_name || '\u2014')}</td>
            <td class="mono">${itemCount}</td>
            <td class="mono">$${total.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td class="mono">${esc(bp.sales_order_number || '\u2014')}</td>
            <td>${esc(bp.submitted_by || '\u2014')}</td>
            <td style="font-size:11px;color:var(--muted)">${bp.submitted_at ? fmtDateTime(bp.submitted_at) : '\u2014'}</td>
        </tr>`;
    }

    html += '</tbody></table></div>';
    el.innerHTML = html;
}

async function openBuyPlanDetail(planId) {
    try {
        _currentBuyPlan = await apiFetch('/api/buy-plans/' + planId);
    } catch (e) { showToast('Failed to load buy plan', 'error'); return; }
    // Re-render inline — reuse renderBuyPlanStatus into a detail overlay
    const el = document.getElementById('buyPlansList');
    const backBtn = `<button class="btn btn-ghost" onclick="loadBuyPlans()" style="margin-bottom:12px">\u2190 Back to list</button>`;
    el.innerHTML = backBtn;
    const section = document.createElement('div');
    section.id = 'buyPlanDetailSection';
    el.appendChild(section);
    renderBuyPlanStatus('buyPlanDetailSection');
}

// ── Token-Based Approval ────────────────────────────────────────────────

async function checkTokenApproval() {
    if (!location.hash.startsWith('#approve-token/')) return false;
    const token = location.hash.replace('#approve-token/', '');
    if (!token) return false;
    try {
        const bp = await fetch('/api/buy-plans/token/' + encodeURIComponent(token)).then(r => {
            if (!r.ok) throw new Error('Invalid token');
            return r.json();
        });
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
        await fetch('/api/buy-plans/token/' + encodeURIComponent(token) + '/approve', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ sales_order_number: soNumber, manager_notes: notes })
        }).then(r => { if (!r.ok) throw new Error('Approval failed'); return r.json(); });
        _showTokenResult('approved', 'Buy plan approved — buyers have been notified.');
    } catch (e) { showToast('Failed to approve: ' + (e.message || e), 'error'); }
}

async function tokenRejectPlan(token) {
    const reason = prompt('Rejection reason:');
    if (reason === null) return;
    try {
        await fetch('/api/buy-plans/token/' + encodeURIComponent(token) + '/reject', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ reason })
        }).then(r => { if (!r.ok) throw new Error('Rejection failed'); return r.json(); });
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
                reason: document.getElementById('lostReason').value,
                notes: document.getElementById('lostNotes').value,
            }
        });
        closeModal('lostModal');
        showToast('Quote marked as lost', 'info');
        notifyStatusChange(lostData);
        loadQuote();
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
    document.getElementById('phMpn').textContent = mpn;
    document.getElementById('phContent').innerHTML = '<p class="empty">Loading...</p>';
    try {
        const data = await apiFetch('/api/pricing-history/' + encodeURIComponent(mpn));
        if (!data.history?.length) {
            document.getElementById('phContent').innerHTML = '<p class="empty">No pricing history for this MPN</p>';
            return;
        }
        let html = '<table class="tbl"><thead><tr><th>Date</th><th>Qty</th><th>Sell</th><th>Margin</th><th>Customer</th><th>Result</th></tr></thead><tbody>';
        data.history.forEach(h => {
            html += '<tr><td>' + fmtDate(h.date) + '</td><td>' + (h.qty||0).toLocaleString() + '</td><td>$' + Number(h.sell_price||0).toFixed(4) + '</td><td>' + Number(h.margin_pct||0).toFixed(1) + '%</td><td>' + esc(h.customer||'') + '</td><td>' + (h.result ? '<span class="status-badge status-'+h.result+'">'+h.result+'</span>' : '—') + '</td></tr>';
        });
        html += '</tbody></table>';
        html += '<div class="ph-summary">Avg: $' + Number(data.avg_price||0).toFixed(4) + ' · Margin: ' + Number(data.avg_margin||0).toFixed(1) + '%' + (data.price_range ? ' · Range: $'+Number(data.price_range[0]).toFixed(4)+' – $'+Number(data.price_range[1]).toFixed(4) : '') + '</div>';
        document.getElementById('phContent').innerHTML = html;
    } catch (e) { logCatchError('pricingHistory', e); document.getElementById('phContent').innerHTML = '<p class="empty">Error loading pricing</p>'; }
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
                        label: c.name + (sites.length > 1 ? ' — ' + s.site_name : ''),
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
    // Always show "+ New Customer" at the bottom
    const qEsc = escAttr(q);
    html += '<div class="site-typeahead-item site-typeahead-add" onclick="quickCreateCompany(\'' + qEsc + '\')">+ New Customer' + (q ? ': <b>' + esc(q) + '</b>' : '') + '</div>';
    list.innerHTML = html;
    list.classList.add('show');
}

function selectSite(id, label) {
    document.getElementById('nrSiteId').value = id;
    document.getElementById('nrSiteSearch').value = label;
    document.getElementById('nrSiteSearch').style.display = 'none';
    document.getElementById('nrSiteList').classList.remove('show');
    // Show selected badge
    const sel = document.getElementById('nrSiteSelected');
    if (sel) {
        document.getElementById('nrSiteSelectedLabel').textContent = label;
        sel.style.display = '';
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
    document.getElementById('nrSiteList').classList.remove('show');
    document.getElementById('ncName').value = prefill || '';
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
    if (!site) { field.style.display = 'none'; return; }
    // Fetch company sites to get contacts
    try {
        const companies = await apiFetch(`/api/companies?search=${encodeURIComponent(site.companyName)}`);
        const company = companies.find(c => c.name === site.companyName);
        if (!company || !company.sites) { field.style.display = 'none'; return; }
        const contacts = company.sites
            .filter(s => s.contact_name)
            .map(s => ({ siteId: s.id, name: s.contact_name, email: s.contact_email, siteName: s.site_name }));
        if (contacts.length === 0) { field.style.display = 'none'; return; }
        select.innerHTML = '<option value="">— Select contact —</option>' +
            contacts.map(c =>
                `<option value="${c.siteId}" ${c.siteId === siteId ? 'selected' : ''}>${esc(c.name)}${c.email ? ' (' + esc(c.email) + ')' : ''} — ${esc(c.siteName)}</option>`
            ).join('');
        field.style.display = '';
    } catch (e) { logCatchError('loadNrContacts', e); field.style.display = 'none'; }
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
    document.getElementById('scModalTitle').textContent = 'Suggested Contacts — ' + (name || domain);
    document.getElementById('scModalSubtitle').textContent = type === 'vendor'
        ? 'Select contacts to add to this vendor card'
        : 'Select a contact to set as the site\'s primary contact';
    document.getElementById('scTitleFilter').value = '';
    document.getElementById('scResults').innerHTML = '<p class="empty">Click Search to find contacts at ' + esc(domain) + '</p>';
    document.getElementById('scAddBtn').style.display = 'none';
    openModal('suggestedContactsModal', 'scTitleFilter');
}

async function searchSuggestedContacts() {
    const domain = scContext.domain;
    if (!domain) return;
    const title = document.getElementById('scTitleFilter').value.trim();
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
        document.getElementById('scAddBtn').style.display = 'none';
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
                    ${c.phone ? '<a href="tel:' + escAttr(c.phone) + '" onclick="event.stopPropagation();autoLogCrmCall(\'' + escAttr(c.phone) + '\')">☎ ' + esc(c.phone) + '</a>' : ''}
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
        const res = await apiFetch(`/api/enrichment/company/${companyId}`, {
            method: 'POST',
            body: { force: true },
        });
        if (res.status === 'completed') {
            const n = (res.enriched_fields || []).length;
            showToast(`Enrichment complete — ${n} field${n !== 1 ? 's' : ''} updated`, 'success');
        } else {
            showToast('Enrichment: ' + (res.status || 'done'));
        }
        loadCustomers();
    } catch (e) {
        console.error('unifiedEnrichCompany:', e);
        showToast('Enrichment failed: ' + (e.message || e), 'error');
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
                                ${c.phone ? `<a href="tel:${escAttr(c.phone)}" onclick="autoLogCrmCall('${escAttr(c.phone)}')">☎ ${esc(c.phone)}</a>` : ''}
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
    document.getElementById('scSiteId').value = siteId;
    document.getElementById('scContactId').value = '';
    document.getElementById('siteContactModalTitle').textContent = 'Add Contact';
    ['scFullName','scTitle','scEmail','scPhone','scNotes'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('scPrimary').checked = false;
    openModal('siteContactModal', 'scFullName');
}

async function openEditSiteContact(siteId, contactId) {
    try {
        const contacts = await apiFetch('/api/sites/' + siteId + '/contacts');
        const c = contacts.find(x => x.id === contactId);
        if (!c) { showToast('Contact not found', 'error'); return; }
        document.getElementById('scSiteId').value = siteId;
        document.getElementById('scContactId').value = contactId;
        document.getElementById('siteContactModalTitle').textContent = 'Edit Contact';
        document.getElementById('scFullName').value = c.full_name || '';
        document.getElementById('scTitle').value = c.title || '';
        document.getElementById('scEmail').value = c.email || '';
        document.getElementById('scPhone').value = c.phone || '';
        document.getElementById('scNotes').value = c.notes || '';
        document.getElementById('scPrimary').checked = !!c.is_primary;
        openModal('siteContactModal', 'scFullName');
    } catch (e) { logCatchError('openEditSiteContact', e); showToast('Error loading contact', 'error'); }
}

async function saveSiteContact() {
    const siteId = document.getElementById('scSiteId').value;
    const contactId = document.getElementById('scContactId').value;
    const data = {
        full_name: document.getElementById('scFullName').value.trim(),
        title: document.getElementById('scTitle').value.trim() || null,
        email: document.getElementById('scEmail').value.trim() || null,
        phone: document.getElementById('scPhone').value.trim() || null,
        notes: document.getElementById('scNotes').value.trim() || null,
        is_primary: document.getElementById('scPrimary').checked,
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
    const companyId = document.getElementById('lcCompanyId').value;
    const data = {
        phone: document.getElementById('lcPhone').value.trim() || null,
        contact_name: document.getElementById('lcContactName').value.trim() || null,
        direction: document.getElementById('lcDirection').value,
        duration_seconds: parseInt(document.getElementById('lcDuration').value) || null,
        notes: document.getElementById('lcNotes').value.trim() || null,
    };
    try {
        await apiFetch('/api/companies/' + companyId + '/activities/call', {
            method: 'POST', body: data
        });
        closeModal('logCallModal');
        showToast('Call logged', 'success');
        const el = document.getElementById('actList-' + companyId);
        if (el) el.innerHTML = '<p class="empty" style="padding:4px;font-size:11px">Loading...</p>';
        loadCompanyActivities(parseInt(companyId));
        const healthEl = document.getElementById('actHealth-' + companyId);
        if (healthEl) { delete healthEl.dataset.loaded; loadCompanyActivityStatus(parseInt(companyId)); }
    } catch(e) { console.error('saveLogCall:', e); showToast('Error logging call', 'error'); }
}

function openLogNoteModal(companyId, companyName) {
    document.getElementById('lnCompanyId').value = companyId;
    document.getElementById('lnCompanyName').textContent = companyName;
    ['lnContactName','lnNotes'].forEach(id => document.getElementById(id).value = '');
    openModal('logNoteModal', 'lnNotes');
}

async function saveLogNote() {
    const companyId = document.getElementById('lnCompanyId').value;
    const notes = document.getElementById('lnNotes').value.trim();
    if (!notes) { showToast('Note text is required', 'error'); return; }
    const data = {
        contact_name: document.getElementById('lnContactName').value.trim() || null,
        notes: notes,
    };
    try {
        await apiFetch('/api/companies/' + companyId + '/activities/note', {
            method: 'POST', body: data
        });
        closeModal('logNoteModal');
        showToast('Note added', 'success');
        const el = document.getElementById('actList-' + companyId);
        if (el) el.innerHTML = '<p class="empty" style="padding:4px;font-size:11px">Loading...</p>';
        loadCompanyActivities(parseInt(companyId));
        const healthEl = document.getElementById('actHealth-' + companyId);
        if (healthEl) { delete healthEl.dataset.loaded; loadCompanyActivityStatus(parseInt(companyId)); }
    } catch(e) { console.error('saveLogNote:', e); showToast('Error adding note', 'error'); }
}

// ── Proactive Offers ──────────────────────────────────────────────────

let _proactiveMatches = [];
let _proactiveSent = [];
let _proactiveTab = 'matches';
let _proactiveSendSiteId = null;
let _proactiveSendMatchIds = [];
let _proactiveSiteContacts = [];

async function showProactiveOffers() {
    showView('view-proactive');
    currentReqId = null;
    switchProactiveTab('matches');
}

function switchProactiveTab(tab, btn) {
    _proactiveTab = tab;
    document.querySelectorAll('#proactiveTabs .tab').forEach(t => t.classList.remove('on'));
    if (btn) btn.classList.add('on');
    else document.querySelectorAll('#proactiveTabs .tab').forEach(t => {
        if (t.textContent.toLowerCase().includes(tab)) t.classList.add('on');
    });
    document.getElementById('proactiveMatchesPanel').style.display = tab === 'matches' ? '' : 'none';
    document.getElementById('proactiveSentPanel').style.display = tab === 'sent' ? '' : 'none';
    document.getElementById('proactiveScorecardPanel').style.display = tab === 'scorecard' ? '' : 'none';
    if (tab === 'matches') loadProactiveMatches();
    else if (tab === 'sent') loadProactiveSent();
    else if (tab === 'scorecard') loadProactiveScorecard();
}

async function loadProactiveMatches() {
    try {
        _proactiveMatches = await apiFetch('/api/proactive/matches');
        renderProactiveMatches();
    } catch (e) { showToast('Failed to load matches', 'error'); }
}

function renderProactiveMatches() {
    const el = document.getElementById('proactiveMatchesPanel');
    if (!_proactiveMatches.length) {
        el.innerHTML = '<p class="empty">No proactive matches yet. When buyers log offers for parts your archived customers needed, matches will appear here.</p>';
        return;
    }
    el.innerHTML = _proactiveMatches.map(group => {
        const matchRows = group.matches.map(m => `
            <tr>
                <td><input type="checkbox" class="pm-check" data-id="${m.id}" data-site="${group.customer_site_id}" checked></td>
                <td><strong>${esc(m.mpn)}</strong></td>
                <td>${esc(m.manufacturer || '')}</td>
                <td>${esc(m.vendor_name)}</td>
                <td>${(m.qty_available||0).toLocaleString()}</td>
                <td>${m.unit_price != null ? '$' + Number(m.unit_price).toFixed(4) : '—'}</td>
                <td>${esc(m.condition || '')}</td>
                <td>${esc(m.lead_time || '')}</td>
                <td style="font-size:10px;color:var(--muted)">${esc(m.original_req_name || '')}</td>
            </tr>
        `).join('');
        return `
        <div class="card" style="margin-bottom:12px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                <div>
                    <strong>${esc(group.company_name)}</strong>
                    ${group.site_name ? ' — ' + esc(group.site_name) : ''}
                    <span style="font-size:11px;color:var(--muted);margin-left:8px">${group.matches.length} match${group.matches.length !== 1 ? 'es' : ''}</span>
                </div>
                <div style="display:flex;gap:6px">
                    <button class="btn btn-primary btn-sm" onclick="openProactiveSendModal(${group.customer_site_id})">Send to Customer</button>
                    <button class="btn btn-ghost btn-sm" onclick="dismissProactiveGroup(${group.customer_site_id})">Dismiss</button>
                </div>
            </div>
            <table class="tbl">
                <thead><tr><th></th><th>MPN</th><th>Mfr</th><th>Vendor</th><th>Qty</th><th>Price</th><th>Cond</th><th>Lead</th><th>Orig. Req</th></tr></thead>
                <tbody>${matchRows}</tbody>
            </table>
        </div>`;
    }).join('');
}

async function dismissProactiveGroup(siteId) {
    const ids = [];
    _proactiveMatches.forEach(g => {
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

async function openProactiveSendModal(siteId) {
    _proactiveSendSiteId = siteId;
    // Get selected match IDs for this site
    const checks = document.querySelectorAll(`.pm-check[data-site="${siteId}"]:checked`);
    _proactiveSendMatchIds = Array.from(checks).map(c => parseInt(c.dataset.id));
    if (!_proactiveSendMatchIds.length) { showToast('Select at least one item', 'error'); return; }

    // Load contacts
    try {
        _proactiveSiteContacts = await apiFetch('/api/proactive/contacts/' + siteId);
    } catch (e) { logCatchError('proactiveContacts', e); _proactiveSiteContacts = []; }

    // Find group for company name
    const group = _proactiveMatches.find(g => g.customer_site_id === siteId);
    const companyName = group ? group.company_name : '';

    // Populate modal
    document.getElementById('psSiteId').value = siteId;
    document.getElementById('psSubject').value = 'Parts Available — ' + companyName;
    document.getElementById('psNotes').value = '';

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
        const defaultSell = m.unit_price ? (m.unit_price * 1.3).toFixed(4) : '0';
        return `<tr>
            <td>${esc(m.mpn)}</td>
            <td>${esc(m.vendor_name)}</td>
            <td>${(m.qty_available||0).toLocaleString()}</td>
            <td>$${m.unit_price != null ? Number(m.unit_price).toFixed(4) : '—'}</td>
            <td><input type="number" step="0.0001" class="ps-sell" data-id="${m.id}" value="${defaultSell}" style="width:90px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px" oninput="_debouncedUpdateProactivePreview()"></td>
            <td class="ps-margin" data-id="${m.id}"></td>
        </tr>`;
    }).join('');
    updateProactivePreview();

    openModal('proactiveSendModal');
}

function updateProactivePreview() {
    let totalSell = 0, totalCost = 0;
    document.querySelectorAll('.ps-sell').forEach(input => {
        const id = input.dataset.id;
        const sell = parseFloat(input.value) || 0;
        const group = _proactiveMatches.find(g => g.customer_site_id === _proactiveSendSiteId);
        const match = group ? group.matches.find(m => m.id === parseInt(id)) : null;
        const cost = match ? (match.unit_price || 0) : 0;
        const qty = match ? (match.qty_available || 0) : 0;
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

    await guardBtn(btn, 'Sending…', async () => {
        try {
            await apiFetch('/api/proactive/send', {
                method: 'POST',
                body: {
                    match_ids: _proactiveSendMatchIds,
                    contact_ids: contactIds,
                    sell_prices: sellPrices,
                    subject: document.getElementById('psSubject').value.trim(),
                    notes: document.getElementById('psNotes').value.trim() || null,
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
    if (!_proactiveSent.length) {
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
    const summaryCards = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px">
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--teal)">${data.total_sent}</div>
            <div style="font-size:11px;color:var(--muted)">Total Sent</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--green)">${data.total_converted}</div>
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
            <div style="font-size:24px;font-weight:700;color:var(--text)">${data.conversion_rate}%</div>
            <div style="font-size:11px;color:var(--muted)">Overall Rate</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--green)">$${Number(data.converted_revenue||0).toLocaleString()}</div>
            <div style="font-size:11px;color:var(--muted)">Won Revenue</div>
        </div>
        <div class="card" style="text-align:center;padding:16px">
            <div style="font-size:24px;font-weight:700;color:var(--green)">$${Number(data.gross_profit||0).toLocaleString()}</div>
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
                <th style="text-align:right">Anticipated</th>
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
                    <td style="text-align:right;color:var(--amber)">$${Number(b.anticipated_revenue||0).toLocaleString()}</td>
                    <td style="text-align:right;color:var(--green)">$${Number(b.revenue||0).toLocaleString()}</td>
                    <td style="text-align:right;color:var(--green)">$${Number(b.gross_profit||0).toLocaleString()}</td>
                </tr>`;
            }).join('')}</tbody>
        </table>
        </div>`;
    }

    el.innerHTML = summaryCards + breakdownHtml;
}

// ── Performance Tracking ─────────────────────────────────────────────

let _perfVendorSort = 'composite_score';
let _perfVendorOrder = 'desc';

function showPerformance() {
    showView('view-performance');
    currentReqId = null;
    // Show digest tab for admins
    const digestTab = document.getElementById('perfDigestTab');
    if (digestTab) digestTab.style.display = window.__isAdmin ? '' : 'none';
    switchPerfTab('vendors');
}

function switchPerfTab(tab, btn) {
    document.querySelectorAll('#perfTabs .fp').forEach(t => t.classList.remove('on'));
    if (btn) btn.classList.add('on');
    else document.querySelector(`#perfTabs .fp[onclick*="${tab}"]`)?.classList.add('on');
    document.getElementById('perfVendorPanel').style.display = tab === 'vendors' ? '' : 'none';
    document.getElementById('perfBuyerPanel').style.display = tab === 'buyers' ? '' : 'none';
    document.getElementById('perfSalesPanel').style.display = tab === 'sales' ? '' : 'none';
    const digestPanel = document.getElementById('perfDigestPanel');
    if (digestPanel) digestPanel.style.display = tab === 'digest' ? '' : 'none';
    if (tab === 'vendors') loadVendorScorecards();
    else if (tab === 'buyers') loadBuyerLeaderboard();
    else if (tab === 'sales') loadSalespersonScorecard();
    else if (tab === 'digest') loadManagerDigest();
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
            html += `<div class="card" style="padding:16px;min-width:120px;text-align:center">
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

    const searchBar = `<div style="margin:0 16px 10px"><input type="text" id="perfVendorSearch" placeholder="Search vendors..." value="${document.getElementById('perfVendorSearch')?.value||''}" class="sbox" oninput="_debouncedLoadVendorScorecards()" style="width:300px"></div>`;

    let html = searchBar + `<div style="overflow-x:auto;padding:0 16px"><table class="tbl">
        <thead><tr>
            <th onclick="window._perfToggleSort('composite_score')"${thC('composite_score')}>Vendor ${sa('composite_score')}</th>
            <th onclick="window._perfToggleSort('response_rate')"${thC('response_rate')}>Response Rate ${sa('response_rate')}</th>
            <th onclick="window._perfToggleSort('quote_conversion')"${thC('quote_conversion')}>Quote Rate ${sa('quote_conversion')}</th>
            <th onclick="window._perfToggleSort('po_conversion')"${thC('po_conversion')}>PO Rate ${sa('po_conversion')}</th>
            <th onclick="window._perfToggleSort('avg_review_rating')"${thC('avg_review_rating')}>Reviews ${sa('avg_review_rating')}</th>
            <th onclick="window._perfToggleSort('composite_score')"${thC('composite_score')}>Score ${sa('composite_score')}</th>
        </tr></thead><tbody>`;

    for (const v of items) {
        if (!v.is_sufficient_data) {
            html += `<tr class="cold-start"><td>${v.vendor_name}</td><td colspan="5" class="metric-cell na" style="text-align:center;font-style:italic">Insufficient Data (${v.interaction_count} interactions)</td></tr>`;
            continue;
        }
        const reviewDisplay = v.avg_review_rating !== null && v.avg_review_rating !== undefined
            ? `<td class="metric-cell ${v.avg_review_rating >= 0.7 ? 'metric-green' : v.avg_review_rating >= 0.4 ? 'metric-yellow' : 'metric-red'}">${(v.avg_review_rating * 5).toFixed(1)}/5</td>`
            : '<td class="metric-cell na">N/A</td>';
        html += `<tr>
            <td><strong>${v.vendor_name}</strong></td>
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
    // Dev assistant defaults to sources tab (Users tab is hidden for them)
    if (!panel && window.__isDevAssistant && !window.__isAdmin) {
        panel = 'sources';
    }
    switchSettingsTab(panel || 'users');
}

function switchSettingsTab(name, btn) {
    document.querySelectorAll('.settings-panel').forEach(p => p.style.display = 'none');
    document.querySelectorAll('#settingsTabs .tab').forEach(t => t.classList.remove('on'));
    const target = document.getElementById('settings-' + name);
    if (target) target.style.display = '';
    if (btn) btn.classList.add('on');
    else {
        const tabBtn = document.querySelector(`#settingsTabs .tab[onclick*="${name}"]`);
        if (tabBtn) tabBtn.classList.add('on');
    }
    // Lazy-load data
    if (name === 'users') loadAdminUsers();
    else if (name === 'health') loadSettingsHealth();
    else if (name === 'scoring') loadSettingsScoring();
    else if (name === 'config') loadSettingsConfig();
    else if (name === 'sources') loadSettingsSources();
    else if (name === 'manage-users') loadAdminUsers();
    else if (name === 'unmatched') loadUnmatchedQueue();
    else if (name === 'teams') loadTeamsConfig();
    else if (name === 'enrichment') { loadEnrichmentQueue(); loadEnrichmentStats(); }
    else if (name === 'tickets') loadTroubleTickets();
}

// Keep backward compat for dropdown links
function showSettings(panel) { openSettingsTab(panel); }

let _sourcesData = [];
let _sourcesFilter = 'all';
let _sourcesQuery = '';
let _sourcesSearchTimer = null;
let _sourcesShowPlanned = false;

function _isPlannedSource(s) {
    // Sources with no env_vars are always "planned" regardless of status
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

    const canToggle = window.__isAdmin || window.__isDevAssistant;

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

    const cardCls = isPlanned ? 'card s-card s-card-planned' : 'card s-card';

    return `<div class="${cardCls}" style="max-width:none">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <div style="display:flex;align-items:center;gap:10px">
                <strong style="font-size:14px">${s.display_name}</strong>
                ${_statusBadge(s.status, isPlanned)}
                <span class="s-hint">${s.source_type}</span>
            </div>
            <div class="s-row" style="gap:8px">
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
        ${statsHtml}${errorHtml}
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
    document.getElementById(`cred-edit-${sourceId}-${varName}`).style.display = '';
    const input = document.getElementById(`cred-input-${sourceId}-${varName}`);
    input.value = '';
    input.focus();
}

function cancelCredEdit(sourceId, varName) {
    document.getElementById(`cred-edit-${sourceId}-${varName}`).style.display = 'none';
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

// ── System Health ──

async function loadSettingsHealth() {
    const el = document.getElementById('settingsHealthContent');
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

async function loadSettingsScoring() {
    const el = document.getElementById('settingsScoringContent');
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const configs = await apiFetch('/api/admin/config');
        const weights = configs.filter(c => c.key.startsWith('weight_'));
        let html = '<div class="card s-card">';
        html += '<h3>Search Scoring Weights</h3>';
        html += '<p class="s-desc">Weights determine how search results are ranked. Total should equal 100.</p>';
        html += '<div class="s-form">';
        for (const w of weights) {
            const label = w.key.replace('weight_', '').replace(/_/g, ' ');
            html += `<div class="s-row">
                <label style="flex:1;font-size:13px;text-transform:capitalize">${label}</label>
                <input type="number" min="0" max="100" value="${w.value}" id="sw_${w.key}"
                    onchange="updateWeightTotal()" class="s-input-num" style="width:60px">
                <button class="btn btn-ghost btn-sm" onclick="saveConfig('${w.key}', document.getElementById('sw_${w.key}').value)">Save</button>
            </div>`;
        }
        html += '</div>';
        html += '<div style="margin-top:16px;font-size:13px"><strong>Total: <span id="weightTotal">0</span></strong> <span id="weightWarn" style="color:var(--red);display:none">(should be 100)</span></div>';
        html += '</div>';
        el.innerHTML = html;
        updateWeightTotal();
    } catch (e) {
        el.innerHTML = '<p class="empty">Error loading scoring config</p>';
    }
}

function updateWeightTotal() {
    const inputs = document.querySelectorAll('[id^="sw_weight_"]');
    let total = 0;
    inputs.forEach(inp => total += parseInt(inp.value) || 0);
    const totalEl = document.getElementById('weightTotal');
    const warnEl = document.getElementById('weightWarn');
    if (totalEl) totalEl.textContent = total;
    if (warnEl) warnEl.style.display = total !== 100 ? '' : 'none';
}


// ── Configuration ──

async function loadSettingsConfig() {
    const el = document.getElementById('settingsConfigContent');
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
        // Reload the panel that owns this key
        if (key.startsWith('weight_')) loadSettingsScoring();
        else loadSettingsConfig();
    } catch (e) {
        showToast('Error saving: ' + (e.message || e), 'error');
    }
}


// ── Manage Users ──

let _adminUsers = [];

async function loadAdminUsers() {
    const el = document.getElementById('adminUsersList');
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
                <option value="dev_assistant" ${u.role==='dev_assistant'?'selected':''}>Dev Assistant</option>
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
    const name = document.getElementById('newUserName').value.trim();
    const email = document.getElementById('newUserEmail').value.trim();
    const role = document.getElementById('newUserRole').value;
    if (!name || !email) { showToast('Name and email are required', 'error'); return; }
    try {
        await apiFetch('/api/admin/users', {method:'POST', body:{name, email, role}});
        document.getElementById('newUserName').value = '';
        document.getElementById('newUserEmail').value = '';
        _userListCache = null;  // Invalidate cache
        showToast('User created successfully', 'success');
        if (typeof loadAdminUsers === 'function') loadAdminUsers();
    } catch (e) {
        showToast('Error: ' + (e.message || e), 'error');
    }
}

async function importCustomers() {
    const fileInput = document.getElementById('customerImportFile');
    if (!fileInput.files.length) { showToast('Select a CSV file first', 'error'); return; }
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    const statusEl = document.getElementById('customerImportStatus');
    statusEl.textContent = 'Importing...';
    try {
        const data = await apiFetch('/api/admin/import/customers', {method:'POST', body:form});
        statusEl.textContent = `Done: ${data.companies_created} companies, ${data.sites_created} sites, ${data.contacts_created} contacts created from ${data.rows_processed} rows`;
        fileInput.value = '';
    } catch (e) {
        statusEl.textContent = 'Error: ' + (e.message || e);
    }
}

async function importVendors() {
    const fileInput = document.getElementById('vendorImportFile');
    if (!fileInput.files.length) { showToast('Select a CSV file first', 'error'); return; }
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    const statusEl = document.getElementById('vendorImportStatus');
    statusEl.textContent = 'Importing...';
    try {
        const data = await apiFetch('/api/admin/import/vendors', {method:'POST', body:form});
        statusEl.textContent = `Done: ${data.vendors_created} vendors, ${data.contacts_created} contacts created from ${data.rows_processed} rows`;
        fileInput.value = '';
    } catch (e) {
        statusEl.textContent = 'Error: ' + (e.message || e);
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  UNMATCHED ACTIVITY QUEUE (Phase 2A)
// ═══════════════════════════════════════════════════════════════════════

async function loadUnmatchedQueue() {
    const el = document.getElementById('unmatchedQueueContent');
    el.innerHTML = '<p class="empty">Loading unmatched activities...</p>';
    try {
        const data = await apiFetch('/api/activities/unmatched?limit=50');
        const items = data.items || [];
        if (!items.length) {
            el.innerHTML = '<p class="empty">No unmatched activities — all clear!</p>';
            return;
        }
        let html = `<p class="s-hint" style="margin:0 0 12px">${data.total} unmatched activit${data.total === 1 ? 'y' : 'ies'} awaiting review</p>`;
        html += '<div class="s-form" style="gap:8px">';
        for (const a of items) {
            const contact = a.contact_email || a.contact_phone || 'Unknown';
            const typeIcon = a.channel === 'email' ? '✉' : '📞';
            const dateStr = a.created_at ? new Date(a.created_at).toLocaleDateString() : '';
            const subject = a.subject ? ` — ${esc(a.subject.substring(0, 60))}` : '';
            html += `<div class="card s-row" style="padding:12px;gap:12px;max-width:none" id="unmatched-${a.id}">
                <span style="font-size:18px">${typeIcon}</span>
                <div style="flex:1;min-width:0">
                    <div style="font-weight:600;font-size:13px">${esc(contact)}${subject}</div>
                    <div class="s-hint">${esc(a.activity_type)} · ${esc(a.user_name || '')} · ${dateStr}</div>
                    ${a.contact_name ? `<div class="s-hint">Contact: ${esc(a.contact_name)}</div>` : ''}
                </div>
                <div style="display:flex;gap:6px;flex-shrink:0">
                    <button class="btn btn-sm" onclick="promptAttributeActivity(${a.id})">Attribute</button>
                    <button class="btn btn-ghost btn-sm" onclick="dismissActivity(${a.id})">Dismiss</button>
                </div>
            </div>`;
        }
        html += '</div>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = `<p class="empty" style="color:var(--red)">Error: ${e.message || e}</p>`;
    }
}

function promptAttributeActivity(activityId) {
    // Show inline attribution panel instead of raw prompt()
    const row = document.getElementById('unmatched-' + activityId);
    if (!row) return;
    // Remove any existing attribution panels
    document.querySelectorAll('.attr-panel').forEach(p => p.remove());
    const panel = document.createElement('div');
    panel.className = 'attr-panel';
    panel.style.cssText = 'padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:8px;margin-top:8px';
    panel.innerHTML = `
        <div style="display:flex;gap:8px;margin-bottom:8px">
            <button class="btn btn-sm attr-type-btn on" data-type="company" onclick="this.parentNode.querySelectorAll('.attr-type-btn').forEach(b=>b.classList.remove('on'));this.classList.add('on');document.getElementById('attrSearch-${activityId}').placeholder='Search companies...';document.getElementById('attrSearch-${activityId}').value='';document.getElementById('attrResults-${activityId}').innerHTML=''">Company</button>
            <button class="btn btn-sm attr-type-btn" data-type="vendor" onclick="this.parentNode.querySelectorAll('.attr-type-btn').forEach(b=>b.classList.remove('on'));this.classList.add('on');document.getElementById('attrSearch-${activityId}').placeholder='Search vendors...';document.getElementById('attrSearch-${activityId}').value='';document.getElementById('attrResults-${activityId}').innerHTML=''">Vendor</button>
            <button class="btn btn-ghost btn-sm" onclick="this.closest('.attr-panel').remove()" style="margin-left:auto">Cancel</button>
        </div>
        <input id="attrSearch-${activityId}" placeholder="Search companies..." oninput="_attrSearch(${activityId},this.value)" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input)">
        <div id="attrResults-${activityId}" style="max-height:150px;overflow-y:auto;margin-top:4px"></div>
    `;
    row.appendChild(panel);
    panel.querySelector('input').focus();
}

var _attrSearchDebounce = null;
function _attrSearch(activityId, query) {
    clearTimeout(_attrSearchDebounce);
    if (query.length < 2) { document.getElementById('attrResults-' + activityId).innerHTML = ''; return; }
    _attrSearchDebounce = setTimeout(async () => {
        const typeBtn = document.querySelector('#unmatched-' + activityId + ' .attr-type-btn.on');
        const entityType = typeBtn ? typeBtn.dataset.type : 'company';
        const resultsEl = document.getElementById('attrResults-' + activityId);
        try {
            let items = [];
            if (entityType === 'company') {
                items = await apiFetch('/api/companies/typeahead?q=' + encodeURIComponent(query));
                items = (items || []).slice(0, 8);
            } else {
                const resp = await apiFetch('/api/vendors?q=' + encodeURIComponent(query));
                items = ((resp.vendors || resp) || []).slice(0, 8).map(v => ({id: v.id, name: v.display_name || v.name}));
            }
            if (!items.length) { resultsEl.innerHTML = '<div style="padding:6px;font-size:11px;color:var(--muted)">No results</div>'; return; }
            resultsEl.innerHTML = items.map(i => `<div onclick="_attrSelect(${activityId},'${entityType}',${i.id})" style="padding:6px 8px;font-size:12px;cursor:pointer;border-radius:4px;hover:background:var(--card2)" onmouseover="this.style.background='var(--card2)'" onmouseout="this.style.background=''">${esc(i.name)}</div>`).join('');
        } catch (e) { resultsEl.innerHTML = '<div style="padding:6px;font-size:11px;color:var(--red)">Search error</div>'; }
    }, 250);
}

async function _attrSelect(activityId, entityType, entityId) {
    try {
        await apiFetch('/api/activities/' + activityId + '/attribute', {
            method: 'POST',
            body: {entity_type: entityType, entity_id: entityId}
        });
        showToast('Activity attributed', 'success');
        const row = document.getElementById('unmatched-' + activityId);
        if (row) row.remove();
    } catch (e) {
        showToast('Error: ' + (e.message || e), 'error');
    }
}

async function dismissActivity(activityId) {
    try {
        await apiFetch(`/api/activities/${activityId}/dismiss`, {method: 'POST'});
        const row = document.getElementById('unmatched-' + activityId);
        if (row) row.remove();
    } catch (e) {
        showToast('Error: ' + (e.message || e), 'error');
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  TEAMS INTEGRATION CONFIG
// ═══════════════════════════════════════════════════════════════════════

async function loadTeamsConfig() {
    const el = document.getElementById('teamsConfigContent');
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

function switchEnrichTab(tab, btn) {
    document.querySelectorAll('#enrichTabs .tab').forEach(t => t.classList.remove('on'));
    btn.classList.add('on');
    document.getElementById('enrichQueuePanel').style.display = tab === 'queue' ? '' : 'none';
    document.getElementById('enrichBackfillPanel').style.display = tab === 'backfill' ? '' : 'none';
    document.getElementById('enrichJobsPanel').style.display = tab === 'jobs' ? '' : 'none';
    const m365Panel = document.getElementById('enrichM365Panel');
    if (m365Panel) m365Panel.style.display = tab === 'm365' ? '' : 'none';

    if (tab === 'queue') loadEnrichmentQueue();
    if (tab === 'backfill') loadEnrichmentJobs();
    if (tab === 'jobs') loadEnrichmentJobs();
    if (tab === 'm365') loadM365Status();
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

// ── Trouble Tickets (Settings Tab) ──────────────────────────────────

async function loadTroubleTickets() {
    const list = document.getElementById('ticketList');
    const detail = document.getElementById('ticketDetail');
    if (!list) return;
    list.style.display = '';
    if (detail) detail.style.display = 'none';
    const status = (document.getElementById('ticketStatusFilter') || {}).value || '';
    const url = '/api/error-reports' + (status ? '?status=' + status : '');
    try {
        const data = await apiFetch(url);
        const countEl = document.getElementById('ticketCount');
        if (countEl) countEl.textContent = data.length + ' ticket' + (data.length !== 1 ? 's' : '');
        if (!data.length) {
            list.innerHTML = '<p class="empty">No trouble tickets found</p>';
            return;
        }
        const statusBadge = (s) => {
            const colors = {open:'red',in_progress:'amber',resolved:'green',closed:'muted'};
            return `<span class="badge" style="background:var(--${colors[s]||'muted'}-light,var(--bg));color:var(--${colors[s]||'muted'});font-size:10px;padding:2px 8px;border-radius:4px">${s.replace('_',' ')}</span>`;
        };
        let html = '<table class="tbl"><thead><tr><th>#</th><th>Title</th><th>Reporter</th><th>Status</th><th>AI</th><th>Screenshot</th><th>Created</th><th></th></tr></thead><tbody>';
        data.forEach(r => {
            const created = r.created_at ? new Date(r.created_at).toLocaleDateString() : '';
            html += `<tr>
                <td>${r.id}</td>
                <td>${esc(r.title)}</td>
                <td class="text-xs">${esc(r.reporter_name || r.reporter_email || '')}</td>
                <td>${statusBadge(r.status)}</td>
                <td class="text-center">${r.has_ai_prompt ? '<span title="AI prompt available" style="color:var(--green)">&#10003;</span>' : '<span style="color:var(--muted)">&mdash;</span>'}</td>
                <td class="text-center">${r.has_screenshot ? '📷' : ''}</td>
                <td class="text-xs-muted">${created}</td>
                <td><button type="button" class="btn btn-sm" onclick="viewTicketDetail(${r.id})">View</button></td>
            </tr>`;
        });
        html += '</tbody></table>';
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = '<p class="empty" style="color:var(--red)">Failed to load tickets</p>';
    }
}

async function viewTicketDetail(id) {
    const list = document.getElementById('ticketList');
    const detail = document.getElementById('ticketDetail');
    if (!detail) return;
    if (list) list.style.display = 'none';
    detail.style.display = '';
    detail.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const r = await apiFetch('/api/error-reports/' + id);
        let consoleHtml = '';
        if (r.console_errors) {
            try {
                const errs = JSON.parse(r.console_errors);
                if (errs.length) {
                    consoleHtml = '<div style="margin-top:12px"><strong style="font-size:12px">Console Errors</strong><pre style="background:var(--bg);padding:8px;border-radius:6px;font-size:11px;max-height:200px;overflow:auto;margin-top:4px">' + esc(errs.map(e => e.msg).join('\n')) + '</pre></div>';
                }
            } catch(e) {}
        }
        let screenshotHtml = '';
        if (r.screenshot_b64) {
            screenshotHtml = '<div style="margin-top:12px"><strong style="font-size:12px">Screenshot</strong><br><img src="' + escAttr(r.screenshot_b64) + '" style="max-width:100%;max-height:400px;border:1px solid var(--border);border-radius:6px;margin-top:4px"></div>';
        }
        const statusOpts = ['open','in_progress','resolved','closed'].map(s =>
            `<option value="${s}" ${s===r.status?'selected':''}>${s.replace('_',' ')}</option>`
        ).join('');
        detail.innerHTML = `
            <button type="button" class="btn btn-ghost btn-sm" onclick="loadTroubleTickets()" style="margin-bottom:12px">&larr; Back to list</button>
            <div class="card s-card">
                <h3>#${r.id} — ${esc(r.title)}</h3>
                <div class="s-row" style="gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text2);margin-bottom:8px">
                    <span>Reporter: <strong>${esc(r.reporter_name || r.reporter_email || 'Unknown')}</strong></span>
                    <span>Created: ${r.created_at ? new Date(r.created_at).toLocaleString() : 'N/A'}</span>
                    ${r.resolved_at ? '<span>Resolved: ' + new Date(r.resolved_at).toLocaleString() + ' by ' + esc(r.resolved_by_email || '') + '</span>' : ''}
                </div>
                ${r.description ? '<div style="margin-top:8px;font-size:13px;white-space:pre-wrap">' + esc(r.description) + '</div>' : ''}
                <div style="margin-top:12px;font-size:11px;color:var(--muted)">
                    <div>URL: ${esc(r.current_url || 'N/A')}</div>
                    <div>View: ${esc(r.current_view || 'N/A')} | Browser: ${esc(r.browser_info || 'N/A')} | Screen: ${esc(r.screen_size || 'N/A')}</div>
                </div>
                ${consoleHtml}
                ${r.ai_prompt ? `
                <div style="margin-top:12px">
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                        <strong style="font-size:12px">AI Prompt</strong>
                        <button type="button" class="btn btn-sm" onclick="copyPromptToClipboard(${r.id})" title="Copy to clipboard">Copy</button>
                        <button type="button" class="btn btn-sm btn-ghost" onclick="regeneratePrompt(${r.id}, this)" title="Re-generate prompt">Regenerate</button>
                    </div>
                    <pre id="aiPrompt_${r.id}" style="background:var(--bg);padding:10px;border-radius:6px;font-size:11px;max-height:300px;overflow:auto;white-space:pre-wrap;word-wrap:break-word;border:1px solid var(--border)">${esc(r.ai_prompt)}</pre>
                </div>` : `
                <div style="margin-top:12px">
                    <div style="display:flex;align-items:center;gap:8px">
                        <span style="font-size:12px;color:var(--muted)">No AI prompt generated</span>
                        <button type="button" class="btn btn-sm" onclick="regeneratePrompt(${r.id}, this)">Generate</button>
                    </div>
                </div>`}
                ${screenshotHtml}
                <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
                    <div class="s-row" style="gap:8px;align-items:flex-end;flex-wrap:wrap">
                        <div class="field" style="flex:0 0 auto"><label style="font-size:11px">Status</label><select id="ticketStatusSelect" class="s-select" style="padding:6px 10px">${statusOpts}</select></div>
                        <div class="field" style="flex:1"><label style="font-size:11px">Admin Notes</label><textarea id="ticketAdminNotes" rows="2" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;resize:vertical;font-family:inherit">${esc(r.admin_notes || '')}</textarea></div>
                        <button type="button" class="btn btn-primary btn-sm" onclick="updateTicketStatus(${r.id}, this)">Update</button>
                    </div>
                </div>
            </div>`;
    } catch (e) {
        detail.innerHTML = '<p class="empty" style="color:var(--red)">Failed to load ticket</p>';
    }
}

async function updateTicketStatus(id, btn) {
    await guardBtn(btn, 'Saving…', async () => {
        const status = (document.getElementById('ticketStatusSelect') || {}).value;
        const notes = (document.getElementById('ticketAdminNotes') || {}).value;
        await apiFetch('/api/error-reports/' + id + '/status', {
            method: 'PUT',
            body: { status: status, admin_notes: notes },
        });
        showToast('Ticket updated', 'success');
        viewTicketDetail(id);
    });
}

function exportTicketsXlsx() {
    const status = (document.getElementById('ticketStatusFilter') || {}).value || '';
    const url = '/api/error-reports/export/xlsx' + (status ? '?status=' + status : '');
    window.open(url, '_blank');
}

function copyPromptToClipboard(id) {
    const pre = document.getElementById('aiPrompt_' + id);
    if (!pre) return;
    const text = pre.textContent || pre.innerText;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            showToast('Prompt copied to clipboard', 'success');
        }).catch(() => {
            _fallbackCopyText(pre);
        });
    } else {
        _fallbackCopyText(pre);
    }
}

function _fallbackCopyText(el) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    try {
        document.execCommand('copy');
        showToast('Prompt copied to clipboard', 'success');
    } catch (e) {
        showToast('Failed to copy — select and copy manually', 'error');
    }
    sel.removeAllRanges();
}

async function regeneratePrompt(id, btn) {
    await guardBtn(btn, 'Generating…', async () => {
        await apiFetch('/api/error-reports/' + id + '/regenerate-prompt', { method: 'POST' });
        showToast('AI prompt regenerated', 'success');
        viewTicketDetail(id);
    });
}

// ── ESM: expose all inline-handler functions to window ────────────────
Object.assign(window, {
    _attrSearch, _attrSelect, _debouncedFilterSiteContacts,
    _debouncedLoadVendorScorecards, _debouncedUpdateBpTotals,
    _debouncedUpdateProactivePreview,
    applyMarkup, approveBuyPlan, approveEnrichItem,
    autoCreateSiteAndSelect, autoLogCrmCall,
    browseOneDrive, cancelBuyPlan, cancelCredEdit, cancelEnrichJob,
    completeBuyPlan, convertProactiveOffer, copyPromptToClipboard,
    copyQuoteTable, deleteAIContact, deleteAdminUser, deleteCredential,
    deleteOffer, deleteOfferAttachment, deleteSiteContact,
    dismissActivity, dismissProactiveGroup, editCredential,
    eqToggleAll, eqToggleItem, loadBuyPlans, loadBuyerLeaderboard,
    loadQuote, loadSpecificQuote, loadSalespersonScorecard,
    loadTroubleTickets, markQuoteResult, onSourcesSearch,
    openAddSiteContact, openAddSiteModal, openBuyPlanDetail,
    openEditCompany, openEditOffer, openEditSiteContact,
    openEditSiteModal, openLogNoteModal, openLostModal,
    openOfferGallery, openPricingHistory, openProactiveSendModal,
    openRejectBuyPlanModal, promptAttributeActivity, quickCreateCompany,
    refreshBuyerLeaderboard, refreshTeamsChannels,
    refreshVendorScorecards, regeneratePrompt, rejectBuyPlan,
    rejectEnrichItem, reopenQuote, resubmitBuyPlan, reviseQuote,
    saveAIContact, saveBuyPlanPOs, saveConfig, saveCredential,
    saveParsedOffers, saveQuoteDraft, saveTeamsConfig, scToggle,
    selectOneDriveFile, selectSite, sendQuoteEmail, setOfferFilter,
    setOfferSort, setSourcesFilter, showView, sortBpList, sortCustList,
    sortSalesScorecard, testSourceCred, testTeamsPost, toggleCustDrill,
    toggleOfferSelect, togglePlannedSources, toggleSiteDetail,
    toggleSourceStatus, tokenApprovePlan, tokenRejectPlan,
    triggerDeepScan, unifiedEnrichCompany, updateQuoteLine,
    updateQuoteLineField, updateTicketStatus, updateUserField,
    updateWeightTotal, verifyBuyPlanPOs, viewTicketDetail,
    openSuggestedContacts,
    // HTML template inline handlers
    addSelectedSuggestedContacts, addSite, bulkApproveSelected,
    confirmSendQuote, createCompany, createUser, exportTicketsXlsx,
    filterSiteTypeahead, importCustomers, importVendors,
    loadCustomers, loadEnrichmentQueue, onSqContactChange,
    openNewCompanyModal, renderBuyPlansList, saveEditCompany,
    saveLogCall, saveLogNote, saveSiteContact, searchSuggestedContacts,
    sendProactiveOffer, setBpFilter, startBackfill, startEmailBackfill,
    startWebsiteScrape, submitBuyPlan, submitLost, switchEnrichTab,
    switchPerfTab, switchProactiveTab, switchSettingsTab,
    toggleCustUnassigned, updateOffer,
    // Cross-file calls from app.js
    goToCompany, showBuyPlans, showCustomers, showPerformance,
    showProactiveOffers, showSettings,
});
