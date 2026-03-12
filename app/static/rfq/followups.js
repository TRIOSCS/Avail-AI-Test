/*
 * rfq/followups.js — RFQ follow-up panel actions and rendering helpers.
 * Called by: app/static/app.js wrapper functions (sendFollowUp/loadFollowUpsPanel/sendBulkFollowUp).
 * Depends on: callbacks passed from app.js (apiFetch, showToast, esc, escAttr, guardBtn, loadActivity).
 */

export function sendFollowUpImpl(ctx, contactId, vendorName, busyRef) {
    ctx.confirmAction('Send Follow-Up', 'Send follow-up email to ' + vendorName + '?', async function() {
        if (busyRef._busy) return;
        busyRef._busy = true;
        try {
            const data = await ctx.apiFetch(`/api/follow-ups/${contactId}/send`, { method: 'POST', body: {} });
            ctx.showToast(data.message || `Follow-up sent to ${vendorName}`, 'success');
            if (ctx.loadActivityFn) ctx.loadActivityFn();
            await ctx.loadFollowUpsPanelFn();
        } catch (_) {
            ctx.showToast('Failed to send follow-up', 'error');
        } finally {
            busyRef._busy = false;
        }
    });
}

export async function loadFollowUpsPanelImpl(ctx) {
    const panel = document.getElementById('followUpsPanel');
    if (!panel) return;
    if (ctx.getCurrentMainView() === 'archive') {
        panel.style.display = 'none';
        return;
    }

    const prevAbort = ctx.getAbortController();
    if (prevAbort) {
        try {
            prevAbort.abort();
        } catch (_) {}
    }
    const controller = new AbortController();
    ctx.setAbortController(controller);

    try {
        const data = await ctx.apiFetch('/api/follow-ups', { signal: controller.signal });
        if (ctx.getCurrentMainView() === 'archive') return;

        const followUps = data.follow_ups || [];
        if (!followUps.length) {
            panel.style.display = 'none';
            return;
        }

        const groups = {};
        for (const fu of followUps) {
            const key = fu.requisition_id || 0;
            if (!groups[key]) {
                groups[key] = {
                    name: fu.requisition_name || 'Unknown Requirement',
                    items: [],
                };
            }
            groups[key].items.push(fu);
        }

        let html = `<div class="card" style="margin:0 16px 12px;padding:12px;border-left:3px solid var(--amber)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-weight:700;font-size:13px;color:var(--amber)">Awaiting Vendor Replies (${followUps.length})</span>
                <button class="btn btn-warning btn-sm" id="bulkFollowUpBtn" onclick="sendBulkFollowUp()" style="font-size:10px;display:none">Send Selected</button>
            </div>`;

        for (const g of Object.values(groups)) {
            html += `<div style="margin-bottom:6px"><span style="font-weight:600;font-size:12px">${ctx.esc(g.name)}</span></div>`;
            for (const fu of g.items) {
                const dayColor = fu.days_waiting > 5 ? 'var(--red)' : fu.days_waiting > 2 ? 'var(--amber)' : 'var(--green)';
                html += `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px">
                    ${fu.contact_id ? `<input type="checkbox" class="fu-cb" data-contact-id="${fu.contact_id}" onchange="_updateBulkFollowUpBtn()">` : ''}
                    <span style="color:var(--text2)">${ctx.esc(fu.vendor_name)}</span>
                    <span style="color:var(--muted)">${ctx.esc(fu.vendor_email || '')}</span>
                    <span style="color:${dayColor};font-weight:600">${fu.days_waiting}d</span>
                    ${fu.parts && fu.parts.length ? `<span style="color:var(--muted)">${ctx.esc(Array.isArray(fu.parts) ? fu.parts.join(', ') : String(fu.parts))}</span>` : ''}
                    ${fu.contact_id ? `<button class="btn btn-ghost btn-sm" onclick="sendFollowUp(${fu.contact_id},'${ctx.escAttr(fu.vendor_name)}')" style="padding:1px 6px;font-size:10px">Send Now</button>` : ''}
                </div>`;
            }
        }

        html += '</div>';
        panel.innerHTML = html;
        panel.style.display = '';
    } catch (e) {
        if (e.name !== 'AbortError') panel.style.display = 'none';
    }
}

export function updateBulkFollowUpBtnImpl() {
    const checked = document.querySelectorAll('.fu-cb:checked').length;
    const btn = document.getElementById('bulkFollowUpBtn');
    if (!btn) return;
    btn.style.display = checked > 0 ? '' : 'none';
    btn.textContent = `Send ${checked} Follow-up${checked > 1 ? 's' : ''}`;
}

export async function sendBulkFollowUpImpl(ctx) {
    const checked = [...document.querySelectorAll('.fu-cb:checked')];
    const contactIds = checked.map(cb => parseInt(cb.dataset.contactId, 10)).filter(Boolean);
    if (!contactIds.length) return;
    const btn = document.getElementById('bulkFollowUpBtn');
    await ctx.guardBtn(btn, 'Sending…', async () => {
        const data = await ctx.apiFetch('/api/follow-ups/send-batch', {
            method: 'POST',
            body: { contact_ids: contactIds },
        });
        ctx.showToast(`Sent ${data.sent} of ${data.total} follow-ups`, data.sent > 0 ? 'success' : 'error');
        await ctx.loadFollowUpsPanelFn();
    });
}
