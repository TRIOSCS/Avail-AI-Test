/* AvailAI Frontend v0.3 */

let results = [], selected = new Set(), curFilter = null;

// --- API helper ---
async function api(url, opts = {}) {
    const r = await fetch(url, { headers: {'Content-Type':'application/json', ...opts.headers}, ...opts });
    if (r.status === 401) { location.href = '/auth/login'; return null; }
    if (!r.ok) { const e = await r.json().catch(() => ({detail:'Error'})); throw new Error(e.detail || `HTTP ${r.status}`); }
    return r.json();
}

// --- Tabs ---
function switchTab(name, btn) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
    document.querySelectorAll('.tc').forEach(c => c.classList.remove('on'));
    btn.classList.add('on');
    document.getElementById(`tab-${name}`).classList.add('on');
    if (name === 'upload') loadLB();
    if (name === 'responses') { loadStats(); loadResp(); }
}

// ═══════════════════════════════════════════════════════════════════════
// SEARCH
// ═══════════════════════════════════════════════════════════════════════

async function doSearch() {
    const raw = document.getElementById('partInput').value.trim();
    if (!raw) return;
    const pns = raw.split(/[,\n]+/).map(s => s.trim()).filter(Boolean);
    const tgt = parseInt(document.getElementById('targetQty').value) || null;
    const btn = document.getElementById('searchBtn');
    btn.disabled = true; btn.textContent = 'Searching…';
    document.getElementById('results').innerHTML = '<p class="empty">Searching all sources…</p>';
    document.getElementById('rmeta').textContent = '';
    try {
        const data = await api('/api/search', { method:'POST', body:JSON.stringify({part_numbers:pns, include_historical:true, target_qty:tgt}) });
        results = data.results; selected.clear(); render(data);
    } catch(e) { document.getElementById('results').innerHTML = `<p class="empty" style="color:var(--red)">Error: ${e.message}</p>`; }
    finally { btn.disabled = false; btn.textContent = 'Search'; }
}

function sc(s) { return s >= 60 ? 'hi' : s >= 35 ? 'mid' : 'lo'; }
function bc(v) { return v >= 70 ? 'var(--green)' : v >= 40 ? 'var(--amber)' : 'var(--red)'; }

function render(data) {
    const el = document.getElementById('results');
    if (!results.length) { el.innerHTML = '<p class="empty">No results found</p>'; return; }
    document.getElementById('rmeta').textContent = `${data.result_count} vendor${data.result_count!==1?'s':''} found`;

    el.innerHTML = results.map((r,i) => {
        const s = r.score_breakdown, c = s.components || {};
        const chk = r.excluded ? 'disabled' : (selected.has(r.vendor_id) ? 'checked' : '');
        let badges = r.vendor_type==='distributor' ? '<span class="badge b-dist">Distributor</span>' : '<span class="badge b-broker">Broker</span>';
        if (r.vendor_is_authorized) badges += '<span class="badge b-auth">Authorized</span>';

        let dets = `<span class="di"><span class="dl">PN</span><span class="dv">${r.part_number}</span></span>`;
        if (r.quantity) dets += `<span class="di"><span class="dl">Qty</span><span class="dv">${r.quantity.toLocaleString()}</span></span>`;
        if (r.price) dets += `<span class="di"><span class="dl">Price</span><span class="dv">$${r.price.toFixed(4)}</span></span>`;
        if (r.lead_time_days!=null) dets += `<span class="di"><span class="dl">Lead</span><span class="dv">${r.lead_time_days}d</span></span>`;
        if (r.condition) dets += `<span class="di"><span class="dl">Cond</span><span class="dv">${r.condition}</span></span>`;
        if (r.manufacturer) dets += `<span class="di"><span class="dl">Mfr</span><span class="dv">${r.manufacturer}</span></span>`;

        const srcs = (r.sources_found_on||[r.source]).join(', ');
        const seen = r.seen_at ? new Date(r.seen_at).toLocaleDateString() : '—';

        const bars = [['Recency',c.recency||0],['Quantity',c.quantity||0],['Vendor',c.vendor_reliability||0],
            ['Complete',c.data_completeness||0],['Source',c.source_credibility||0],['Price',c.price||0]]
            .map(([l,v]) => `<div class="brow"><span class="blbl">${l}</span><div class="btrack"><div class="bfill" style="width:${v}%;background:${bc(v)}"></div></div><span class="bval">${Math.round(v)}</span></div>`).join('');

        const pen = s.penalty_multiplier < 1 ? `<div class="brow"><span class="blbl" style="color:var(--red)">Penalty</span><span style="color:var(--red);font-size:11px">×${s.penalty_multiplier} — ${(s.penalty_reasons||[]).join(', ')}</span></div>` : '';

        return `<div class="rc ${r.excluded?'excl':''}">
            <input type="checkbox" ${chk} onchange="togV('${r.vendor_id}')">
            <div class="rb">
                <div class="rtop"><div><div class="vname">${r.vendor_name}</div><div>${badges}</div></div>
                    <div style="text-align:center"><div class="ring ${sc(r.score)}">${Math.round(r.score)}</div><div style="font-size:10px;color:var(--muted)">score</div></div></div>
                <div class="rdets">${dets}</div>
                <div class="rmeta2"><span>Source: ${srcs}</span><span>Seen: ${seen}</span><span>Conf: ${r.confidence}/5</span></div>
                ${r.excluded ? `<div class="excl-warn">⚠ ${r.exclusion_reason}</div>` : ''}
                <button class="bdbtn" onclick="document.getElementById('bd-${i}').classList.toggle('open')">Score breakdown ▾</button>
                <div id="bd-${i}" class="bd">${bars}${pen}</div>
            </div></div>`;
    }).join('');
    updSend();
}

function togV(id) { selected.has(id) ? selected.delete(id) : selected.add(id); updSend(); }
function selAll() { results.forEach(r => { if(!r.excluded) selected.add(r.vendor_id); }); render({result_count:results.length}); }
function selNone() { selected.clear(); render({result_count:results.length}); }
function updSend() { const b=document.getElementById('sendBtn'); b.textContent=`Send RFQ (${selected.size})`; b.disabled=!selected.size; }

// ═══════════════════════════════════════════════════════════════════════
// RFQ MODAL
// ═══════════════════════════════════════════════════════════════════════

async function openRfq() {
    if (!selected.size) return;
    const pns = [...new Set(results.map(r => r.part_number))];
    try {
        const d = await api('/api/outreach/preview', { method:'POST', body:JSON.stringify({vendor_ids:[...selected], part_numbers:pns}) });
        document.getElementById('rfqSubject').value = d.draft_subject;
        document.getElementById('rfqBody').value = d.draft_body;
        document.getElementById('rfqVendors').innerHTML = d.vendors.map(v =>
            `<div class="mvr ${v.excluded?'excl':''}">${v.name} — ${v.email||'no email'}${v.excluded?' ⚠ '+v.exclusion_reason:''}</div>`
        ).join('');
        document.getElementById('rfqModal').classList.add('open');
    } catch(e) { alert('Error: '+e.message); }
}
function closeRfq() { document.getElementById('rfqModal').classList.remove('open'); }

async function sendRfq() {
    const btn = document.getElementById('confirmBtn');
    btn.disabled = true; btn.textContent = 'Sending…';
    try {
        const pns = [...new Set(results.map(r => r.part_number))];
        const d = await api('/api/outreach/send', { method:'POST', body:JSON.stringify({
            vendor_ids:[...selected], part_numbers:pns,
            subject:document.getElementById('rfqSubject').value,
            body:document.getElementById('rfqBody').value,
        }) });
        alert(`✓ Sent ${d.sent_count} email${d.sent_count!==1?'s':''}`);
        closeRfq(); selected.clear(); doSearch();
    } catch(e) { alert('Send failed: '+e.message); }
    finally { btn.disabled = false; btn.textContent = 'Send'; }
}

// ═══════════════════════════════════════════════════════════════════════
// UPLOAD
// ═══════════════════════════════════════════════════════════════════════

async function doUpload() {
    const inp = document.getElementById('fileInput');
    if (!inp.files.length) return;
    const file = inp.files[0], form = new FormData();
    form.append('file', file);
    const st = document.getElementById('uploadStatus');
    st.className = 'ustatus load'; st.textContent = `Uploading ${file.name}…`;
    try {
        const r = await fetch('/api/uploads', { method:'POST', body:form });
        if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || 'Upload failed');
        const d = await r.json();
        if (d.status === 'complete') {
            st.className = 'ustatus ok';
            st.textContent = `✓ ${d.sighting_count.toLocaleString()} parts from ${d.row_count.toLocaleString()} rows` + (d.error_count?` (${d.error_count} errors)`:'');
        } else { st.className = 'ustatus err'; st.textContent = `Failed: ${d.error_message||'Unknown'}`; }
        inp.value = ''; loadLB();
    } catch(e) { st.className = 'ustatus err'; st.textContent = `Error: ${e.message}`; }
}

// Drag & drop
document.addEventListener('DOMContentLoaded', () => {
    const z = document.getElementById('dropZone');
    if (!z) return;
    z.addEventListener('dragover', e => { e.preventDefault(); z.classList.add('drag'); });
    z.addEventListener('dragleave', () => z.classList.remove('drag'));
    z.addEventListener('drop', e => { e.preventDefault(); z.classList.remove('drag');
        document.getElementById('fileInput').files = e.dataTransfer.files; doUpload(); });
});

async function loadLB() {
    try {
        const d = await api('/api/stats/uploads');
        const tb = document.getElementById('leaderboard');
        if (!d.users.length) { tb.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted)">No uploads yet</td></tr>'; return; }
        tb.innerHTML = d.users.map((u,i) => `<tr><td><span class="lbr">${i+1}</span></td><td>${u.display_name}</td><td>${u.upload_count}</td><td>${u.sighting_count.toLocaleString()}</td></tr>`).join('');
    } catch(e) {}
}

// ═══════════════════════════════════════════════════════════════════════
// RESPONSES
// ═══════════════════════════════════════════════════════════════════════

async function loadStats() {
    try {
        const d = await api('/api/monitor/stats');
        document.getElementById('stSent').textContent = d.outreach.total_sent;
        document.getElementById('stRepl').textContent = d.outreach.total_responded;
        document.getElementById('stRate').textContent = `${d.outreach.response_rate}% rate`;
        document.getElementById('stPos').textContent = d.outreach.total_positive;
        document.getElementById('stHrs').textContent = d.outreach.avg_response_hours || '—';
        document.getElementById('stParsed').textContent = d.parsing.total_parsed;
        document.getElementById('stConf').textContent = d.parsing.avg_confidence ? `${Math.round(d.parsing.avg_confidence*100)}% avg` : '';
        document.getElementById('stPend').textContent = d.parsing.pending_review;
    } catch(e) {}
}

async function loadResp(status) {
    const el = document.getElementById('respList');
    try {
        const d = await api(status ? `/api/monitor/responses?status=${status}` : '/api/monitor/responses');
        if (!d.responses.length) { el.innerHTML = '<p class="empty">No responses found</p>'; return; }
        el.innerHTML = d.responses.map(renderVR).join('');
    } catch(e) { el.innerHTML = `<p class="empty" style="color:var(--red)">Error: ${e.message}</p>`; }
}

function filterResp(s, btn) {
    curFilter = s;
    document.querySelectorAll('.ft').forEach(t => t.classList.remove('on'));
    btn.classList.add('on');
    loadResp(s);
}

function renderVR(r) {
    const cp = Math.round((r.confidence||0)*100);
    const cc = cp >= 80 ? 'var(--green)' : cp >= 50 ? 'var(--amber)' : 'var(--red)';
    const when = r.received_at ? new Date(r.received_at).toLocaleString() : '—';

    let flds = '';
    if (r.has_stock!=null) flds += `<div class="qf"><div class="qfl">Stock</div><div class="qfv" style="color:${r.has_stock?'var(--green)':'var(--red)'}">${r.has_stock?'✓ Yes':'✗ No'}</div></div>`;
    if (r.quoted_price) flds += `<div class="qf"><div class="qfl">Price</div><div class="qfv">$${r.quoted_price.toFixed(4)}</div></div>`;
    if (r.quoted_quantity) flds += `<div class="qf"><div class="qfl">Qty</div><div class="qfv">${r.quoted_quantity.toLocaleString()}</div></div>`;
    if (r.quoted_lead_time_text||r.quoted_lead_time_days) flds += `<div class="qf"><div class="qfl">Lead Time</div><div class="qfv">${r.quoted_lead_time_text||r.quoted_lead_time_days+'d'}</div></div>`;
    if (r.quoted_condition) flds += `<div class="qf"><div class="qfl">Condition</div><div class="qfv">${r.quoted_condition}</div></div>`;
    if (r.quoted_date_code) flds += `<div class="qf"><div class="qfl">Date Code</div><div class="qfv">${r.quoted_date_code}</div></div>`;
    if (r.quoted_manufacturer) flds += `<div class="qf"><div class="qfl">Mfr</div><div class="qfv">${r.quoted_manufacturer}</div></div>`;
    if (r.quoted_moq) flds += `<div class="qf"><div class="qfl">MOQ</div><div class="qfv">${r.quoted_moq.toLocaleString()}</div></div>`;

    let acts = '';
    if (r.status === 'parsed') acts = `<div class="vra"><button class="btn-a" onclick="approveVR('${r.id}')">✓ Approve</button><button class="btn-r" onclick="rejectVR('${r.id}')">✗ Reject</button></div>`;

    return `<div class="vrc">
        <div class="vrh"><div><span class="vrv">${r.vendor_name}</span> <span class="vrp">${r.part_number||'—'}</span>
            <span class="sbdg s-${r.status}">${r.status.replace('_',' ')}</span></div>
            <div style="text-align:right"><div class="vrt">${when}</div>
            <div class="conf"><div class="cbar"><div class="cfill" style="width:${cp}%;background:${cc}"></div></div>${cp}%</div></div></div>
        <div class="vrq">${flds}</div>
        ${r.parse_notes?`<div style="font-size:12px;color:var(--muted);margin-bottom:6px">AI: ${r.parse_notes}</div>`:''}
        ${r.email_preview?`<div class="vrep" onclick="this.classList.toggle('exp')">${r.email_preview}</div>`:''}
        ${acts}</div>`;
}

async function doPoll() {
    const btn = document.getElementById('pollBtn');
    btn.disabled = true; btn.textContent = '⟳ Checking…';
    try {
        const d = await api('/api/monitor/poll', {method:'POST'});
        const msg = `Found ${d.new_replies} repl${d.new_replies!==1?'ies':'y'}, ${d.parsed_quotes} quotes, ${d.sightings_created} sightings`;
        const el = document.getElementById('respList');
        const t = document.createElement('div');
        t.style.cssText = 'padding:12px;margin-bottom:12px;border-radius:8px;font-size:13px;background:rgba(59,130,246,.1);color:var(--blue)';
        t.textContent = `✓ ${msg}`; el.prepend(t); setTimeout(() => t.remove(), 5000);
        loadStats(); loadResp(curFilter);
    } catch(e) { alert('Poll failed: '+e.message); }
    finally { btn.disabled = false; btn.textContent = '⟳ Check Inbox'; }
}

async function approveVR(id) {
    try { await api(`/api/monitor/responses/${id}/approve`, {method:'POST'}); loadResp(curFilter); loadStats(); }
    catch(e) { alert('Error: '+e.message); }
}
async function rejectVR(id) {
    try { await api(`/api/monitor/responses/${id}/reject`, {method:'POST'}); loadResp(curFilter); loadStats(); }
    catch(e) { alert('Error: '+e.message); }
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    const inp = document.getElementById('partInput');
    if (inp) inp.addEventListener('keydown', e => { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();doSearch();} });
    loadLB();
});
