/*
 * rfq/workspace.js — RFQ workspace tab data-fetch helpers.
 * Called by: app/static/app.js (_rfqLoadTab).
 * Depends on: apiFetch callback passed from app.js.
 */

export async function fetchRfqWorkspaceTabData(ctx, tab, partId, reqId) {
    switch (tab) {
        case 'offers':
            return ctx.apiFetch(`/api/requirements/${partId}/offers`);
        case 'activity': {
            const [notes, tasks, history] = await Promise.all([
                ctx.apiFetch(`/api/requirements/${partId}/notes`).catch(() => ({})),
                ctx.apiFetch(`/api/requirements/${partId}/tasks`).catch(() => []),
                ctx.apiFetch(`/api/requirements/${partId}/history`).catch(() => []),
            ]);
            return { notes, tasks, history };
        }
        case 'sightings':
            return ctx.apiFetch(`/api/requisitions/${reqId}/sightings`);
        default:
            return null;
    }
}

// ── Offer status & retry actions ────────────────────────────────────────────────
// Error messages displayed via showToast on failure:
// "Couldn't retry RFQ — " + error detail
// "Couldn't update response status — " + error detail

export async function updateRfqOfferStatus(ctx, offerId, status) {
    try {
        return await ctx.apiFetch(`/api/offers/${offerId}`, { method: 'PUT', body: { status } });
    } catch (e) {
        if (ctx.showToast) ctx.showToast("Couldn't update response status — " + (e.message || 'unknown error'), 'error');
        throw e;
    }
}

export async function retryRfqContact(ctx, contactId) {
    try {
        return await ctx.apiFetch(`/api/contacts/${contactId}/retry`, { method: 'POST', body: {} });
    } catch (e) {
        if (ctx.showToast) ctx.showToast("Couldn't retry RFQ — " + (e.message || 'unknown error'), 'error');
        throw e;
    }
}
