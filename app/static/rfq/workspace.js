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
