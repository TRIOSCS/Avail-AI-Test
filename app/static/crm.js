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

async function loadOffers() {
    if (!currentReqId) return;
    try {
        const res = await fetch('/api/requisitions/' + currentReqId + '/offers');
        if (!res.ok) return;
        crmOffers = await res.json();
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
            const details = [o.firmware && 'FW: '+esc(o.firmware), o.hardware_code && 'HW: '+esc(o.hardware_code), o.packaging && 'Pkg: '+esc(o.packaging)].filter(Boolean).join(' · ');
            return `
            <tr class="${isRef ? 'offer-ref' : ''}">
                <td><input type="checkbox" ${checked} ${isRef ? 'disabled' : ''} onchange="toggleOfferSelect(${o.id},this.checked)"></td>
                <td>${esc(o.vendor_name)}${details ? '<div class="sc-detail" style="font-size:10px;color:var(--muted)">'+details+'</div>' : ''}</td>
                <td>${o.unit_price != null ? '$'+Number(o.unit_price).toFixed(4) : '—'}</td>
                <td>${o.qty_available != null ? o.qty_available.toLocaleString() : '—'}</td>
                <td>${esc(o.lead_time || '—')}</td>
                <td>${esc(o.condition || '—')}</td>
                <td>${esc(o.date_code || '—')}</td>
                <td>${isRef ? '<span class="offer-ref-badge">ref</span>' : '<button class="btn btn-danger btn-sm" onclick="deleteOffer('+o.id+')" title="Remove offer" style="padding:2px 6px;font-size:10px">✕</button>'}</td>
            </tr>`;
        }).join('') : '<tr><td colspan="8" class="empty" style="padding:8px">No offers for this part</td></tr>';
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
                <thead><tr><th style="width:30px"></th><th>Vendor</th><th>Price</th><th>Avail</th><th>Lead</th><th>Cond</th><th>DC</th><th style="width:40px"></th></tr></thead>
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
        source: document.querySelector('input[name="loSource"]:checked')?.value || 'manual',
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
        showToast('Offer from ' + data.vendor_name + ' saved', 'success');
        notifyStatusChange(result);
        if (andNext) {
            ['loVendor','loQty','loPrice','loLead','loDC','loFirmware','loHardware','loPackaging','loMoq','loNotes','loWebsite'].forEach(id => document.getElementById(id).value = '');
            document.getElementById('loCond').value = 'New';
            document.getElementById('loWebsiteRow').style.display = 'none';
            document.getElementById('loVendor').focus();
        } else {
            closeModal('logOfferModal');
        }
        loadOffers();
    } catch (e) { console.error('saveOffer:', e); showToast('Error saving offer', 'error'); }
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
    <div class="quote-actions">${statusActions[q.status] || ''}</div>`;
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
    if (result === 'won' && !confirm('Mark as Won? Revenue: $' + Number(crmQuote.subtotal||0).toLocaleString())) return;
    try {
        const res = await fetch('/api/quotes/' + crmQuote.id + '/result', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ result })
        });
        if (!res.ok) { showToast('Failed to update result', 'error'); return; }
        const resultData = await res.json();
        showToast(result === 'won' ? 'Quote marked as Won!' : 'Quote updated', result === 'won' ? 'success' : 'info');
        notifyStatusChange(resultData);
        loadQuote();
    } catch (e) { console.error('markQuoteResult:', e); showToast('Error updating result', 'error'); }
}

function openLostModal() {
    document.getElementById('lostModal').classList.add('open');
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
