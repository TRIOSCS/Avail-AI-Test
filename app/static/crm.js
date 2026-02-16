/* AVAIL v1.2.0 — CRM Extension: Customers, Offers, Quotes */

// Depends on app.js (loaded first): apiFetch, debounce, esc, escAttr, showToast, fmtDate, fmtDateTime

// ── CRM State ──────────────────────────────────────────────────────────
let crmCustomers = [];
let crmOffers = [];
let crmQuote = null;
let selectedOffers = new Set();

// ── Customers View ─────────────────────────────────────────────────────

async function showCustomers() {
    showView('view-customers');
    currentReqId = null;
    await loadCustomers();
}

async function loadCustomers() {
    try {
        const filter = document.getElementById('custFilter')?.value || '';
        const myOnly = document.getElementById('custMyOnly')?.checked;
        let url = '/api/companies?search=' + encodeURIComponent(filter);
        if (myOnly && window.userId) url += '&owner_id=' + window.userId;
        crmCustomers = await apiFetch(url);
        renderCustomers();
    } catch (e) { showToast('Failed to load customers', 'error'); }
}

function renderCustomers() {
    const el = document.getElementById('custList');
    if (!crmCustomers.length) {
        el.innerHTML = '<p class="empty">No customers yet — add a company to get started</p>';
        return;
    }
    el.innerHTML = crmCustomers.map(c => {
        const sitesHtml = c.sites.map(s => `
            <div class="cust-site" onclick="event.stopPropagation();toggleSiteDetail(${s.id})">
                <span class="cust-site-name">${esc(s.site_name)}</span>
                <span class="cust-site-owner">${esc(s.owner_name || '—')}</span>
                <span class="cust-site-reqs">${s.open_reqs ? s.open_reqs + ' open reqs' : '—'}</span>
            </div>
            <div id="siteDetail-${s.id}" class="site-detail-panel" style="display:none"></div>
        `).join('');
        const enrichHtml = c.last_enriched_at
            ? `<div class="enrich-bar">
                    ${c.industry ? '<span class="enrich-tag">' + esc(c.industry) + '</span>' : ''}
                    ${c.employee_size ? '<span class="enrich-tag">👥 ' + esc(c.employee_size) + '</span>' : ''}
                    ${c.hq_city ? '<span class="enrich-tag">📍 ' + esc(c.hq_city) + (c.hq_state ? ', ' + esc(c.hq_state) : '') + '</span>' : ''}
                    ${c.linkedin_url ? '<a href="' + escAttr(c.linkedin_url) + '" target="_blank" style="color:var(--teal);text-decoration:none;font-size:10px">LinkedIn ↗</a>' : ''}
                </div>`
            : '';
        const domain = c.domain || (c.website ? c.website.replace(/https?:\/\/(www\.)?/, '').split('/')[0] : '');
        return `
        <div class="card cust-card" id="custCard-${c.id}">
            <div class="cust-header" onclick="toggleCompanyCard(this.parentElement,${c.id})">
                <span class="cust-expand">▶</span>
                <span class="cust-name">${esc(c.name)}</span>
                ${domain ? '<span style="font-size:10px;color:var(--muted)">' + esc(domain) + '</span>' : ''}
                <span class="cust-count">${c.site_count} site${c.site_count !== 1 ? 's' : ''}</span>
                <span id="actHealth-${c.id}" style="margin-left:4px"></span>
                <span style="margin-left:auto;display:flex;gap:4px;flex-wrap:wrap" onclick="event.stopPropagation()">
                    <button class="btn-enrich" onclick="enrichCompany(${c.id},'${escAttr(domain)}')">Enrich</button>
                    ${domain ? '<button class="btn-enrich" onclick="openSuggestedContacts(\'company\','+c.id+',\''+escAttr(domain)+'\',\''+escAttr(c.name)+'\')">Suggested Contacts</button>' : ''}
                </span>
            </div>
            ${enrichHtml}
            <div class="cust-sites">${sitesHtml}
                <div class="cust-add-site">
                    <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openAddSiteModal(${c.id},'${escAttr(c.name)}')">+ Add Site</button>
                </div>
            </div>
            <div id="actSection-${c.id}" class="cust-activity-section" style="display:none">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                    <span class="si-contacts-title">Recent Activity</span>
                    <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openLogCallModal(${c.id},'${escAttr(c.name)}')">+ Log Call</button>
                </div>
                <div id="actList-${c.id}"><p class="empty" style="padding:4px;font-size:11px">Loading...</p></div>
            </div>
        </div>`;
    }).join('');
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
            const contactsHtml = (s.contacts || []).length
                ? s.contacts.map(c => `
                    <div class="si-contact">
                        <div class="si-contact-info">
                            <div>
                                <span class="si-contact-name">${esc(c.full_name)}</span>
                                ${c.is_primary ? '<span class="si-contact-badge">Primary</span>' : ''}
                                ${c.title ? '<span class="si-contact-title">' + esc(c.title) + '</span>' : ''}
                            </div>
                            <div class="si-contact-meta">
                                ${c.email ? '<a href="mailto:'+esc(c.email)+'">'+esc(c.email)+'</a>' : ''}
                                ${c.email && c.phone ? ' · ' : ''}
                                ${c.phone ? '<span>' + esc(c.phone) + '</span>' : ''}
                            </div>
                            ${c.notes ? '<div class="si-contact-notes">'+esc(c.notes)+'</div>' : ''}
                        </div>
                        <div class="si-contact-actions">
                            <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openEditSiteContact(${s.id},${c.id})">Edit</button>
                            <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteSiteContact(${s.id},${c.id},'${escAttr(c.full_name)}')">✕</button>
                        </div>
                    </div>`).join('')
                : '<p class="empty" style="padding:4px;font-size:11px">No contacts — add one below</p>';
            panel.innerHTML = `
            <div class="site-info">
                <div class="si-row"><span class="si-label">Owner</span><span>${esc(s.owner_name || '—')}</span></div>
                <div class="si-contacts">
                    <div class="si-contacts-title">Contacts</div>
                    ${contactsHtml}
                    <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openAddSiteContact(${s.id})" style="margin-top:4px">+ Add Contact</button>
                </div>
                <div class="si-row"><span class="si-label">Terms</span><span>${esc(s.payment_terms || '—')} · ${esc(s.shipping_terms || '—')}</span></div>
                <div class="si-row"><span class="si-label">Address</span><span>${esc(s.address_line1 || '')} ${s.city ? esc(s.city)+', ' : ''}${esc(s.state || '')} ${esc(s.zip || '')}</span></div>
                ${s.notes ? '<div class="si-row"><span class="si-label">Notes</span><span>'+esc(s.notes)+'</span></div>' : ''}
                <div class="si-reqs">
                    <strong style="font-size:11px;color:var(--muted)">Recent Requisitions</strong>
                    ${(s.recent_reqs || []).length ? s.recent_reqs.map(r => `
                        <div class="si-req" onclick="showDetail(${r.id},'${escAttr(r.name)}')">
                            <span>REQ-${String(r.id).padStart(3,'0')}</span>
                            <span>${r.requirement_count} MPNs</span>
                            <span class="status-badge status-${r.status}">${r.status}</span>
                            <span>${fmtDate(r.created_at)}</span>
                        </div>
                    `).join('') : '<p class="empty" style="padding:4px;font-size:11px">No requisitions</p>'}
                </div>
                <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
                    <button class="btn btn-ghost btn-sm" onclick="openEditSiteModal(${s.id})">Edit Site</button>
                    <button class="btn-enrich" onclick="openSuggestedContacts('site',${s.id},'${escAttr(siteDomain)}','${escAttr(s.company_name || '')}')">Suggested Contacts</button>
                    <button class="btn-ai" onclick="findAIContacts('site',${s.id},'${escAttr(s.company_name || '')}','${escAttr(siteDomain)}')">🤖 Find Contacts</button>
                </div>
                <div id="siteIntel-${s.id}"></div>
            </div>`;
            // Load company intel asynchronously
            const intelEl = document.getElementById('siteIntel-' + s.id);
            if (intelEl && s.company_name) {
                loadCompanyIntel(s.company_name, siteDomain, intelEl);
            }
        } catch (e) { panel.innerHTML = '<p class="empty" style="padding:8px">Error loading site</p>'; }
    } else {
        panel.style.display = 'none';
    }
}

function openNewCompanyModal() {
    document.getElementById('newCompanyModal').classList.add('open');
    setTimeout(() => document.getElementById('ncName').focus(), 100);
}

async function createCompany() {
    const name = document.getElementById('ncName').value.trim();
    if (!name) return;
    try {
        const data = await apiFetch('/api/companies', {
            method: 'POST', body: {
                name, website: document.getElementById('ncWebsite').value.trim(),
                industry: document.getElementById('ncIndustry').value.trim(),
            }
        });
        closeModal('newCompanyModal');
        document.getElementById('ncName').value = '';
        document.getElementById('ncWebsite').value = '';
        document.getElementById('ncIndustry').value = '';
        showToast('Company "' + data.name + '" created', 'success');
        openAddSiteModal(data.id, data.name);
        loadCustomers();
        loadSiteOptions();
    } catch (e) { showToast('Failed to create company', 'error'); }
}

function openAddSiteModal(companyId, companyName) {
    document.getElementById('asSiteCompanyId').value = companyId;
    delete document.getElementById('asSiteCompanyId').dataset.editSiteId;
    document.getElementById('asSiteCompanyName').textContent = companyName;
    document.querySelector('#addSiteModal h2').innerHTML = 'Add Site to <span id="asSiteCompanyName">' + esc(companyName) + '</span>';
    ['asSiteName','asSiteContactName','asSiteContactEmail','asSiteContactPhone','asSitePayTerms','asSiteShipTerms'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('addSiteModal').classList.add('open');
    setTimeout(() => document.getElementById('asSiteName').focus(), 100);
}

async function addSite() {
    const companyId = document.getElementById('asSiteCompanyId').value;
    const data = {
        site_name: document.getElementById('asSiteName').value.trim(),
        owner_id: document.getElementById('asSiteOwner').value || null,
        contact_name: document.getElementById('asSiteContactName').value.trim(),
        contact_email: document.getElementById('asSiteContactEmail').value.trim(),
        contact_phone: document.getElementById('asSiteContactPhone').value.trim(),
        payment_terms: document.getElementById('asSitePayTerms').value.trim(),
        shipping_terms: document.getElementById('asSiteShipTerms').value.trim(),
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
        ['asSiteName','asSiteContactName','asSiteContactEmail','asSiteContactPhone','asSitePayTerms','asSiteShipTerms'].forEach(id => document.getElementById(id).value = '');
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
        document.getElementById('asSiteContactName').value = s.contact_name || '';
        document.getElementById('asSiteContactEmail').value = s.contact_email || '';
        document.getElementById('asSiteContactPhone').value = s.contact_phone || '';
        document.getElementById('asSitePayTerms').value = s.payment_terms || '';
        document.getElementById('asSiteShipTerms').value = s.shipping_terms || '';
        document.getElementById('addSiteModal').classList.add('open');
        document.querySelector('#addSiteModal h2').innerHTML = 'Edit Site — <span>' + esc(s.site_name || '') + '</span>';
    } catch (e) { console.error('openEditSiteModal:', e); showToast('Error loading site', 'error'); }
}

// ── Offers Tab ─────────────────────────────────────────────────────────

let _hasNewOffers = false;
let _latestOfferAt = null;
let _pendingOfferFiles = [];  // Files queued for upload after offer save

async function loadOffers() {
    if (!currentReqId) return;
    try {
        const res = await fetch('/api/requisitions/' + currentReqId + '/offers');
        if (!res.ok) return;
        const data = await res.json();
        _hasNewOffers = data.has_new_offers || false;
        _latestOfferAt = data.latest_offer_at || null;
        crmOffers = data.groups || [];
        selectedOffers.clear();
        renderOffers();
        updateOfferTabBadge();
    } catch (e) { console.error('loadOffers:', e); }
}

function updateOfferTabBadge() {
    const totalOffers = crmOffers.reduce((sum, g) => sum + (g.offers?.length || 0), 0);
    document.querySelectorAll('.tab').forEach(t => {
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

function renderOffers() {
    const el = document.getElementById('offersContent');
    if (!crmOffers.length) {
        el.innerHTML = '<p class="empty">No offers yet — log vendor offers as they come in</p>';
        return;
    }
    el.innerHTML = crmOffers.map(group => {
        const targetStr = group.target_price ? '$' + Number(group.target_price).toFixed(4) : 'no target';
        const lastQ = group.last_quoted ? 'last: $' + Number(group.last_quoted.sell_price).toFixed(4) : '';
        const offersHtml = group.offers.length ? group.offers.map(o => {
            const checked = selectedOffers.has(o.id) ? 'checked' : '';
            const isRef = o.status === 'reference';
            const subDetails = [o.firmware && 'FW: '+esc(o.firmware), o.hardware_code && 'HW: '+esc(o.hardware_code), o.packaging && 'Pkg: '+esc(o.packaging)].filter(Boolean).join(' · ');
            const noteStr = o.notes ? '<div style="font-size:10px;color:var(--text2);margin-top:2px">'+esc(o.notes)+'</div>' : '';
            const attHtml = (o.attachments||[]).map(a => `<a href="${esc(a.onedrive_url||'#')}" target="_blank" style="font-size:10px;color:var(--teal);text-decoration:underline">${esc(a.file_name)}</a>`).join(' ');
            const enteredStr = o.entered_by ? '<span style="font-size:10px;color:var(--muted)">by '+esc(o.entered_by)+'</span>' : '';
            return `
            <tr class="${isRef ? 'offer-ref' : ''}">
                <td><input type="checkbox" ${checked} ${isRef ? 'disabled' : ''} onchange="toggleOfferSelect(${o.id},this.checked)"></td>
                <td>${esc(o.vendor_name)}${subDetails ? '<div class="sc-detail" style="font-size:10px;color:var(--muted)">'+subDetails+'</div>' : ''}${noteStr}${attHtml ? '<div style="margin-top:2px">'+attHtml+'</div>' : ''}</td>
                <td>${o.unit_price != null ? '$'+Number(o.unit_price).toFixed(4) : '—'}</td>
                <td>${o.qty_available != null ? o.qty_available.toLocaleString() : '—'}</td>
                <td>${esc(o.lead_time || '—')}</td>
                <td>${esc(o.condition || '—')}</td>
                <td>${esc(o.date_code || '—')}</td>
                <td>${o.moq ? o.moq.toLocaleString() : '—'}</td>
                <td>${enteredStr}</td>
                <td>${isRef ? '<span class="offer-ref-badge">ref</span>' : '<button class="btn btn-danger btn-sm" onclick="deleteOffer('+o.id+')" title="Remove offer" style="padding:2px 6px;font-size:10px">✕</button>'}</td>
            </tr>`;
        }).join('') : '<tr><td colspan="10" class="empty" style="padding:8px">No offers for this part</td></tr>';
        return `
        <div class="offer-group">
            <div class="offer-group-header">
                <strong>${esc(group.mpn)}</strong>
                <span>need ${(group.target_qty||0).toLocaleString()}</span>
                <span>${targetStr}</span>
                <span>${lastQ}</span>
                <button class="btn btn-ghost btn-sm" onclick="openPricingHistory('${escAttr(group.mpn)}')">📊</button>
            </div>
            <table class="tbl offer-table">
                <thead><tr><th style="width:30px"></th><th>Vendor</th><th>Price</th><th>Avail</th><th>Lead</th><th>Cond</th><th>DC</th><th>MOQ</th><th>By</th><th style="width:40px"></th></tr></thead>
                <tbody>${offersHtml}</tbody>
            </table>
        </div>`;
    }).join('');
    updateBuildQuoteBtn();
}

function toggleOfferSelect(offerId, checked) {
    if (checked) selectedOffers.add(offerId);
    else selectedOffers.delete(offerId);
    updateBuildQuoteBtn();
}

function updateBuildQuoteBtn() {
    const btn = document.getElementById('buildQuoteBtn');
    if (btn) {
        btn.disabled = selectedOffers.size === 0;
        btn.textContent = 'Build Quote from Selected (' + selectedOffers.size + ')';
    }
}

function openLogOfferModal() {
    document.getElementById('logOfferModal').classList.add('open');
    _pendingOfferFiles = [];
    document.getElementById('loAttachments').innerHTML = '';
    const sel = document.getElementById('loMpn');
    sel.innerHTML = crmOffers.map(g => '<option value="' + g.requirement_id + '" data-mpn="' + escAttr(g.mpn) + '">' + esc(g.mpn) + ' (need ' + g.target_qty + ')</option>').join('');
    setTimeout(() => document.getElementById('loVendor').focus(), 100);
}

async function saveOffer(andNext) {
    const sel = document.getElementById('loMpn');
    const reqId = sel.value;
    const mpn = sel.options[sel.selectedIndex]?.getAttribute('data-mpn') || '';
    const data = {
        requirement_id: parseInt(reqId) || null,
        mpn: mpn,
        vendor_name: document.getElementById('loVendor').value.trim(),
        qty_available: parseInt(document.getElementById('loQty').value) || null,
        unit_price: parseFloat(document.getElementById('loPrice').value) || null,
        lead_time: document.getElementById('loLead').value.trim(),
        condition: document.getElementById('loCond').value,
        date_code: document.getElementById('loDC').value.trim(),
        firmware: document.getElementById('loFirmware').value.trim() || null,
        hardware_code: document.getElementById('loHardware').value.trim() || null,
        packaging: document.getElementById('loPackaging').value.trim() || null,
        moq: parseInt(document.getElementById('loMoq').value) || null,
        notes: document.getElementById('loNotes').value.trim(),
        vendor_website: document.getElementById('loWebsite').value.trim() || null,
    };
    if (!data.vendor_name || !data.mpn) return;
    try {
        const res = await fetch('/api/requisitions/' + currentReqId + '/offers', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        if (!res.ok) { showToast('Failed to save offer', 'error'); return; }
        const result = await res.json();
        // Upload pending attachments
        if (_pendingOfferFiles.length && result.id) {
            for (const f of _pendingOfferFiles) {
                try {
                    if (f._onedrive_item_id) {
                        await apiFetch('/api/offers/' + result.id + '/attachments/onedrive', {
                            method: 'POST', body: { item_id: f._onedrive_item_id }
                        });
                    } else {
                        const fd = new FormData();
                        fd.append('file', f);
                        await fetch('/api/offers/' + result.id + '/attachments', { method: 'POST', body: fd });
                    }
                } catch (e) { console.error('Attachment upload failed:', e); }
            }
        }
        _pendingOfferFiles = [];
        showToast('Offer from ' + data.vendor_name + ' saved', 'success');
        notifyStatusChange(result);
        if (andNext) {
            ['loVendor','loQty','loPrice','loLead','loDC','loFirmware','loHardware','loPackaging','loMoq','loNotes','loWebsite'].forEach(id => document.getElementById(id).value = '');
            document.getElementById('loCond').value = 'New';
            document.getElementById('loWebsiteRow').style.display = 'none';
            document.getElementById('loAttachments').innerHTML = '';
            document.getElementById('loVendor').focus();
        } else {
            closeModal('logOfferModal');
        }
        loadOffers();
    } catch (e) { console.error('saveOffer:', e); showToast('Error saving offer', 'error'); }
}

function handleOfferFileSelect(input) {
    const files = Array.from(input.files);
    _pendingOfferFiles.push(...files);
    renderPendingAttachments();
    input.value = '';
}

function renderPendingAttachments() {
    const el = document.getElementById('loAttachments');
    el.innerHTML = _pendingOfferFiles.map((f, i) =>
        `<span class="badge" style="background:var(--bg3);font-size:10px;padding:2px 8px;display:inline-flex;align-items:center;gap:4px">
            ${esc(f.name)} <button onclick="_pendingOfferFiles.splice(${i},1);renderPendingAttachments()" style="border:none;background:none;cursor:pointer;color:var(--muted);font-size:12px">✕</button>
        </span>`
    ).join('');
}

// ── OneDrive Browser ────────────────────────────────────────────────────

let _odCurrentPath = '';
let _odTargetOfferId = null;

function openOneDrivePicker(offerId) {
    _odTargetOfferId = offerId || null;
    _odCurrentPath = '';
    document.getElementById('oneDriveModal').classList.add('open');
    browseOneDrive('');
}

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
                // Store as a special OneDrive reference in pending files
                const odRef = new File([], item.name);
                odRef._onedrive_item_id = itemId;
                odRef._onedrive_name = item.name;
                _pendingOfferFiles.push(odRef);
                renderPendingAttachments();
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
        const res = await fetch('/api/offers/' + offerId, { method: 'DELETE' });
        if (!res.ok) { showToast('Failed to delete offer', 'error'); return; }
        showToast('Offer removed', 'info');
        selectedOffers.delete(offerId);
        loadOffers();
    } catch (e) { console.error('deleteOffer:', e); showToast('Error deleting offer', 'error'); }
}

// ── Quote Tab ──────────────────────────────────────────────────────────

async function loadQuote() {
    if (!currentReqId) return;
    try {
        const res = await fetch('/api/requisitions/' + currentReqId + '/quote');
        if (!res.ok) { crmQuote = null; renderQuote(); updateQuoteTabBadge(); return; }
        crmQuote = await res.json();
        renderQuote();
        updateQuoteTabBadge();
        if (crmQuote && crmQuote.status === 'won') loadBuyPlan();
    } catch (e) { console.error('loadQuote:', e); crmQuote = null; renderQuote(); }
}

function updateQuoteTabBadge() {
    document.querySelectorAll('.tab').forEach(t => {
        if (t.textContent.match(/^Quote/)) {
            t.textContent = crmQuote ? 'Quote (' + crmQuote.status + ')' : 'Quote';
        }
    });
}

async function buildQuoteFromSelected() {
    if (selectedOffers.size === 0) return;
    try {
        const res = await fetch('/api/requisitions/' + currentReqId + '/quote', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ offer_ids: Array.from(selectedOffers) })
        });
        if (!res.ok) { showToast('Failed to build quote', 'error'); return; }
        crmQuote = await res.json();
        showToast('Quote built — review and adjust sell prices', 'success');
        notifyStatusChange(crmQuote);
        const tabs = document.querySelectorAll('.tab');
        switchTab('quote', tabs[4]);
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) { console.error('buildQuoteFromSelected:', e); showToast('Error building quote', 'error'); }
}

function renderQuote() {
    const el = document.getElementById('quoteContent');
    if (!crmQuote) {
        el.innerHTML = '<p class="empty">No quote yet — select offers on the Offers tab and click "Build Quote"</p>';
        return;
    }
    const q = crmQuote;
    const lines = (q.line_items || []).map((item, i) => `
        <tr>
            <td>${esc(item.mpn)}</td>
            <td>${esc(item.manufacturer || '—')}</td>
            <td>${(item.qty||0).toLocaleString()}</td>
            <td class="quote-cost">$${Number(item.cost_price||0).toFixed(4)}</td>
            <td>${item.target_price != null ? '$'+Number(item.target_price).toFixed(4) : '—'}</td>
            <td><input type="number" step="0.0001" class="quote-sell-input" value="${item.sell_price||0}" onchange="updateQuoteLine(${i},this.value)"></td>
            <td class="quote-margin" id="qm-${i}">${Number(item.margin_pct||0).toFixed(1)}%</td>
            <td>${esc(item.lead_time || '—')}</td>
        </tr>`).join('');

    const statusActions = {
        draft: '<button class="btn btn-ghost" onclick="saveQuoteDraft()">Save Draft</button> <button class="btn btn-ghost" onclick="copyQuoteTable()">📋 Copy Quote Table</button> <button class="btn btn-primary" onclick="markQuoteSent()">Mark Sent</button>',
        sent: '<button class="btn btn-success" onclick="markQuoteResult(\'won\')">Mark Won</button> <button class="btn btn-danger" onclick="openLostModal()">Mark Lost</button> <button class="btn btn-ghost" onclick="reviseQuote()">Revise</button>',
        won: '<p style="color:var(--green);font-weight:600">✓ Won — $' + Number(q.won_revenue||0).toLocaleString() + '</p>',
        lost: '<p style="color:var(--red);font-weight:600">✗ Lost — ' + esc(q.result_reason||'') + '</p> <button class="btn btn-ghost" onclick="reopenQuote(false)">Reopen Quote</button> <button class="btn btn-ghost" onclick="reopenQuote(true)">Reopen &amp; Revise</button>',
        revised: '<p style="color:var(--muted)">Superseded by Rev ' + (q.revision + 1) + '</p>',
    };

    el.innerHTML = `
    <div class="quote-header">
        <div>
            <strong>${esc(q.quote_number)} Rev ${q.revision}</strong>
            <span class="status-badge status-${q.status}">${q.status}</span>
        </div>
        <div style="color:var(--text2);font-size:12px">
            ${esc(q.customer_name || '')}<br>
            ${esc(q.contact_name || '')} · ${q.contact_email ? '<a href="mailto:'+esc(q.contact_email)+'">'+esc(q.contact_email)+'</a>' : ''}
        </div>
    </div>
    <table class="tbl quote-table">
        <thead><tr><th>MPN</th><th>Mfr</th><th>Qty</th><th>Cost</th><th>Target</th><th>Sell</th><th>Margin</th><th>Lead</th></tr></thead>
        <tbody>${lines}</tbody>
    </table>
    <div class="quote-markup">
        Quick Markup: <input type="number" id="quickMarkup" value="20" style="width:50px" min="0" max="100">%
        <button class="btn btn-ghost btn-sm" onclick="applyMarkup()">Apply to All</button>
    </div>
    <div class="quote-totals">
        <div>Cost: <strong>$${Number(q.total_cost||0).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
        <div>Sell: <strong>$${Number(q.subtotal||0).toLocaleString(undefined,{minimumFractionDigits:2})}</strong></div>
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
    <div id="buyPlanSection"></div>`;
}

function updateQuoteLine(idx, newSellPrice) {
    if (!crmQuote) return;
    const item = crmQuote.line_items[idx];
    item.sell_price = parseFloat(newSellPrice) || 0;
    const cost = item.cost_price || 0;
    item.margin_pct = item.sell_price > 0 ? ((item.sell_price - cost) / item.sell_price * 100) : 0;
    const mEl = document.getElementById('qm-' + idx);
    if (mEl) mEl.textContent = item.margin_pct.toFixed(1) + '%';
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
    try {
        const res = await fetch('/api/quotes/' + crmQuote.id, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                line_items: crmQuote.line_items,
                payment_terms: document.getElementById('qtTerms').value,
                shipping_terms: document.getElementById('qtShip').value,
                validity_days: parseInt(document.getElementById('qtValid').value) || 7,
                notes: document.getElementById('qtNotes').value,
            })
        });
        if (!res.ok) { showToast('Failed to save draft', 'error'); return; }
        showToast('Draft saved', 'success');
        loadQuote();
    } catch (e) { console.error('saveQuoteDraft:', e); showToast('Error saving draft', 'error'); }
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
    });
}

async function markQuoteSent() {
    if (!crmQuote) return;
    try {
        await saveQuoteDraft();
        const res = await fetch('/api/quotes/' + crmQuote.id + '/send', { method: 'POST' });
        if (!res.ok) { showToast('Failed to mark as sent', 'error'); return; }
        const sendData = await res.json();
        showToast('Quote marked as sent', 'success');
        notifyStatusChange(sendData);
        loadQuote();
    } catch (e) { console.error('markQuoteSent:', e); showToast('Error sending quote', 'error'); }
}

async function markQuoteResult(result) {
    if (!crmQuote) return;
    if (result === 'won') {
        // Open buy plan modal instead of simple confirm
        openBuyPlanModal();
        return;
    }
    try {
        const res = await fetch('/api/quotes/' + crmQuote.id + '/result', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ result })
        });
        if (!res.ok) { showToast('Failed to update result', 'error'); return; }
        const resultData = await res.json();
        showToast('Quote updated', 'info');
        notifyStatusChange(resultData);
        loadQuote();
    } catch (e) { console.error('markQuoteResult:', e); showToast('Error updating result', 'error'); }
}

function openLostModal() {
    document.getElementById('lostModal').classList.add('open');
}

// ── Buy Plan ────────────────────────────────────────────────────────────

let _currentBuyPlan = null;

function openBuyPlanModal() {
    if (!crmQuote) return;
    const modal = document.getElementById('buyPlanModal');
    modal.classList.add('open');
    const items = (crmQuote.line_items || []).map((item, i) => `
        <tr>
            <td><input type="checkbox" class="bp-check" data-idx="${i}" checked></td>
            <td>${esc(item.mpn)}</td>
            <td>${esc(item.manufacturer || '—')}</td>
            <td>${(item.qty||0).toLocaleString()}</td>
            <td>$${Number(item.cost_price||0).toFixed(4)}</td>
            <td>$${(Number(item.qty||0) * Number(item.cost_price||0)).toFixed(2)}</td>
            <td>${esc(item.lead_time || '—')}</td>
        </tr>
    `).join('');
    document.getElementById('bpItems').innerHTML = items;
    document.getElementById('bpTotal').textContent = '$' + Number(crmQuote.total_cost||0).toLocaleString();
}

async function submitBuyPlan() {
    if (!crmQuote) return;
    const checks = document.querySelectorAll('.bp-check:checked');
    const selectedIndices = Array.from(checks).map(c => parseInt(c.dataset.idx));
    if (!selectedIndices.length) { showToast('Select at least one item', 'error'); return; }

    // Get offer IDs from line items
    const offerIds = selectedIndices
        .map(i => (crmQuote.line_items || [])[i]?.offer_id)
        .filter(Boolean);
    if (!offerIds.length) { showToast('No offer IDs found', 'error'); return; }

    try {
        const res = await apiFetch('/api/quotes/' + crmQuote.id + '/buy-plan', {
            method: 'POST', body: { offer_ids: offerIds }
        });
        showToast('Buy plan submitted for approval!', 'success');
        closeModal('buyPlanModal');
        notifyStatusChange(res);
        loadQuote();
    } catch (e) {
        console.error('submitBuyPlan:', e);
        showToast('Failed to submit buy plan', 'error');
    }
}

async function loadBuyPlan() {
    if (!crmQuote) return;
    try {
        _currentBuyPlan = await apiFetch('/api/buy-plans/for-quote/' + crmQuote.id);
    } catch (e) { _currentBuyPlan = null; }
    renderBuyPlanStatus();
}

function renderBuyPlanStatus() {
    const el = document.getElementById('buyPlanSection');
    if (!el) return;
    if (!_currentBuyPlan) { el.innerHTML = ''; return; }
    const bp = _currentBuyPlan;
    const isAdmin = window.userRole === 'admin' || (window.userEmail && window.userEmail.toLowerCase() === 'mkhoury@trioscs.com');
    const isBuyer = window.userRole === 'buyer';

    const statusColors = {
        pending_approval: '#f59e0b',
        approved: '#16a34a',
        rejected: '#dc2626',
        po_entered: '#2563eb',
        po_confirmed: '#16a34a',
        complete: '#16a34a',
    };
    const statusLabels = {
        pending_approval: 'Pending Approval',
        approved: 'Approved — Awaiting PO',
        rejected: 'Rejected',
        po_entered: 'PO Entered — Verifying',
        po_confirmed: 'PO Confirmed',
        complete: 'Complete',
    };

    const statusColor = statusColors[bp.status] || 'var(--muted)';
    const statusLabel = statusLabels[bp.status] || bp.status;

    let itemsHtml = (bp.line_items || []).map((item, i) => {
        const poCell = (isBuyer && bp.status === 'approved') || (isBuyer && bp.status === 'po_entered' && !item.po_number)
            ? `<input type="text" class="po-input" data-idx="${i}" placeholder="PO#" value="${esc(item.po_number||'')}" style="width:100px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px">`
            : (item.po_number ? `<span style="font-weight:600">${esc(item.po_number)}</span>` : '—');
        const verifyIcon = item.po_verified
            ? '<span style="color:#16a34a" title="Verified">✓</span>'
            : (item.po_number ? '<span style="color:#f59e0b" title="Unverified">⏳</span>' : '');
        const poDetails = item.po_verified
            ? `<div style="font-size:10px;color:var(--muted)">Sent to ${esc(item.po_recipient||'')} at ${item.po_sent_at||''}</div>`
            : '';
        return `<tr>
            <td>${esc(item.mpn)}</td>
            <td>${esc(item.vendor_name)}</td>
            <td>${(item.qty||0).toLocaleString()}</td>
            <td>$${Number(item.cost_price||0).toFixed(4)}</td>
            <td>${esc(item.lead_time||'—')}</td>
            <td>${poCell} ${verifyIcon}${poDetails}</td>
        </tr>`;
    }).join('');

    let actionsHtml = '';
    if (isAdmin && bp.status === 'pending_approval') {
        actionsHtml = `
            <div style="display:flex;gap:8px;margin-top:12px">
                <textarea id="bpManagerNotes" placeholder="Manager notes (optional)…" style="flex:1;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;min-height:40px"></textarea>
            </div>
            <div style="display:flex;gap:8px;margin-top:8px">
                <button class="btn btn-success" onclick="approveBuyPlan()">Approve</button>
                <button class="btn btn-danger" onclick="openRejectBuyPlanModal()">Reject</button>
            </div>`;
    }
    if (isBuyer && (bp.status === 'approved' || bp.status === 'po_entered')) {
        actionsHtml = `
            <div style="margin-top:12px">
                <button class="btn btn-primary" onclick="saveBuyPlanPOs()">Save PO Numbers</button>
                <button class="btn btn-ghost" onclick="verifyBuyPlanPOs()">Verify PO Sent</button>
            </div>`;
    }

    el.innerHTML = `
        <div class="card" style="margin-top:16px;border-left:4px solid ${statusColor}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div>
                    <strong>Buy Plan</strong>
                    <span class="status-badge" style="background:${statusColor};color:#fff;margin-left:8px">${statusLabel}</span>
                </div>
                <span style="font-size:11px;color:var(--muted)">Submitted by ${esc(bp.submitted_by||'')} ${bp.submitted_at ? '· '+fmtDateTime(bp.submitted_at) : ''}</span>
            </div>
            ${bp.manager_notes ? '<p style="font-size:12px;color:var(--text2);margin-bottom:8px"><em>Manager: '+esc(bp.manager_notes)+'</em></p>' : ''}
            ${bp.rejection_reason ? '<p style="font-size:12px;color:#dc2626;margin-bottom:8px"><strong>Rejected:</strong> '+esc(bp.rejection_reason)+'</p>' : ''}
            <table class="tbl" style="margin-bottom:0">
                <thead><tr><th>MPN</th><th>Vendor</th><th>Qty</th><th>Cost</th><th>Lead</th><th>PO</th></tr></thead>
                <tbody>${itemsHtml}</tbody>
            </table>
            ${actionsHtml}
        </div>`;
}

async function approveBuyPlan() {
    if (!_currentBuyPlan) return;
    const notes = document.getElementById('bpManagerNotes')?.value?.trim() || '';
    try {
        await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/approve', {
            method: 'PUT', body: { manager_notes: notes }
        });
        showToast('Buy plan approved — buyers notified', 'success');
        loadBuyPlan();
    } catch (e) { showToast('Failed to approve', 'error'); }
}

function openRejectBuyPlanModal() {
    const reason = prompt('Rejection reason:');
    if (reason === null) return;
    rejectBuyPlan(reason);
}

async function rejectBuyPlan(reason) {
    if (!_currentBuyPlan) return;
    try {
        await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/reject', {
            method: 'PUT', body: { reason }
        });
        showToast('Buy plan rejected', 'info');
        loadBuyPlan();
    } catch (e) { showToast('Failed to reject', 'error'); }
}

async function saveBuyPlanPOs() {
    if (!_currentBuyPlan) return;
    const inputs = document.querySelectorAll('.po-input');
    let saved = 0;
    for (const input of inputs) {
        const idx = parseInt(input.dataset.idx);
        const po = input.value.trim();
        if (!po) continue;
        try {
            await apiFetch('/api/buy-plans/' + _currentBuyPlan.id + '/po', {
                method: 'PUT', body: { line_index: idx, po_number: po }
            });
            saved++;
        } catch (e) { console.error('Failed to save PO for line', idx, e); }
    }
    if (saved) showToast(saved + ' PO number(s) saved', 'success');
    loadBuyPlan();
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
    document.querySelectorAll('[data-bp-status]').forEach(b => b.classList.remove('on'));
    if (btn) btn.classList.add('on');
    loadBuyPlans();
}

function renderBuyPlansList() {
    const el = document.getElementById('buyPlansList');
    if (!_buyPlans.length) {
        el.innerHTML = '<p class="empty">No buy plans found</p>';
        return;
    }
    const statusColors = {
        pending_approval: 'var(--amber)',
        approved: 'var(--green)',
        rejected: 'var(--red)',
        po_entered: '#2563eb',
        po_confirmed: 'var(--green)',
        complete: 'var(--green)',
    };
    const statusLabels = {
        pending_approval: 'Pending',
        approved: 'Approved',
        rejected: 'Rejected',
        po_entered: 'PO Entered',
        po_confirmed: 'Confirmed',
        complete: 'Complete',
    };
    el.innerHTML = _buyPlans.map(bp => {
        const color = statusColors[bp.status] || 'var(--muted)';
        const label = statusLabels[bp.status] || bp.status;
        const itemCount = (bp.line_items || []).length;
        const total = (bp.line_items || []).reduce((s, li) => s + (Number(li.qty)||0) * (Number(li.cost_price)||0), 0);
        return `
        <div class="card card-clickable" style="border-left:4px solid ${color}" onclick="openBuyPlanDetail(${bp.id})">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                    <strong>${esc(bp.requisition_name || 'Requisition #' + bp.requisition_id)}</strong>
                    <span class="status-badge" style="background:${color};color:#fff;margin-left:8px;font-size:10px">${label}</span>
                </div>
                <span style="font-size:11px;color:var(--muted)">${bp.submitted_at ? fmtDateTime(bp.submitted_at) : ''}</span>
            </div>
            <div style="font-size:12px;color:var(--text2);margin-top:6px">
                ${itemCount} item${itemCount !== 1 ? 's' : ''} · $${total.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}
                · Submitted by ${esc(bp.submitted_by || '—')}
                ${bp.approved_by ? ' · Approved by ' + esc(bp.approved_by) : ''}
            </div>
        </div>`;
    }).join('');
}

async function openBuyPlanDetail(planId) {
    try {
        _currentBuyPlan = await apiFetch('/api/buy-plans/' + planId);
    } catch (e) { showToast('Failed to load buy plan', 'error'); return; }
    // Re-render inline — reuse renderBuyPlanStatus into a modal-like overlay
    const el = document.getElementById('buyPlansList');
    const backBtn = `<button class="btn btn-ghost" onclick="loadBuyPlans()" style="margin-bottom:12px">← Back to list</button>`;
    el.innerHTML = backBtn;
    const section = document.createElement('div');
    section.id = 'buyPlanSection';
    el.appendChild(section);
    renderBuyPlanStatus();
}

async function submitLost() {
    if (!crmQuote) return;
    try {
        const res = await fetch('/api/quotes/' + crmQuote.id + '/result', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                result: 'lost',
                reason: document.getElementById('lostReason').value,
                notes: document.getElementById('lostNotes').value,
            })
        });
        if (!res.ok) { showToast('Failed to submit', 'error'); return; }
        const lostData = await res.json();
        closeModal('lostModal');
        showToast('Quote marked as lost', 'info');
        notifyStatusChange(lostData);
        loadQuote();
    } catch (e) { console.error('submitLost:', e); showToast('Error submitting', 'error'); }
}

async function reviseQuote() {
    if (!crmQuote) return;
    try {
        const res = await fetch('/api/quotes/' + crmQuote.id + '/revise', { method: 'POST' });
        if (!res.ok) { showToast('Failed to revise', 'error'); return; }
        crmQuote = await res.json();
        showToast('New revision created', 'success');
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) { console.error('reviseQuote:', e); showToast('Error revising quote', 'error'); }
}

async function reopenQuote(revise) {
    if (!crmQuote) return;
    try {
        const res = await fetch('/api/quotes/' + crmQuote.id + '/reopen', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ revise: revise })
        });
        if (!res.ok) { showToast('Failed to reopen', 'error'); return; }
        crmQuote = await res.json();
        showToast(revise ? 'Quote reopened with new revision' : 'Quote reopened', 'success');
        renderQuote();
        updateQuoteTabBadge();
    } catch (e) { console.error('reopenQuote:', e); showToast('Error reopening quote', 'error'); }
}

// ── Pricing History ────────────────────────────────────────────────────

async function openPricingHistory(mpn) {
    document.getElementById('phModal').classList.add('open');
    document.getElementById('phMpn').textContent = mpn;
    document.getElementById('phContent').innerHTML = '<p class="empty">Loading...</p>';
    try {
        const res = await fetch('/api/pricing-history/' + encodeURIComponent(mpn));
        if (!res.ok) { document.getElementById('phContent').innerHTML = '<p class="empty">Error loading</p>'; return; }
        const data = await res.json();
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
    } catch (e) { console.error('openPricingHistory:', e); document.getElementById('phContent').innerHTML = '<p class="empty">Error loading pricing</p>'; }
}

// ── Clone Requisition ──────────────────────────────────────────────────

async function cloneRequisition(reqId) {
    if (!confirm('Clone this requisition? All parts and offers will be copied.')) return;
    try {
        const res = await fetch('/api/requisitions/' + reqId + '/clone', { method: 'POST' });
        if (!res.ok) { showToast('Failed to clone', 'error'); return; }
        const data = await res.json();
        showToast('Requisition cloned', 'success');
        showDetail(data.id, data.name);
    } catch (e) { console.error('cloneRequisition:', e); showToast('Error cloning requisition', 'error'); }
}

// ── User list loader for owner dropdowns ──────────────────────────────
let _userListCache = null;
async function loadUserOptions(selectId) {
    try {
        if (!_userListCache) {
            const res = await fetch('/api/users/list');
            if (res.ok) _userListCache = await res.json();
            else _userListCache = [];
        }
        const sel = document.getElementById(selectId);
        if (!sel) return;
        sel.innerHTML = '<option value="">— None —</option>' +
            _userListCache.map(u => '<option value="' + u.id + '">' + esc(u.name) + ' (' + u.role + ')</option>').join('');
    } catch (e) { console.error('loadUserOptions:', e); }
}

// ── Customer site typeahead for req creation ──────────────────────────
let _siteListCache = null;
async function loadSiteOptions() {
    try {
        const res = await fetch('/api/companies');
        if (!res.ok) return;
        const companies = await res.json();
        _siteListCache = [];
        companies.forEach(c => {
            (c.sites || []).forEach(s => {
                _siteListCache.push({
                    id: s.id,
                    label: c.name + ' — ' + s.site_name,
                    companyName: c.name,
                    siteName: s.site_name,
                });
            });
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
    if (matches.length === 0) {
        list.innerHTML = '<div class="site-typeahead-item" style="color:var(--muted)">No matches</div>';
    } else {
        list.innerHTML = matches.map(s =>
            '<div class="site-typeahead-item" onclick="selectSite(' + s.id + ',\'' + escAttr(s.label) + '\')">' + esc(s.label) + '</div>'
        ).join('');
    }
    list.classList.add('show');
}

function selectSite(id, label) {
    document.getElementById('nrSiteId').value = id;
    document.getElementById('nrSiteSearch').value = label;
    document.getElementById('nrSiteList').classList.remove('show');
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
    document.getElementById('suggestedContactsModal').classList.add('open');
    setTimeout(() => document.getElementById('scTitleFilter').focus(), 100);
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
        const res = await fetch(url);
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            el.innerHTML = `<p class="empty" style="padding:12px">${esc(err.detail || 'Search failed')}</p>`;
            return;
        }
        const data = await res.json();
        scResults = data.contacts || [];
        renderSuggestedContacts();
    } catch (e) {
        el.innerHTML = '<p class="empty" style="padding:12px">Error searching contacts</p>';
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
                    ${c.email ? '<span>✉ ' + esc(c.email) + '</span>' : ''}
                    ${c.phone ? '<span>☎ ' + esc(c.phone) + '</span>' : ''}
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
            const res = await fetch('/api/suggested-contacts/add-to-vendor', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ vendor_card_id: scContext.id, contacts })
            });
            if (!res.ok) { showToast('Failed to add contacts', 'error'); return; }
            const data = await res.json();
            showToast(`${data.added} contact${data.added !== 1 ? 's' : ''} added`, 'success');
            closeModal('suggestedContactsModal');
            openVendorPopup(scContext.id);  // Refresh vendor popup
        } else if (scContext.type === 'site') {
            const c = contacts[0];
            const res = await fetch('/api/suggested-contacts/add-to-site', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ site_id: scContext.id, contact: c })
            });
            if (!res.ok) { showToast('Failed to set contact', 'error'); return; }
            showToast('Contact set on site', 'success');
            closeModal('suggestedContactsModal');
            loadCustomers();
        }
    } catch (e) {
        console.error('addSelectedSuggestedContacts:', e);
        showToast('Error adding contacts', 'error');
    }
}


// ── Company Enrichment ────────────────────────────────────────────────

async function enrichCompany(companyId, domain) {
    if (!domain) {
        domain = prompt('Enter company domain (e.g. ibm.com):');
        if (!domain) return;
    }
    showToast('Enriching…', 'info');
    try {
        const res = await fetch('/api/enrich/company/' + companyId, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ domain })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.detail || 'Enrichment failed', 'error');
            return;
        }
        const data = await res.json();
        showToast(`Updated ${data.updated_fields.length} fields`, 'success');
        loadCustomers();
    } catch (e) {
        console.error('enrichCompany:', e);
        showToast('Enrichment error', 'error');
    }
}

async function enrichVendor(cardId, domain) {
    if (!domain) {
        domain = prompt('Enter vendor domain (e.g. arrow.com):');
        if (!domain) return;
    }
    showToast('Enriching…', 'info');
    try {
        const res = await fetch('/api/enrich/vendor/' + cardId, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ domain })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.detail || 'Enrichment failed', 'error');
            return;
        }
        const data = await res.json();
        showToast(`Updated ${data.updated_fields.length} fields`, 'success');
        openVendorPopup(cardId);
    } catch (e) {
        console.error('enrichVendor:', e);
        showToast('Enrichment error', 'error');
    }
}


// ── Init CRM on page load ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    loadUserOptions('asSiteOwner');
    loadSiteOptions();
    initNameAutocomplete('loVendor', 'loVendorList', null, { types: 'vendor', websiteId: 'loWebsite' });
    initNameAutocomplete('ncName', 'ncNameList', null, { types: 'all' });
});


// ══════════════════════════════════════════════════════════════════════
// Intelligence Layer — AI-powered features
// ══════════════════════════════════════════════════════════════════════

// ── Feature 1: AI Contact Enrichment ──────────────────────────────────

async function findAIContacts(entityType, entityId, companyName, domain) {
    const btn = event?.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Searching…'; }
    try {
        const res = await fetch('/api/ai/find-contacts', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                entity_type: entityType,
                entity_id: entityId,
                company_name: companyName,
                domain: domain || null,
            })
        });
        if (res.status === 403) {
            showToast('AI features not enabled for your account', 'error');
            return;
        }
        if (!res.ok) {
            showToast('Contact search failed', 'error');
            return;
        }
        const data = await res.json();
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

function openAIContactsPanel(contacts, entityType, entityId) {
    const old = document.getElementById('aiContactsBg');
    if (old) old.remove();

    const bg = document.createElement('div');
    bg.id = 'aiContactsBg';
    bg.className = 'ai-panel-bg';
    bg.onclick = e => { if (e.target === bg) bg.remove(); };
    bg.innerHTML = `
        <div class="ai-panel">
            <div class="ai-panel-header">
                <h3>AI-Found Contacts <span style="font-size:11px;color:var(--muted);font-weight:400">(${contacts.length})</span></h3>
                <button class="btn-close-ai" onclick="document.getElementById('aiContactsBg').remove()">✕</button>
            </div>
            <div style="max-height:400px;overflow-y:auto">
                ${contacts.map(c => `
                    <div class="ai-contact-row">
                        <div class="ai-contact-info">
                            <div class="ai-contact-name">${esc(c.full_name)}</div>
                            <div class="ai-contact-title">${esc(c.title || 'No title')}</div>
                            <div class="ai-contact-meta">
                                ${c.email ? `<span>✉ ${esc(c.email)}</span>` : ''}
                                ${c.phone ? `<span>☎ ${esc(c.phone)}</span>` : ''}
                                ${c.linkedin_url ? `<a href="${escAttr(c.linkedin_url)}" target="_blank">LinkedIn ↗</a>` : ''}
                            </div>
                        </div>
                        <div class="ai-contact-actions">
                            <span class="badge ${c.confidence === 'high' ? 'badge-green' : c.confidence === 'medium' ? 'badge-yellow' : 'badge-gray'}"
                                  title="Source: ${esc(c.source)}">${esc(c.confidence)}</span>
                            ${!c.is_saved ? `<button class="btn btn-ghost btn-sm" id="aiSave${c.id}" onclick="saveAIContact(${c.id})" title="Save contact">Save</button>` : '<span class="badge badge-green">Saved</span>'}
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
        const res = await fetch(`/api/ai/prospect-contacts/${contactId}/save`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });
        if (res.ok) {
            showToast('Contact saved', 'success');
            if (btn) { btn.outerHTML = '<span class="badge badge-green">Saved</span>'; }
        } else {
            showToast('Save failed', 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
        }
    } catch (e) {
        showToast('Save error', 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
    }
}


// ── Feature 2: Response Parse Preview ─────────────────────────────────

async function parseResponseAI(responseId) {
    const btn = event?.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Parsing…'; }
    try {
        const res = await fetch(`/api/ai/parse-response/${responseId}`, { method: 'POST' });
        if (res.status === 403) {
            showToast('AI features not enabled', 'error');
            return;
        }
        if (!res.ok) {
            showToast('Parse failed', 'error');
            return;
        }
        const data = await res.json();
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
                    <button class="btn btn-primary" onclick="saveParsedOffers(${responseId}, ${escAttr(JSON.stringify(data.draft_offers))})">
                        Save ${data.draft_offers.length} Offer(s)
                    </button>` : ''}
                <button class="btn btn-ghost" onclick="document.getElementById('parseBg').remove()">Close</button>
            </div>
        </div>
    `;
    document.body.appendChild(bg);
}

async function saveParsedOffers(responseId, offers) {
    if (typeof offers === 'string') offers = JSON.parse(offers);
    if (!currentReqId) {
        showToast('No requisition selected', 'error');
        return;
    }
    try {
        const res = await fetch('/api/ai/save-parsed-offers', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ response_id: responseId, offers, requisition_id: currentReqId })
        });
        if (res.ok) {
            const data = await res.json();
            showToast(`Saved ${data.created} offer(s) — review in Offers tab`, 'success');
            document.getElementById('parseBg')?.remove();
            loadOffers();
        } else {
            showToast('Failed to save offers', 'error');
        }
    } catch (e) {
        showToast('Save error', 'error');
    }
}


// ── Upgrade 2: Parse Response Attachments ────────────────────────────

async function parseResponseAttachments(responseId) {
    const btn = event ? event.target : null;
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Parsing…'; }
    try {
        const resp = await fetch(`/api/email-mining/parse-response-attachments/${responseId}`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (!resp.ok) {
            showToast(data.error || 'Attachment parse failed', 'error');
            return;
        }
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
        const res = await fetch('/api/ai/company-intel?' + params);
        if (res.status === 403 || !res.ok) {
            targetEl.innerHTML = '';
            return;
        }
        const data = await res.json();
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


// ── Feature 4: Smart RFQ Draft ────────────────────────────────────────

async function generateSmartRFQ(vendorName, parts) {
    try {
        const res = await fetch('/api/ai/draft-rfq', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ vendor_name: vendorName, parts })
        });
        if (res.status === 403) return null;
        if (!res.ok) return null;
        const data = await res.json();
        return data.available ? data.body : null;
    } catch (e) {
        return null;
    }
}

// ── Site Contacts CRUD ─────────────────────────────────────────────────

function openAddSiteContact(siteId) {
    document.getElementById('scSiteId').value = siteId;
    document.getElementById('scContactId').value = '';
    document.getElementById('siteContactModalTitle').textContent = 'Add Contact';
    ['scFullName','scTitle','scEmail','scPhone','scNotes'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('scPrimary').checked = false;
    document.getElementById('siteContactModal').classList.add('open');
    setTimeout(() => document.getElementById('scFullName').focus(), 100);
}

async function openEditSiteContact(siteId, contactId) {
    try {
        const res = await fetch('/api/sites/' + siteId + '/contacts');
        if (!res.ok) { showToast('Failed to load contacts', 'error'); return; }
        const contacts = await res.json();
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
        document.getElementById('siteContactModal').classList.add('open');
        setTimeout(() => document.getElementById('scFullName').focus(), 100);
    } catch (e) { console.error('openEditSiteContact:', e); showToast('Error loading contact', 'error'); }
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
        const res = await fetch(url, {
            method: contactId ? 'PUT' : 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        if (!res.ok) { showToast('Failed to save contact', 'error'); return; }
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
        const res = await fetch('/api/sites/' + siteId + '/contacts/' + contactId, { method: 'DELETE' });
        if (!res.ok) { showToast('Failed to delete contact', 'error'); return; }
        showToast('Contact removed', 'info');
        const panel = document.getElementById('siteDetail-' + siteId);
        if (panel) { panel.style.display = 'none'; toggleSiteDetail(siteId); }
    } catch (e) { console.error('deleteSiteContact:', e); showToast('Error deleting contact', 'error'); }
}


// Generate smart draft for the currently selected first vendor in RFQ modal
async function generateSmartRFQForModal() {
    const btn = event?.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
    try {
        // Gather all parts from the current requisition
        const parts = (window._currentRequirements || []).map(r => r.mpn);
        // Use first selected vendor
        const firstVendor = rfqVendorData.find(v => v.selected);
        if (!firstVendor || !parts.length) {
            showToast('No vendor or parts selected', 'info');
            return;
        }
        const body = await generateSmartRFQ(firstVendor.name, parts);
        if (body) {
            document.getElementById('rfqBody').value = body;
            showToast('Smart draft applied', 'success');
        } else {
            showToast('AI draft unavailable — using template', 'info');
        }
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🤖 Smart Draft'; }
    }
}

// ── Company Activity Tracking ─────────────────────────────────────────

function toggleCompanyCard(cardEl, companyId) {
    cardEl.classList.toggle('expanded');
    if (cardEl.classList.contains('expanded')) {
        loadCompanyActivityStatus(companyId);
        loadCompanyActivities(companyId);
    }
}

async function loadCompanyActivityStatus(companyId) {
    const el = document.getElementById('actHealth-' + companyId);
    if (!el || el.dataset.loaded) return;
    try {
        const res = await fetch('/api/companies/' + companyId + '/activity-status');
        if (!res.ok) return;
        const d = await res.json();
        const colors = { green: 'var(--green)', yellow: 'var(--amber)', red: 'var(--red)', no_activity: 'var(--muted)' };
        const labels = { green: 'Active', yellow: 'At risk', red: 'Stale', no_activity: 'No activity' };
        const daysText = d.days_since_activity != null ? ' (' + d.days_since_activity + 'd)' : '';
        el.innerHTML = `<span class="badge" style="background:color-mix(in srgb,${colors[d.status]} 15%,transparent);color:${colors[d.status]};font-size:9px;padding:1px 6px;border-radius:8px">${labels[d.status]}${daysText}</span>`;
        el.dataset.loaded = '1';
    } catch(e) { console.error('loadActivityStatus:', e); }
}

async function loadCompanyActivities(companyId) {
    const section = document.getElementById('actSection-' + companyId);
    const el = document.getElementById('actList-' + companyId);
    if (!section || !el) return;
    section.style.display = 'block';
    try {
        const res = await fetch('/api/companies/' + companyId + '/activities');
        if (!res.ok) { el.innerHTML = '<p class="empty" style="font-size:11px">Failed to load</p>'; return; }
        const activities = await res.json();
        if (!activities.length) {
            el.innerHTML = '<p class="empty" style="padding:4px;font-size:11px">No activity recorded yet</p>';
            return;
        }
        el.innerHTML = activities.slice(0, 10).map(a => {
            const icons = { email_sent: '&#x1f4e4;', email_received: '&#x1f4e5;', call_outbound: '&#x1f4de;', call_inbound: '&#x1f4f2;', ownership_warning: '&#x26a0;&#xfe0f;' };
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
                </div>
                <span class="act-row-meta">${esc(a.user_name || '')}</span>
                <span class="act-row-meta">${typeof fmtRelative === 'function' ? fmtRelative(a.created_at) : (a.created_at || '').slice(0, 10)}</span>
            </div>`;
        }).join('');
    } catch(e) { console.error('loadCompanyActivities:', e); el.innerHTML = '<p class="empty" style="font-size:11px">Error</p>'; }
}

function openLogCallModal(companyId, companyName) {
    document.getElementById('lcCompanyId').value = companyId;
    document.getElementById('lcCompanyName').textContent = companyName;
    ['lcPhone','lcContactName','lcDuration'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('lcDirection').value = 'outbound';
    document.getElementById('logCallModal').classList.add('open');
    setTimeout(() => document.getElementById('lcPhone').focus(), 100);
}

async function saveLogCall() {
    const companyId = document.getElementById('lcCompanyId').value;
    const data = {
        phone: document.getElementById('lcPhone').value.trim(),
        contact_name: document.getElementById('lcContactName').value.trim() || null,
        direction: document.getElementById('lcDirection').value,
        duration_seconds: parseInt(document.getElementById('lcDuration').value) || null,
    };
    if (!data.phone) { showToast('Phone number is required', 'error'); return; }
    try {
        const res = await fetch('/api/activities/call', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        if (!res.ok) { showToast('Failed to log call', 'error'); return; }
        const result = await res.json();
        closeModal('logCallModal');
        if (result.status === 'no_match') {
            showToast('Call logged but phone did not match any known contact', 'info');
        } else {
            showToast('Call logged', 'success');
        }
        // Refresh activity section
        const el = document.getElementById('actList-' + companyId);
        if (el) el.innerHTML = '<p class="empty" style="padding:4px;font-size:11px">Loading...</p>';
        loadCompanyActivities(parseInt(companyId));
        const healthEl = document.getElementById('actHealth-' + companyId);
        if (healthEl) { delete healthEl.dataset.loaded; loadCompanyActivityStatus(parseInt(companyId)); }
    } catch(e) { console.error('saveLogCall:', e); showToast('Error logging call', 'error'); }
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
    } catch (e) { _proactiveSiteContacts = []; }

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
            <td><input type="number" step="0.0001" class="ps-sell" data-id="${m.id}" value="${defaultSell}" style="width:90px;padding:4px;border:1px solid var(--border);border-radius:4px;font-size:11px" oninput="updateProactivePreview()"></td>
            <td class="ps-margin" data-id="${m.id}"></td>
        </tr>`;
    }).join('');
    updateProactivePreview();

    document.getElementById('proactiveSendModal').classList.add('open');
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

    const sellPrices = {};
    document.querySelectorAll('.ps-sell').forEach(input => {
        sellPrices[input.dataset.id] = parseFloat(input.value) || 0;
    });

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
    try {
        const result = await apiFetch('/api/proactive/convert/' + offerId, { method: 'POST' });
        showToast('Converted! Requisition #' + result.requisition_id + ' created with buy plan.', 'success');
        loadProactiveSent();
    } catch (e) { showToast('Conversion failed', 'error'); }
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
    switchPerfTab('vendors');
}

function switchPerfTab(tab, btn) {
    document.querySelectorAll('#perfTabs .tab').forEach(t => t.classList.remove('on'));
    if (btn) btn.classList.add('on');
    else document.querySelector(`#perfTabs .tab[onclick*="${tab}"]`)?.classList.add('on');
    document.getElementById('perfVendorPanel').style.display = tab === 'vendors' ? '' : 'none';
    document.getElementById('perfBuyerPanel').style.display = tab === 'buyers' ? '' : 'none';
    if (tab === 'vendors') loadVendorScorecards();
    else loadBuyerLeaderboard();
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

    function sortIcon(col) {
        if (col !== _perfVendorSort) return '';
        return _perfVendorOrder === 'desc' ? ' ▼' : ' ▲';
    }
    function toggleSort(col) {
        if (_perfVendorSort === col) _perfVendorOrder = _perfVendorOrder === 'desc' ? 'asc' : 'desc';
        else { _perfVendorSort = col; _perfVendorOrder = 'desc'; }
        loadVendorScorecards();
    }

    function metricCell(val, invert) {
        if (val === null || val === undefined) return '<td class="metric-cell na">N/A</td>';
        const displayed = invert ? val : val;
        const score = invert ? 1 - val : val;
        let cls = 'metric-red';
        if (score >= 0.7) cls = 'metric-green';
        else if (score >= 0.4) cls = 'metric-yellow';
        return `<td class="metric-cell ${cls}">${(displayed * 100).toFixed(0)}%</td>`;
    }

    // Make toggleSort available globally
    window._perfToggleSort = toggleSort;

    const searchBar = `<div style="margin-bottom:10px"><input type="text" id="perfVendorSearch" placeholder="Search vendors..." value="${document.getElementById('perfVendorSearch')?.value||''}" class="filter-search" oninput="loadVendorScorecards()" style="max-width:300px"></div>`;

    let html = searchBar + `<div style="overflow-x:auto"><table class="perf-table">
        <thead><tr>
            <th style="cursor:pointer" onclick="window._perfToggleSort('composite_score')">Vendor${sortIcon('composite_score')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('response_rate')">Response Rate${sortIcon('response_rate')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('quote_accuracy')">Quote Accuracy${sortIcon('quote_accuracy')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('on_time_delivery')">On-Time${sortIcon('on_time_delivery')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('cancellation_rate')">Cancel Rate${sortIcon('cancellation_rate')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('rma_rate')">RMA Rate${sortIcon('rma_rate')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('lead_time_accuracy')">Lead Time${sortIcon('lead_time_accuracy')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('quote_conversion')">Quote Rate${sortIcon('quote_conversion')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('po_conversion')">PO Rate${sortIcon('po_conversion')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('avg_review_rating')">Reviews${sortIcon('avg_review_rating')}</th>
            <th style="cursor:pointer" onclick="window._perfToggleSort('composite_score')">Score${sortIcon('composite_score')}</th>
        </tr></thead><tbody>`;

    for (const v of items) {
        if (!v.is_sufficient_data) {
            html += `<tr class="cold-start"><td>${v.vendor_name}</td><td colspan="10" class="metric-cell na" style="text-align:center;font-style:italic">Insufficient Data (${v.interaction_count} interactions)</td></tr>`;
            continue;
        }
        const reviewDisplay = v.avg_review_rating !== null && v.avg_review_rating !== undefined
            ? `<td class="metric-cell ${v.avg_review_rating >= 0.7 ? 'metric-green' : v.avg_review_rating >= 0.4 ? 'metric-yellow' : 'metric-red'}">${(v.avg_review_rating * 5).toFixed(1)}/5</td>`
            : '<td class="metric-cell na">N/A</td>';
        html += `<tr>
            <td><strong>${v.vendor_name}</strong></td>
            ${metricCell(v.response_rate)}
            ${metricCell(v.quote_accuracy)}
            ${metricCell(v.on_time_delivery)}
            ${metricCell(v.cancellation_rate, true)}
            ${metricCell(v.rma_rate, true)}
            ${metricCell(v.lead_time_accuracy)}
            ${metricCell(v.quote_conversion)}
            ${metricCell(v.po_conversion)}
            ${reviewDisplay}
            ${metricCell(v.composite_score)}
        </tr>`;
    }

    html += '</tbody></table></div>';
    if (window.__isAdmin) {
        html += `<div style="margin-top:10px"><button class="btn btn-ghost btn-sm" onclick="refreshVendorScorecards()">Refresh Scorecards</button></div>`;
    }
    el.innerHTML = html;
}

async function refreshVendorScorecards() {
    try {
        await apiFetch('/api/performance/vendors/refresh', {method:'POST'});
        loadVendorScorecards();
    } catch (e) {
        alert('Error refreshing: ' + (e.message || e));
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

    let monthSelector = `<select onchange="loadBuyerLeaderboard(this.value)" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:13px">`;
    for (const m of months) {
        const label = new Date(m + '-15').toLocaleDateString('en-US', {month:'long', year:'numeric'});
        monthSelector += `<option value="${m}" ${m === _leaderboardMonth ? 'selected' : ''}>${label}</option>`;
    }
    monthSelector += '</select>';

    // Summary cards
    const totalPts = entries.reduce((s, e) => s + e.total_points, 0);
    const topScorer = entries.length ? entries[0].user_name : '—';
    const totalOffers = entries.reduce((s, e) => s + e.offers_logged, 0);

    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
        <div>${monthSelector}</div>
        ${window.__isAdmin ? '<button class="btn btn-ghost btn-sm" onclick="refreshBuyerLeaderboard()">Refresh</button>' : ''}
    </div>`;

    html += `<div class="perf-summary">
        <div class="perf-card"><div class="perf-card-num">${totalOffers}</div><div class="perf-card-label">Offers Logged</div></div>
        <div class="perf-card"><div class="perf-card-num">${totalPts}</div><div class="perf-card-label">Total Points</div></div>
        <div class="perf-card"><div class="perf-card-num">${topScorer}</div><div class="perf-card-label">Top Scorer</div></div>
    </div>`;

    if (!entries.length) {
        html += '<p class="empty">No data for this month</p>';
        el.innerHTML = html;
        return;
    }

    const currentEmail = (window.__userEmail || '').toLowerCase();

    html += `<table class="perf-table"><thead><tr>
        <th>#</th><th>Buyer</th>
        <th>Offers (x1)</th><th>Quoted (x3)</th><th>Buy Plan (x5)</th><th>PO Confirmed (x8)</th><th>Stock Lists (x2)</th>
        <th>Total</th>
    </tr></thead><tbody>`;

    for (const e of entries) {
        const isMe = e.user_name && currentEmail && entries.some(x => x.user_id === e.user_id);
        const rowCls = isMe ? 'class="lb-highlight"' : '';
        const medal = e.rank === 1 ? ' 🥇' : e.rank === 2 ? ' 🥈' : e.rank === 3 ? ' 🥉' : '';
        html += `<tr ${rowCls}>
            <td><strong>${e.rank}${medal}</strong></td>
            <td>${e.user_name || 'Unknown'}</td>
            <td>${e.offers_logged} <span class="pts">(${e.points_offers})</span></td>
            <td>${e.offers_quoted} <span class="pts">(${e.points_quoted})</span></td>
            <td>${e.offers_in_buyplan} <span class="pts">(${e.points_buyplan})</span></td>
            <td>${e.offers_po_confirmed} <span class="pts">(${e.points_po})</span></td>
            <td>${e.stock_lists_uploaded || 0} <span class="pts">(${e.points_stock || 0})</span></td>
            <td><strong>${e.total_points}</strong></td>
        </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
}

async function refreshBuyerLeaderboard() {
    try {
        await apiFetch('/api/performance/buyers/refresh', {method:'POST'});
        loadBuyerLeaderboard(_leaderboardMonth);
    } catch (e) {
        alert('Error refreshing: ' + (e.message || e));
    }
}

// ── Settings (Admin) ─────────────────────────────────────────────────

function toggleSettingsDropdown() {
    const dd = document.getElementById('settingsDropdownContent');
    dd.style.display = dd.style.display === 'block' ? 'none' : 'block';
}

// Close dropdown when clicking outside
document.addEventListener('click', function(e) {
    const menu = document.getElementById('settingsMenu');
    if (menu && !menu.contains(e.target)) {
        const dd = document.getElementById('settingsDropdownContent');
        if (dd) dd.style.display = 'none';
    }
});

function openSettingsTab(panel) {
    document.getElementById('settingsDropdownContent').style.display = 'none';
    showView('view-settings');
    document.querySelectorAll('.topbar-nav button').forEach(b => b.classList.remove('active'));
    switchSettingsTab(panel);
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
}

// Keep backward compat for dropdown links
function showSettings(panel) { openSettingsTab(panel); }

async function loadSettingsSources() {
    const el = document.getElementById('settingsSourcesList');
    el.innerHTML = '<p class="empty">Loading data sources...</p>';
    try {
        const res = await apiFetch('/api/sources');
        const sources = (res.sources || []).filter(s => s.source_type !== 'internal');
        if (!sources.length) { el.innerHTML = '<p class="empty">No data sources configured</p>'; return; }

        // Sort: live first, then pending, error, disabled
        const order = {live: 0, pending: 1, error: 2, disabled: 3};
        sources.sort((a, b) => (order[a.status] || 9) - (order[b.status] || 9));

        let html = '';
        for (const s of sources) {
            const dot = s.status === 'live' ? '🟢' : s.status === 'pending' ? '🟡' : s.status === 'error' ? '🔴' : '⚫';
            const envVars = s.env_vars || [];
            const envStatus = s.env_status || {};

            let credsHtml = '';
            for (const v of envVars) {
                const isSet = envStatus[v];
                const badge = isSet
                    ? '<span style="color:var(--teal);font-size:11px;font-weight:600">Set</span>'
                    : '<span style="color:var(--muted);font-size:11px">Not set</span>';
                credsHtml += `
                    <div class="cred-row" id="cred-row-${s.id}-${v}" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
                        <code style="font-size:11px;min-width:180px;color:var(--text2)">${v}</code>
                        <span id="cred-status-${s.id}-${v}">${badge}</span>
                        <div style="flex:1"></div>
                        <button class="btn-sm" onclick="editCredential(${s.id},'${v}')" style="font-size:11px;padding:2px 10px">Edit</button>
                    </div>
                    <div id="cred-edit-${s.id}-${v}" style="display:none;padding:6px 0 10px">
                        <div style="display:flex;gap:8px;align-items:center">
                            <input type="password" id="cred-input-${s.id}-${v}" placeholder="Enter value..."
                                   style="flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--bg);color:var(--text)">
                            <button class="btn-sm" onclick="saveCredential(${s.id},'${v}')"
                                    style="font-size:11px;padding:4px 12px;background:var(--teal);color:#fff;border:none;border-radius:4px">Save</button>
                            <button class="btn-sm" onclick="cancelCredEdit(${s.id},'${v}')"
                                    style="font-size:11px;padding:4px 10px">Cancel</button>
                        </div>
                    </div>`;
            }

            const statsHtml = s.total_searches
                ? `<div style="font-size:10px;color:var(--muted);margin-top:8px">${s.total_searches} searches / ${s.total_results} results / ${s.avg_response_ms}ms avg</div>`
                : '';
            const errorHtml = s.last_error
                ? `<div style="font-size:10px;color:#e74c3c;margin-top:4px">Last error: ${s.last_error}</div>`
                : '';

            html += `<div class="card" style="padding:16px;margin-bottom:12px">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <div>
                        <strong style="font-size:14px">${s.display_name}</strong>
                        <span style="font-size:11px;color:var(--muted);margin-left:8px">${s.source_type}</span>
                    </div>
                    <div style="display:flex;align-items:center;gap:10px">
                        <span style="font-size:11px">${dot} ${s.status}</span>
                        <button class="btn-sm" id="test-btn-${s.id}" onclick="testSourceCred(${s.id})"
                                style="font-size:11px;padding:3px 12px">Test</button>
                    </div>
                </div>
                <div style="font-size:11px;color:var(--text2);margin-bottom:10px">${s.description || ''}</div>
                ${s.setup_notes ? '<div style="font-size:10px;color:var(--muted);margin-bottom:8px;padding:6px 10px;background:var(--bg);border-radius:4px">' + s.setup_notes + '</div>' : ''}
                ${s.signup_url ? '<a href="' + s.signup_url + '" target="_blank" style="font-size:10px;color:var(--teal);text-decoration:none">Get API credentials ↗</a>' : ''}
                <div style="margin-top:10px">${credsHtml}</div>
                <div id="test-result-${s.id}"></div>
                ${statsHtml}${errorHtml}
            </div>`;
        }
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<p class="empty">Failed to load sources</p>';
    }
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
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        showToast('Credential saved', 'success');
        cancelCredEdit(sourceId, varName);
        loadSettingsSources();
    } catch (e) {
        showToast('Failed to save credential: ' + (e.message || e), 'error');
    }
}

async function testSourceCred(sourceId) {
    const btn = document.getElementById(`test-btn-${sourceId}`);
    const resultEl = document.getElementById(`test-result-${sourceId}`);
    btn.disabled = true;
    btn.textContent = 'Testing...';
    resultEl.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:6px 0">Running test...</div>';
    try {
        const data = await apiFetch(`/api/sources/${sourceId}/test`, {method: 'POST'});
        if (data.status === 'ok') {
            resultEl.innerHTML = `<div style="font-size:11px;color:var(--teal);padding:6px 0">Test passed — ${data.results_count} results in ${data.elapsed_ms}ms</div>`;
        } else if (data.status === 'no_results') {
            resultEl.innerHTML = '<div style="font-size:11px;color:#e67e22;padding:6px 0">Connected but no results for test MPN</div>';
        } else {
            resultEl.innerHTML = `<div style="font-size:11px;color:#e74c3c;padding:6px 0">Test failed: ${data.error || 'Unknown error'}</div>`;
        }
        loadSettingsSources();
    } catch (e) {
        resultEl.innerHTML = `<div style="font-size:11px;color:#e74c3c;padding:6px 0">Test error: ${e.message || e}</div>`;
    }
    btn.disabled = false;
    btn.textContent = 'Test';
}

// ── System Health ──

async function loadSettingsHealth() {
    const el = document.getElementById('settingsHealthContent');
    el.innerHTML = '<p class="empty">Loading...</p>';
    try {
        const data = await apiFetch('/api/admin/health');
        let html = '';

        // Version
        html += `<div class="card" style="padding:16px;margin-bottom:16px"><strong>Version:</strong> ${data.version}</div>`;

        // DB stats
        html += '<div class="card" style="padding:16px;margin-bottom:16px"><h3 style="margin:0 0 12px;font-size:14px">Database Statistics</h3>';
        html += '<table class="perf-table"><thead><tr><th>Table</th><th>Rows</th></tr></thead><tbody>';
        for (const [k, v] of Object.entries(data.db_stats || {})) {
            html += `<tr><td>${k}</td><td>${v.toLocaleString()}</td></tr>`;
        }
        html += '</tbody></table></div>';

        // Scheduler status
        html += '<div class="card" style="padding:16px;margin-bottom:16px"><h3 style="margin:0 0 12px;font-size:14px">M365 Scheduler Status</h3>';
        html += '<table class="perf-table"><thead><tr><th>User</th><th>M365</th><th>Token</th><th>Last Inbox Scan</th></tr></thead><tbody>';
        for (const u of data.scheduler || []) {
            const dot = u.m365_connected ? '<span style="color:var(--teal)">Connected</span>' : '<span style="color:var(--muted)">Disconnected</span>';
            const scan = u.last_inbox_scan ? new Date(u.last_inbox_scan).toLocaleString() : '—';
            html += `<tr><td>${u.email}</td><td>${dot}</td><td>${u.has_refresh_token ? 'Yes' : 'No'}</td><td>${scan}</td></tr>`;
        }
        html += '</tbody></table></div>';

        // Connector health
        html += '<div class="card" style="padding:16px"><h3 style="margin:0 0 12px;font-size:14px">Connector Health</h3>';
        html += '<table class="perf-table"><thead><tr><th>Name</th><th>Status</th><th>Searches</th><th>Results</th><th>Last Success</th></tr></thead><tbody>';
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
        let html = '<div class="card" style="padding:20px;max-width:600px">';
        html += '<h3 style="margin:0 0 16px;font-size:14px">Search Scoring Weights</h3>';
        html += '<p style="font-size:12px;color:var(--muted);margin:0 0 16px">Weights determine how search results are ranked. Total should equal 100.</p>';
        html += '<div style="display:flex;flex-direction:column;gap:10px">';
        for (const w of weights) {
            const label = w.key.replace('weight_', '').replace(/_/g, ' ');
            html += `<div style="display:flex;align-items:center;gap:10px">
                <label style="flex:1;font-size:13px;text-transform:capitalize">${label}</label>
                <input type="number" min="0" max="100" value="${w.value}" id="sw_${w.key}"
                    onchange="updateWeightTotal()"
                    style="width:60px;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface);color:var(--text);text-align:center">
                <button class="btn btn-ghost btn-sm" onclick="saveConfig('${w.key}', document.getElementById('sw_${w.key}').value)">Save</button>
            </div>`;
        }
        html += '</div>';
        html += '<div style="margin-top:16px;font-size:13px"><strong>Total: <span id="weightTotal">0</span></strong> <span id="weightWarn" style="color:#e74c3c;display:none">(should be 100)</span></div>';
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
        let html = '<div class="card" style="padding:20px;max-width:600px">';
        html += '<h3 style="margin:0 0 16px;font-size:14px">System Configuration</h3>';
        html += '<div style="display:flex;flex-direction:column;gap:12px">';
        for (const c of nonWeights) {
            const isBool = c.value === 'true' || c.value === 'false';
            if (isBool) {
                const checked = c.value === 'true' ? 'checked' : '';
                html += `<div style="display:flex;align-items:center;gap:10px">
                    <label style="flex:1;font-size:13px">${c.key.replace(/_/g, ' ')}<br><span style="font-size:11px;color:var(--muted)">${c.description || ''}</span></label>
                    <label style="display:flex;align-items:center;gap:4px;cursor:pointer">
                        <input type="checkbox" ${checked} onchange="saveConfig('${c.key}', this.checked ? 'true' : 'false')">
                        <span style="font-size:12px">${c.value === 'true' ? 'On' : 'Off'}</span>
                    </label>
                </div>`;
            } else {
                html += `<div style="display:flex;align-items:center;gap:10px">
                    <label style="flex:1;font-size:13px">${c.key.replace(/_/g, ' ')}<br><span style="font-size:11px;color:var(--muted)">${c.description || ''}</span></label>
                    <input type="text" value="${c.value}" id="cfg_${c.key}"
                        style="width:80px;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface);color:var(--text);text-align:center">
                    <button class="btn btn-ghost btn-sm" onclick="saveConfig('${c.key}', document.getElementById('cfg_${c.key}').value)">Save</button>
                </div>`;
            }
        }
        html += '</div>';
        if (nonWeights.length) {
            const lastUpdate = nonWeights.find(c => c.updated_by);
            if (lastUpdate) html += `<p style="font-size:11px;color:var(--muted);margin-top:12px">Last updated by ${lastUpdate.updated_by}</p>`;
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
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({value: String(value)})
        });
        // Reload the panel that owns this key
        if (key.startsWith('weight_')) loadSettingsScoring();
        else loadSettingsConfig();
    } catch (e) {
        alert('Error saving: ' + (e.message || e));
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
    let html = `<table class="perf-table"><thead><tr>
        <th>Name</th><th>Email</th><th>Role</th><th>Active</th><th>M365</th><th>Actions</th>
    </tr></thead><tbody>`;
    for (const u of _adminUsers) {
        const activeChecked = u.is_active !== false ? 'checked' : '';
        html += `<tr>
            <td>${u.name || '—'}</td>
            <td>${u.email}</td>
            <td><select onchange="updateUserField(${u.id}, 'role', this.value)" style="padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface);color:var(--text)">
                <option value="buyer" ${u.role==='buyer'?'selected':''}>Buyer</option>
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
        await apiFetch(`/api/admin/users/${userId}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    } catch (e) {
        alert('Error: ' + (e.message || e));
        loadAdminUsers();
    }
}

async function deleteAdminUser(userId, name) {
    if (!confirm(`Delete user "${name}"? This cannot be undone.`)) return;
    try {
        await apiFetch(`/api/admin/users/${userId}`, {method:'DELETE'});
        loadAdminUsers();
    } catch (e) {
        alert('Error: ' + (e.message || e));
    }
}

async function createUser() {
    const name = document.getElementById('newUserName').value.trim();
    const email = document.getElementById('newUserEmail').value.trim();
    const role = document.getElementById('newUserRole').value;
    if (!name || !email) { alert('Name and email are required'); return; }
    try {
        await apiFetch('/api/admin/users', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, email, role})});
        document.getElementById('newUserName').value = '';
        document.getElementById('newUserEmail').value = '';
        alert('User created successfully');
    } catch (e) {
        alert('Error: ' + (e.message || e));
    }
}

async function importCustomers() {
    const fileInput = document.getElementById('customerImportFile');
    if (!fileInput.files.length) { alert('Select a CSV file first'); return; }
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    const statusEl = document.getElementById('customerImportStatus');
    statusEl.textContent = 'Importing...';
    try {
        const res = await fetch('/api/admin/import/customers', {method:'POST', body:form});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Import failed');
        statusEl.textContent = `Done: ${data.companies_created} companies, ${data.sites_created} sites, ${data.contacts_created} contacts created from ${data.rows_processed} rows`;
        fileInput.value = '';
    } catch (e) {
        statusEl.textContent = 'Error: ' + (e.message || e);
    }
}

async function importVendors() {
    const fileInput = document.getElementById('vendorImportFile');
    if (!fileInput.files.length) { alert('Select a CSV file first'); return; }
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    const statusEl = document.getElementById('vendorImportStatus');
    statusEl.textContent = 'Importing...';
    try {
        const res = await fetch('/api/admin/import/vendors', {method:'POST', body:form});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Import failed');
        statusEl.textContent = `Done: ${data.vendors_created} vendors, ${data.contacts_created} contacts created from ${data.rows_processed} rows`;
        fileInput.value = '';
    } catch (e) {
        statusEl.textContent = 'Error: ' + (e.message || e);
    }
}
