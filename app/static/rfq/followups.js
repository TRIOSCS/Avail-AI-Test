/**
 * rfq/followups.js
 *
 * Purpose:
 * Encapsulates RFQ follow-up panel loading and follow-up send actions so the
 * main frontend bundle does not keep RFQ follow-up behavior inline.
 *
 * Business rules enforced:
 * - Follow-ups are hidden in archive view.
 * - Bulk follow-up sends must refresh the same follow-up panel they came from.
 * - Follow-up UI keeps existing inline handler names and DOM contracts.
 *
 * Called by:
 * - app/static/app.js
 * - inline RFQ follow-up buttons rendered into index.html/app.js templates
 *
 * Depends on:
 * - app/static/app.js to inject shared helpers via configureRfqFollowups()
 * - DOM elements #followUpsPanel and #bulkFollowUpBtn
 * - RFQ follow-up API endpoints under /api/follow-ups
 */

let _followUpsAbort = null;

let _deps = {
    apiFetch: null,
    confirmAction: null,
    showToast: null,
    esc: null,
    escAttr: null,
    guardBtn: null,
    getCurrentMainView: () => 'sales',
    refreshActivity: null,
};

export function configureRfqFollowups(deps) {
    _deps = { ..._deps, ...(deps || {}) };
}

export function cancelFollowUpsRequests() {
    if (_followUpsAbort) {
        try { _followUpsAbort.abort(); } catch (e) {}
        _followUpsAbort = null;
    }
}

export async function sendFollowUp(contactId, vendorName) {
    _deps.confirmAction('Send Follow-Up', 'Send follow-up email to ' + vendorName + '?', async function() {
        if (sendFollowUp._busy) return;
        sendFollowUp._busy = true;
        try {
            const data = await _deps.apiFetch(`/api/follow-ups/${contactId}/send`, { method: 'POST', body: {} });
            _deps.showToast(data.message || `Follow-up sent to ${vendorName}`, 'success');
            if (typeof _deps.refreshActivity === 'function') _deps.refreshActivity();
            await loadFollowUpsPanel();
        } catch (e) {
            _deps.showToast('Failed to send follow-up', 'error');
        } finally {
            sendFollowUp._busy = false;
        }
    });
}

export async function loadFollowUpsPanel() {
    const panel = document.getElementById('followUpsPanel');
    if (!panel) return;
    if (_deps.getCurrentMainView() === 'archive') {
        panel.style.display = 'none';
        return;
    }

    cancelFollowUpsRequests();
    _followUpsAbort = new AbortController();

    try {
        const data = await _deps.apiFetch('/api/follow-ups', { signal: _followUpsAbort.signal });
        if (_deps.getCurrentMainView() === 'archive') return;

        const followUps = data.follow_ups || [];
        if (!followUps.length) {
            panel.style.display = 'none';
            panel.innerHTML = '';
            return;
        }

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

        for (const g of Object.values(groups)) {
            html += `<div style="margin-bottom:6px"><span style="font-weight:600;font-size:12px">${_deps.esc(g.name)}</span></div>`;
            for (const fu of g.items) {
                const dayColor = fu.days_waiting > 5 ? 'var(--red)' : fu.days_waiting > 2 ? 'var(--amber)' : 'var(--green)';
                html += `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px">
                    ${fu.contact_id ? `<input type="checkbox" class="fu-cb" data-contact-id="${fu.contact_id}" onchange="_updateBulkFollowUpBtn()">` : ''}
                    <span style="color:var(--text2)">${_deps.esc(fu.vendor_name)}</span>
                    <span style="color:var(--muted)">${_deps.esc(fu.vendor_email || '')}</span>
                    <span style="color:${dayColor};font-weight:600">${fu.days_waiting}d</span>
                    ${fu.parts && fu.parts.length ? `<span style="color:var(--muted)">${_deps.esc(Array.isArray(fu.parts) ? fu.parts.join(', ') : String(fu.parts))}</span>` : ''}
                    ${fu.contact_id ? `<button class="btn btn-ghost btn-sm" onclick="sendFollowUp(${fu.contact_id},'${_deps.escAttr(fu.vendor_name)}')" style="padding:1px 6px;font-size:10px">Send Now</button>` : ''}
                </div>`;
            }
        }

        html += '</div>';
        panel.innerHTML = html;
        panel.style.display = '';
    } catch (e) {
        if (e.name !== 'AbortError') {
            panel.style.display = 'none';
        }
    }
}

export function _updateBulkFollowUpBtn() {
    const checked = document.querySelectorAll('.fu-cb:checked').length;
    const btn = document.getElementById('bulkFollowUpBtn');
    if (btn) {
        btn.style.display = checked > 0 ? '' : 'none';
        btn.textContent = `Send ${checked} Follow-up${checked > 1 ? 's' : ''}`;
    }
}

export async function sendBulkFollowUp() {
    const checked = [...document.querySelectorAll('.fu-cb:checked')];
    const contactIds = checked.map(cb => parseInt(cb.dataset.contactId)).filter(Boolean);
    if (!contactIds.length) return;

    const btn = document.getElementById('bulkFollowUpBtn');
    await _deps.guardBtn(btn, 'Sending…', async () => {
        const data = await _deps.apiFetch('/api/follow-ups/send-batch', {
            method: 'POST',
            body: { contact_ids: contactIds },
        });
        _deps.showToast(`Sent ${data.sent} of ${data.total} follow-ups`, data.sent > 0 ? 'success' : 'error');
        await loadFollowUpsPanel();
    });
}
