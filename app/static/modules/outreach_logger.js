/**
 * outreach_logger.js — delegated [data-outreach-log] click logger for the CDM
 * contact panel: fire-and-forget keepalive fetch to /api/activity/outreach-initiated,
 * outcome toasts, the call-outcome prompt hook, and the CDM account-list refresh.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs (callOutcome store), ./globals.js (csrfToken, showToast).
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';
import { csrfToken, showToast } from './globals.js';

// ── Click-to-contact outreach logger (CDM contact panel) ────
// Any element with [data-outreach-log] (tel:/mailto:/Teams/WeChat links in
// customer contact panels) fires a fire-and-forget POST to
// /api/activity/outreach-initiated when clicked, logging the touch and
// bumping company/site last_activity_at. The default link navigation is NOT
// prevented — the native handler (dialer, mail client, Teams) still opens.
// P5.2 NOTE: this site intentionally stays on raw fetch() rather than the
// postJSON helper — it needs `keepalive: true` so the log POST survives the
// browser navigating away for the tel:/mailto:/Teams handler that fires in
// the same click, and XMLHttpRequest (which htmx.ajax wraps) has no keepalive
// equivalent; converting it would silently drop outreach logs on click. It
// also branches on the parsed response body (dropped_links, activity id for
// the call-outcome prompt), which fetch's r.json() gives directly.
document.body.addEventListener('click', (evt) => {
    const el = evt.target.closest('[data-outreach-log]');
    if (!el) return;
    const d = el.dataset;
    const payload = {
        channel: d.channel,
        contact_value: d.value,
        company_id: d.companyId ? parseInt(d.companyId, 10) : null,
        customer_site_id: d.siteId ? parseInt(d.siteId, 10) : null,
        site_contact_id: d.contactId ? parseInt(d.contactId, 10) : null,
        contact_name: d.contactName || null,
        origin: 'cdm_workspace',
    };
    const headers = { 'Content-Type': 'application/json' };
    const csrf = csrfToken();
    if (csrf) headers['x-csrftoken'] = csrf;
    // Refresh the CDM account list (if on the workspace) so the logged touch
    // is immediately visible in the staleness sort/labels. This refresh is
    // SYSTEM-initiated mid-workflow: unlike a user filter change it must not
    // reset pagination, so the current offset/limit (rendered as data-* on
    // the _account_list.html header — the filter form intentionally carries
    // no offset field) is passed through explicitly. source: #cdm-filters
    // includes the current filter values, like the pagination links do.
    const refreshAccountList = () => {
        const cdmFilters = document.getElementById('cdm-filters');
        const cdmList = document.getElementById('cdm-list');
        if (!cdmFilters || !cdmList) return;
        const meta = cdmList.querySelector('[data-offset]');
        const page = meta ? '?offset=' + meta.dataset.offset + '&limit=' + meta.dataset.limit : '';
        htmx.ajax('GET', '/v2/partials/customers/account-list' + page, {
            source: '#cdm-filters',
            target: '#cdm-list',
            swap: 'innerHTML',
            indicator: '#cdm-filters .htmx-indicator',
        });
    };
    // keepalive lets the request finish even if the click navigates away.
    // The fetch is never awaited before the default link action, so the
    // call/email/Teams handler always opens — but failures must still be
    // VISIBLE: a silent 429/500 means the rep believes the touch was logged
    // while the staleness sort quietly stops reflecting their work.
    fetch('/api/activity/outreach-initiated', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload),
        keepalive: true,
    }).then(async (resp) => {
        if (!resp.ok) {
            showToast(
                resp.status === 429
                    ? 'Outreach NOT logged — rate limit hit, wait a minute'
                    : 'Outreach NOT logged (error ' + resp.status + ')',
                'error'
            );
            return;
        }
        // The POST committed — from here on any failure is a RENDERING
        // problem, not a transport one. Contain it so the .catch below (the
        // rep-facing "NOT logged" message) only ever reports genuine fetch
        // rejections; a false "NOT logged" toast invites a duplicate re-click.
        let droppedLinks = [];
        let body = {};
        try {
            body = await resp.json();
            droppedLinks = body.dropped_links || [];
        } catch (err) {
            console.error('[outreach-log] could not parse response body', err);
        }
        try {
            if (droppedLinks.length) {
                // Logged, but the server dropped stale entity links — the touch
                // exists yet won't show on this account, so don't claim success
                // (and skip the list refresh: nothing changed for this view).
                showToast(
                    'Outreach logged, but the ' + droppedLinks.join('/') +
                    ' link no longer exists — refresh the page',
                    'warning'
                );
                return;
            }
            const labels = { phone: 'Call', email: 'Email', teams: 'Teams message', wechat: 'WeChat message' };
            showToast(
                (labels[d.channel] || 'Outreach') + ' logged' + (d.contactName ? ' — ' + d.contactName : ''),
                'success'
            );
            if (payload.channel === 'phone' && body && body.id) {
                const store = Alpine.store('callOutcome');
                store.activityId = body.id;
                store.contactName = d.contactName || '';
                store.note = '';
                store.show = true;
            }
            refreshAccountList();
        } catch (err) {
            console.error('[outreach-log] post-success UI update failed', err);
        }
    }).catch((err) => {
        console.error('[outreach-log] failed', err);
        showToast('Outreach NOT logged — network error', 'error');
    });
});
