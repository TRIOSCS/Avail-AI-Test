/* AVAIL v1.2.0 — Trouble Tickets: submit, my-tickets, admin dashboard.
 *
 * Called by: sidebarNav('tickets') in app.js
 * Depends on: app.js (apiFetch, esc, showView, showToast, sidebarNav)
 */

import {
    apiFetch, esc, showView, showToast, sidebarNav,
} from 'app';

// ── Common Issues dropdown options ─────────────────────────────────────
var COMMON_ISSUES = [
    '',
    'Search results not loading',
    'RFQ emails not sending',
    'Login / session expired unexpectedly',
    'Page loads slowly or times out',
    'Data missing or incorrect',
];

var STATUS_LABELS = {
    submitted: 'Submitted', triaging: 'Triaging', diagnosed: 'Diagnosed',
    prompt_ready: 'Prompt Ready', fix_in_progress: 'Fixing',
    awaiting_verification: 'Verify', resolved: 'Resolved',
    rejected: 'Rejected', escalated: 'Escalated',
};
var STATUS_COLORS = {
    submitted: '#6b7280', triaging: '#d97706', diagnosed: '#2563eb',
    prompt_ready: '#7c3aed', fix_in_progress: '#ea580c',
    awaiting_verification: '#0891b2', resolved: '#16a34a',
    rejected: '#dc2626', escalated: '#dc2626',
};
var RISK_COLORS = { low: '#16a34a', medium: '#d97706', high: '#dc2626' };

// ── DOM helpers (avoid innerHTML for security) ─────────────────────────
function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
        for (var k in attrs) {
            if (k === 'className') node.className = attrs[k];
            else if (k === 'textContent') node.textContent = attrs[k];
            else if (k === 'onclick') node.onclick = attrs[k];
            else if (k === 'onchange') node.onchange = attrs[k];
            else if (k === 'onsubmit') node.onsubmit = attrs[k];
            else if (k === 'oninput') node.oninput = attrs[k];
            else node.setAttribute(k, attrs[k]);
        }
    }
    if (children) {
        if (!Array.isArray(children)) children = [children];
        for (var i = 0; i < children.length; i++) {
            var c = children[i];
            if (c == null) continue;
            if (typeof c === 'string') node.appendChild(document.createTextNode(c));
            else node.appendChild(c);
        }
    }
    return node;
}

function txt(s) { return document.createTextNode(s || ''); }

function badge(label, color) {
    return el('span', {
        className: 'chip on',
        style: 'background:' + color + ';color:#fff;font-size:10px;padding:2px 8px;border-radius:10px;',
        textContent: label,
    });
}

function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
}

// ── Show Tickets View (entry point from sidebarNav) ───────────────────
function showTickets() {
    showView('view-tickets');
    var container = document.getElementById('view-tickets');
    if (!container) return;
    if (window.__isAdmin) {
        renderAdminDashboard(container);
    } else {
        renderMyTickets(container);
    }
}
window.showTickets = showTickets;

// ── Submit Ticket Form ────────────────────────────────────────────────
function renderSubmitForm(container) {
    clearNode(container);
    var header = el('div', { className: 'vendor-header' }, [
        el('h2', {}, ['Submit Trouble Ticket']),
        el('button', {
            className: 'btn btn-ghost btn-sm',
            textContent: 'My Tickets',
            onclick: function() { renderMyTickets(container); },
        }),
    ]);
    container.appendChild(header);

    var form = el('form', { className: 'form-grid', style: 'max-width:700px;padding:16px;' });
    form.onsubmit = function(e) { e.preventDefault(); submitTicket(form, container); };

    // Common issues quick-select
    var commonSelect = el('select', { id: 'ttCommonIssue', style: 'width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:13px;' });
    commonSelect.appendChild(el('option', { value: '', textContent: 'Select a common issue (optional)...' }));
    COMMON_ISSUES.forEach(function(issue) {
        if (issue) commonSelect.appendChild(el('option', { value: issue, textContent: issue }));
    });
    commonSelect.onchange = function() {
        var titleInput = document.getElementById('ttTitle');
        if (this.value && titleInput) titleInput.value = this.value;
    };

    var titleInput = el('input', {
        id: 'ttTitle', type: 'text', required: 'required',
        placeholder: 'Brief description of the issue',
        maxlength: '200',
        style: 'width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:13px;',
    });

    var descArea = el('textarea', {
        id: 'ttDesc', required: 'required', rows: '5',
        placeholder: 'Describe what happened, what you expected, and steps to reproduce...',
        style: 'width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:13px;resize:vertical;font-family:inherit;',
    });

    var submitBtn = el('button', {
        type: 'submit', className: 'btn btn-primary',
        textContent: 'Submit Ticket',
    });

    form.appendChild(fieldWrap('Quick Select', commonSelect));
    form.appendChild(fieldWrap('Title *', titleInput));
    form.appendChild(fieldWrap('Description *', descArea));
    form.appendChild(submitBtn);

    container.appendChild(form);
}

function fieldWrap(label, input) {
    var div = el('div', { style: 'margin-bottom:12px;' }, [
        el('label', { style: 'display:block;font-size:11px;font-weight:600;color:var(--muted);margin-bottom:4px;', textContent: label }),
        input,
    ]);
    return div;
}

async function submitTicket(form, container) {
    var title = document.getElementById('ttTitle').value.trim();
    var desc = document.getElementById('ttDesc').value.trim();
    if (!title || !desc) { showToast('Title and description are required', 'error'); return; }

    var btn = form.querySelector('button[type="submit"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting...'; }

    try {
        var data = await apiFetch('/api/trouble-tickets', {
            method: 'POST',
            body: {
                title: title,
                description: desc,
                current_page: window.location.hash || window.location.pathname,
                frontend_errors: (window.__errorBuffer || []).slice(-5),
            },
        });
        showToast('Ticket ' + data.ticket_number + ' submitted', 'success');
        renderMyTickets(container);
    } catch (e) {
        showToast('Failed to submit ticket: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Submit Ticket'; }
    }
}

// ── My Tickets View ───────────────────────────────────────────────────
async function renderMyTickets(container) {
    clearNode(container);
    var header = el('div', { className: 'vendor-header' }, [
        el('h2', {}, ['My Tickets']),
        el('button', {
            className: 'btn btn-primary btn-sm',
            textContent: '+ New Ticket',
            onclick: function() { renderSubmitForm(container); },
        }),
    ]);
    container.appendChild(header);
    container.appendChild(el('p', { className: 'empty', textContent: 'Loading...' }));

    try {
        var data = await apiFetch('/api/trouble-tickets/my-tickets');
        clearNode(container);
        container.appendChild(header);

        if (!data.length) {
            container.appendChild(el('p', { className: 'empty', textContent: 'No tickets yet. Submit one to get started.' }));
            return;
        }
        var table = buildTicketTable(data, false);
        container.appendChild(table);
    } catch (e) {
        clearNode(container);
        container.appendChild(header);
        container.appendChild(el('p', { className: 'empty', textContent: 'Failed to load tickets.' }));
    }
}

// ── Admin Dashboard ───────────────────────────────────────────────────
var _adminFilter = '';
var _adminOffset = 0;

async function renderAdminDashboard(container) {
    clearNode(container);
    var header = el('div', { className: 'vendor-header' }, [
        el('h2', {}, ['Trouble Tickets']),
        el('button', {
            className: 'btn btn-primary btn-sm',
            textContent: '+ New Ticket',
            onclick: function() { renderSubmitForm(container); },
        }),
    ]);
    container.appendChild(header);

    // Filter pills
    var pills = el('div', { className: 'fpills fpills-sm', style: 'margin-bottom:12px;' });
    var filters = ['', 'submitted', 'diagnosed', 'prompt_ready', 'fix_in_progress', 'awaiting_verification', 'resolved', 'escalated'];
    var labels = ['All', 'Submitted', 'Diagnosed', 'Review Queue', 'Fixing', 'Verify', 'Resolved', 'Escalated'];
    filters.forEach(function(f, i) {
        var btn = el('button', {
            type: 'button',
            className: 'fp fp-sm' + (f === _adminFilter ? ' on' : ''),
            textContent: labels[i],
            onclick: function() {
                _adminFilter = f;
                _adminOffset = 0;
                renderAdminDashboard(container);
            },
        });
        pills.appendChild(btn);
    });
    container.appendChild(pills);

    container.appendChild(el('p', { className: 'empty', textContent: 'Loading...' }));

    try {
        var url = '/api/trouble-tickets?limit=50&offset=' + _adminOffset;
        if (_adminFilter) url += '&status=' + _adminFilter;
        var data = await apiFetch(url);

        // Remove loading indicator
        var loading = container.querySelector('.empty');
        if (loading) loading.remove();

        if (!data.items || !data.items.length) {
            container.appendChild(el('p', { className: 'empty', textContent: 'No tickets found.' }));
            return;
        }

        var table = buildTicketTable(data.items, true);
        container.appendChild(table);

        // Pagination
        if (data.total > data.limit) {
            var pag = el('div', { style: 'display:flex;gap:8px;justify-content:center;padding:12px;' });
            if (_adminOffset > 0) {
                pag.appendChild(el('button', {
                    className: 'btn btn-ghost btn-sm', textContent: 'Previous',
                    onclick: function() { _adminOffset = Math.max(0, _adminOffset - 50); renderAdminDashboard(container); },
                }));
            }
            if (_adminOffset + data.limit < data.total) {
                pag.appendChild(el('button', {
                    className: 'btn btn-ghost btn-sm', textContent: 'Next',
                    onclick: function() { _adminOffset += 50; renderAdminDashboard(container); },
                }));
            }
            pag.appendChild(el('span', {
                style: 'font-size:11px;color:var(--muted);align-self:center;',
                textContent: 'Showing ' + (data.offset + 1) + '-' + Math.min(data.offset + data.limit, data.total) + ' of ' + data.total,
            }));
            container.appendChild(pag);
        }
    } catch (e) {
        var loadEl = container.querySelector('.empty');
        if (loadEl) loadEl.textContent = 'Failed to load tickets.';
    }
}

// ── Shared ticket table builder ───────────────────────────────────────
function buildTicketTable(tickets, isAdmin) {
    var table = el('table', { className: 'tbl', style: 'width:100%;' });
    var thead = el('thead');
    var headRow = el('tr');
    var cols = ['#', 'Title', 'Status', 'Risk', 'Created'];
    if (isAdmin) cols.splice(2, 0, 'Submitter');
    cols.forEach(function(c) {
        headRow.appendChild(el('th', { textContent: c }));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el('tbody');
    tickets.forEach(function(t) {
        var row = el('tr', { style: 'cursor:pointer;' });
        row.onclick = function() { showTicketDetail(t.id || t.ticket_id); };

        row.appendChild(el('td', { textContent: t.ticket_number || '' }));
        row.appendChild(el('td', { textContent: t.title || '' }));
        if (isAdmin) {
            row.appendChild(el('td', { textContent: t.submitted_by_name || 'User #' + (t.submitted_by || '?') }));
        }
        // Status badge
        var statusTd = el('td');
        var st = t.status || 'submitted';
        statusTd.appendChild(badge(STATUS_LABELS[st] || st, STATUS_COLORS[st] || '#6b7280'));
        row.appendChild(statusTd);
        // Risk badge
        var riskTd = el('td');
        if (t.risk_tier) {
            riskTd.appendChild(badge(t.risk_tier, RISK_COLORS[t.risk_tier] || '#6b7280'));
        } else {
            riskTd.appendChild(txt('—'));
        }
        row.appendChild(riskTd);
        // Created
        row.appendChild(el('td', {
            textContent: t.created_at ? new Date(t.created_at).toLocaleDateString() : '—',
            style: 'font-size:12px;color:var(--muted);',
        }));

        tbody.appendChild(row);
    });
    table.appendChild(tbody);
    return table;
}

// ── Ticket Detail View ────────────────────────────────────────────────
async function showTicketDetail(ticketId) {
    var container = document.getElementById('view-tickets');
    if (!container) return;
    clearNode(container);
    container.appendChild(el('p', { className: 'empty', textContent: 'Loading...' }));

    try {
        var t = await apiFetch('/api/trouble-tickets/' + ticketId);
        clearNode(container);

        // Back button
        var backBtn = el('button', {
            className: 'btn btn-ghost btn-sm',
            textContent: 'Back',
            onclick: function() { showTickets(); },
        });

        var header = el('div', { className: 'vendor-header' }, [
            el('h2', {}, [t.ticket_number || 'Ticket']),
            backBtn,
        ]);
        container.appendChild(header);

        // Status + risk row
        var metaRow = el('div', { style: 'display:flex;gap:8px;align-items:center;padding:0 16px 12px;' });
        var st = t.status || 'submitted';
        metaRow.appendChild(badge(STATUS_LABELS[st] || st, STATUS_COLORS[st] || '#6b7280'));
        if (t.risk_tier) metaRow.appendChild(badge(t.risk_tier, RISK_COLORS[t.risk_tier] || '#6b7280'));
        if (t.category) metaRow.appendChild(el('span', { style: 'font-size:12px;color:var(--muted);', textContent: t.category }));
        container.appendChild(metaRow);

        // Title + description
        var body = el('div', { style: 'padding:0 16px;' });
        body.appendChild(el('h3', { style: 'margin-bottom:8px;', textContent: t.title || '' }));
        body.appendChild(el('p', { style: 'white-space:pre-wrap;color:var(--fg);font-size:13px;margin-bottom:16px;', textContent: t.description || '' }));

        // Timestamps
        var times = el('div', { style: 'font-size:11px;color:var(--muted);margin-bottom:16px;' });
        if (t.created_at) times.appendChild(el('div', {}, ['Created: ' + new Date(t.created_at).toLocaleString()]));
        if (t.diagnosed_at) times.appendChild(el('div', {}, ['Diagnosed: ' + new Date(t.diagnosed_at).toLocaleString()]));
        if (t.resolved_at) times.appendChild(el('div', {}, ['Resolved: ' + new Date(t.resolved_at).toLocaleString()]));
        body.appendChild(times);

        // Diagnosis section (collapsible)
        if (t.diagnosis) {
            body.appendChild(collapsibleSection('Diagnosis', JSON.stringify(t.diagnosis, null, 2)));
        }

        // Generated prompt section (collapsible)
        if (t.generated_prompt) {
            body.appendChild(collapsibleSection('Generated Prompt', t.generated_prompt));
        }

        // Fix info
        if (t.fix_branch || t.fix_pr_url) {
            var fixInfo = el('div', { style: 'margin-bottom:16px;' });
            fixInfo.appendChild(el('strong', { textContent: 'Fix Info' }));
            if (t.fix_branch) fixInfo.appendChild(el('div', { style: 'font-size:12px;', textContent: 'Branch: ' + t.fix_branch }));
            if (t.fix_pr_url) {
                var prLink = el('a', { href: t.fix_pr_url, target: '_blank', textContent: 'View PR' });
                var prDiv = el('div', { style: 'font-size:12px;' }, ['PR: ', prLink]);
                fixInfo.appendChild(prDiv);
            }
            if (t.iterations_used) fixInfo.appendChild(el('div', { style: 'font-size:12px;', textContent: 'Iterations: ' + t.iterations_used }));
            if (t.cost_usd) fixInfo.appendChild(el('div', { style: 'font-size:12px;', textContent: 'Cost: $' + t.cost_usd.toFixed(2) }));
            body.appendChild(fixInfo);
        }

        // Resolution notes
        if (t.resolution_notes) {
            body.appendChild(el('div', { style: 'margin-bottom:16px;' }, [
                el('strong', { textContent: 'Resolution Notes' }),
                el('p', { style: 'font-size:13px;white-space:pre-wrap;', textContent: t.resolution_notes }),
            ]));
        }

        // Verify buttons (for submitter when awaiting_verification)
        if (t.status === 'awaiting_verification') {
            var verifyRow = el('div', { style: 'display:flex;gap:8px;margin-top:16px;' });
            verifyRow.appendChild(el('button', {
                className: 'btn btn-primary',
                textContent: 'Confirm Fixed',
                onclick: function() { verifyTicket(ticketId, 'resolved', container); },
            }));
            verifyRow.appendChild(el('button', {
                className: 'btn btn-ghost',
                textContent: 'Still Broken',
                onclick: function() { verifyTicket(ticketId, 'still_broken', container); },
            }));
            body.appendChild(verifyRow);
        }

        // Admin actions
        if (window.__isAdmin && t.status !== 'resolved' && t.status !== 'rejected') {
            var adminRow = el('div', { style: 'display:flex;gap:8px;margin-top:16px;border-top:1px solid var(--border);padding-top:12px;' });
            adminRow.appendChild(el('strong', { textContent: 'Admin: ', style: 'align-self:center;font-size:12px;' }));

            if (t.status === 'submitted') {
                adminRow.appendChild(el('button', {
                    className: 'btn btn-sm', textContent: 'Start Triage',
                    onclick: function() { updateTicketStatus(ticketId, 'triaging', container); },
                }));
            }
            if (!t.diagnosis && (t.status === 'submitted' || t.status === 'triaging')) {
                adminRow.appendChild(el('button', {
                    className: 'btn btn-sm', textContent: 'AI Diagnose',
                    style: 'background:var(--blue);color:#fff;',
                    onclick: function() { diagnoseTicket(ticketId, container); },
                }));
            }
            if (t.generated_prompt && (t.status === 'diagnosed' || t.status === 'prompt_ready')) {
                adminRow.appendChild(el('button', {
                    className: 'btn btn-sm', textContent: 'Execute Fix',
                    style: 'background:#16a34a;color:#fff;',
                    onclick: function() { executeTicketFix(ticketId, container); },
                }));
            }
            if (t.status !== 'escalated') {
                adminRow.appendChild(el('button', {
                    className: 'btn btn-sm', textContent: 'Escalate',
                    style: 'background:#f59e0b;color:#fff;',
                    onclick: function() {
                        if (confirm('Escalate this ticket to human review?')) updateTicketStatus(ticketId, 'escalated', container);
                    },
                }));
            }
            adminRow.appendChild(el('button', {
                className: 'btn btn-sm btn-ghost', textContent: 'Reject',
                style: 'color:var(--red);',
                onclick: function() {
                    if (confirm('Reject this ticket?')) updateTicketStatus(ticketId, 'rejected', container);
                },
            }));
            body.appendChild(adminRow);
        }

        container.appendChild(body);
    } catch (e) {
        clearNode(container);
        container.appendChild(el('p', { className: 'empty', textContent: 'Failed to load ticket.' }));
    }
}

function collapsibleSection(title, content) {
    var wrapper = el('div', { style: 'margin-bottom:12px;border:1px solid var(--border);border-radius:6px;' });
    var headerBtn = el('button', {
        type: 'button',
        style: 'width:100%;text-align:left;padding:8px 12px;font-weight:600;font-size:12px;background:var(--bg-alt);border:none;border-radius:6px;cursor:pointer;',
        textContent: title + ' [+]',
    });
    var body = el('pre', {
        style: 'display:none;padding:8px 12px;font-size:11px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;margin:0;',
        textContent: content,
    });
    headerBtn.onclick = function() {
        var showing = body.style.display !== 'none';
        body.style.display = showing ? 'none' : 'block';
        headerBtn.textContent = title + (showing ? ' [+]' : ' [-]');
    };
    wrapper.appendChild(headerBtn);
    wrapper.appendChild(body);
    return wrapper;
}

// ── Actions ───────────────────────────────────────────────────────────
async function verifyTicket(ticketId, verdict, container) {
    try {
        await apiFetch('/api/trouble-tickets/' + ticketId + '/verify', {
            method: 'POST', body: { verdict: verdict },
        });
        showToast(verdict === 'resolved' ? 'Ticket confirmed as fixed' : 'Ticket re-opened — follow-up created', 'success');
        showTicketDetail(ticketId);
    } catch (e) {
        showToast('Verify failed: ' + e.message, 'error');
    }
}

async function diagnoseTicket(ticketId, container) {
    try {
        showToast('Running AI diagnosis...', 'info');
        var result = await apiFetch('/api/trouble-tickets/' + ticketId + '/diagnose', { method: 'POST' });
        showToast('Diagnosis complete — risk: ' + (result.risk_tier || 'unknown'), 'success');
        showTicketDetail(ticketId);
    } catch (e) {
        showToast('Diagnosis failed: ' + e.message, 'error');
    }
}

async function executeTicketFix(ticketId, container) {
    if (!confirm('Execute AI-generated fix for this ticket?')) return;
    try {
        showToast('Executing fix...', 'info');
        var result = await apiFetch('/api/trouble-tickets/' + ticketId + '/execute', { method: 'POST' });
        showToast(result.message || 'Fix executed', 'success');
        showTicketDetail(ticketId);
    } catch (e) {
        showToast('Execution failed: ' + e.message, 'error');
        showTicketDetail(ticketId);
    }
}

async function updateTicketStatus(ticketId, newStatus, container) {
    try {
        await apiFetch('/api/trouble-tickets/' + ticketId, {
            method: 'PATCH', body: { status: newStatus },
        });
        showToast('Ticket updated to ' + (STATUS_LABELS[newStatus] || newStatus), 'success');
        showTicketDetail(ticketId);
    } catch (e) {
        showToast('Update failed: ' + e.message, 'error');
    }
}

// ── System Notifications (self-heal pipeline) ────────────────────────────
var _sysNotifTimer = null;

var SYS_EVENT_COLORS = {
    diagnosed: '#2563eb', prompt_ready: '#7c3aed', escalated: '#f59e0b',
    fixed: '#16a34a', failed: '#ef4444',
};

var SYS_EVENT_LABELS = {
    diagnosed: 'Diagnosed', prompt_ready: 'Prompt Ready', escalated: 'Escalated',
    fixed: 'Fixed', failed: 'Failed',
};

function toggleSysNotifs() {
    var panel = document.getElementById('sysNotifPanel');
    if (!panel) return;
    var open = panel.style.display !== 'none';
    panel.style.display = open ? 'none' : 'block';
    if (!open) loadSysNotifs();
}

async function loadSysNotifs() {
    var el = document.getElementById('sysNotifList');
    if (!el) return;
    try {
        var data = await apiFetch('/api/notifications/unread');
        var items = data.items || [];
        if (!items.length) {
            el.textContent = '';
            var p = document.createElement('p');
            p.style.cssText = 'color:#94a3b8;font-size:12px;text-align:center;padding:16px 0';
            p.textContent = 'No alerts';
            el.appendChild(p);
            return;
        }
        el.textContent = '';
        items.forEach(function(n) {
            var color = SYS_EVENT_COLORS[n.event_type] || '#6b7280';
            var label = SYS_EVENT_LABELS[n.event_type] || n.event_type;
            var row = document.createElement('div');
            row.style.cssText = 'padding:8px 10px;border-radius:8px;margin-bottom:4px;cursor:pointer;border-left:3px solid ' + color + ';background:#f8fafc;transition:background .15s';
            row.onmouseover = function() { row.style.background = '#f1f5f9'; };
            row.onmouseout = function() { row.style.background = '#f8fafc'; };
            row.onclick = function() {
                markSysNotifRead(n.id);
                if (n.ticket_id) {
                    document.getElementById('sysNotifPanel').style.display = 'none';
                    sidebarNav('tickets', document.getElementById('troublePill'));
                    setTimeout(function() { showTicketDetail(n.ticket_id); }, 300);
                }
            };

            var badge = document.createElement('span');
            badge.style.cssText = 'display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;color:#fff;background:' + color + ';margin-right:6px';
            badge.textContent = label;

            var title = document.createElement('span');
            title.style.cssText = 'font-weight:600;font-size:12px;color:#1e293b';
            title.textContent = n.title;

            var header = document.createElement('div');
            header.style.cssText = 'display:flex;align-items:center;gap:4px;margin-bottom:2px';
            header.appendChild(badge);
            header.appendChild(title);
            row.appendChild(header);

            if (n.body) {
                var body = document.createElement('div');
                body.style.cssText = 'font-size:11px;color:#64748b;line-height:1.3';
                body.textContent = n.body.length > 120 ? n.body.slice(0, 120) + '...' : n.body;
                row.appendChild(body);
            }

            var time = document.createElement('div');
            time.style.cssText = 'font-size:10px;color:#94a3b8;margin-top:2px';
            time.textContent = _sysNotifTimeAgo(n.created_at);
            row.appendChild(time);

            el.appendChild(row);
        });
    } catch (e) {
        el.textContent = '';
        var err = document.createElement('p');
        err.style.cssText = 'color:#ef4444;font-size:12px;text-align:center';
        err.textContent = 'Failed to load alerts';
        el.appendChild(err);
    }
}

async function markSysNotifRead(id) {
    try { await apiFetch('/api/notifications/' + id + '/read', { method: 'POST' }); } catch (e) { /* silent */ }
    loadSysNotifBadge();
}

async function markAllSysNotifsRead() {
    try { await apiFetch('/api/notifications/read-all', { method: 'POST' }); } catch (e) { /* silent */ }
    loadSysNotifBadge();
    loadSysNotifs();
}

async function loadSysNotifBadge() {
    var badge = document.getElementById('sysNotifCount');
    if (!badge) return;
    try {
        var data = await apiFetch('/api/notifications/unread?limit=1');
        var count = data.count || 0;
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.style.display = count > 0 ? '' : 'none';
        // Pulse animation on bell when there are unread
        var bell = document.getElementById('sysNotifBell');
        if (bell) bell.style.animation = count > 0 ? 'sysNotifPulse 2s ease-in-out infinite' : 'none';
    } catch (e) { /* silent */ }
}

function _sysNotifTimeAgo(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    var s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}

function startSysNotifPolling() {
    if (_sysNotifTimer || !document.getElementById('sysNotifBell')) return;
    loadSysNotifBadge();
    _sysNotifTimer = setInterval(loadSysNotifBadge, 30000);
}

// Expose to window for onclick handlers in HTML
window.toggleSysNotifs = toggleSysNotifs;
window.markAllSysNotifsRead = markAllSysNotifsRead;

// Start polling when DOM is ready (admin only — bell exists)
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startSysNotifPolling);
} else {
    startSysNotifPolling();
}
