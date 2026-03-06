/* AVAIL v1.2.0 — Trouble Tickets: submit, my-tickets, admin dashboard.
 *
 * Called by: switchSettingsTab('tickets') in crm.js
 * Depends on: app.js (apiFetch, esc, showToast, sidebarNav)
 */

import {
    apiFetch, esc, showToast, sidebarNav,
} from 'app';

// ── Common Issues dropdown options ─────────────────────────────────────
var COMMON_ISSUES = [
    { label: '', title: '', hint: '' },
    { label: 'Search not working', title: 'Search returns no/wrong results for [part]', hint: 'What part number did you search? What did you expect to see?' },
    { label: 'Page won\'t load', title: 'Page fails to load: [which page]', hint: 'Which page? Do you see an error message or blank screen?' },
    { label: 'Data looks wrong', title: 'Incorrect data on [what]', hint: 'What data is wrong? What should it be?' },
    { label: 'Slow performance', title: 'Slow response on [where]', hint: 'Which page is slow? How long does it take to load?' },
    { label: 'Email/RFQ issue', title: 'Email or RFQ problem: [describe]', hint: 'Which RFQ? What happened? Did you get an error?' },
    { label: 'Other', title: '', hint: 'Describe what happened and what you expected.' },
];

var STATUS_LABELS = {
    submitted: 'Submitted', diagnosed: 'Diagnosed',
    fix_proposed: 'Fix Proposed', fix_in_progress: 'Fixing',
    fix_applied: 'Fix Applied', awaiting_verification: 'Awaiting Verify',
    in_progress: 'In Progress', open: 'Open',
    resolved: 'Resolved', escalated: 'Escalated', rejected: 'Rejected',
};
var STATUS_COLORS = {
    submitted: '#6b7280', diagnosed: '#7c3aed',
    fix_proposed: '#2563eb', fix_in_progress: '#2563eb',
    fix_applied: '#0891b2', awaiting_verification: '#d97706',
    in_progress: '#2563eb', open: '#6b7280',
    resolved: '#16a34a', escalated: '#dc2626', rejected: '#991b1b',
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
    var container = document.getElementById('settings-tickets');
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
            textContent: '\u2190 Back to Tickets',
            onclick: function() { renderMyTickets(container); },
        }),
    ]);
    container.appendChild(header);

    var form = el('form', { className: 'form-grid', style: 'max-width:700px;padding:16px;' });
    form.onsubmit = function(e) { e.preventDefault(); submitTicket(form, container); };

    // Common issues quick-select
    var commonSelect = el('select', { id: 'ttCommonIssue', style: 'width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:13px;' });
    commonSelect.appendChild(el('option', { value: '', textContent: 'Select a common issue (optional)...' }));
    COMMON_ISSUES.forEach(function(item) {
        if (item.label) commonSelect.appendChild(el('option', { value: item.label, textContent: item.label }));
    });
    commonSelect.onchange = function() {
        var sel = COMMON_ISSUES.find(function(i) { return i.label === commonSelect.value; });
        if (!sel) return;
        var titleInput = document.getElementById('ttTitle');
        var descArea = document.getElementById('ttDesc');
        if (sel.title && titleInput) titleInput.value = sel.title;
        if (sel.hint && descArea) descArea.placeholder = sel.hint;
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
        var resp = await apiFetch('/api/trouble-tickets/my-tickets');
        var data = resp.items || resp;
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
var _adminFilter = 'submitted';
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
    var filters = ['', 'submitted', 'diagnosed', 'escalated', 'resolved'];
    var labels = ['All', 'Submitted', 'Diagnosed', 'Escalated', 'Resolved'];
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

    // Health + Stats bar (loads async, non-blocking)
    var statsBar = el('div', { id: 'ttStatsBar', style: 'margin-bottom:12px;' });
    container.appendChild(statsBar);
    loadStatsBar(statsBar);

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
    if (isAdmin) cols.push('Linked');
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
        var st = t.status || 'open';
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
        // Linked count badge (admin only)
        if (isAdmin) {
            var linkedTd = el('td');
            if (t.child_count > 0) {
                linkedTd.appendChild(badge(String(t.child_count), '#7c3aed'));
            } else {
                linkedTd.appendChild(txt('—'));
            }
            row.appendChild(linkedTd);
        }
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

        // Status + risk + source row
        var metaRow = el('div', { style: 'display:flex;gap:8px;align-items:center;padding:0 16px 12px;flex-wrap:wrap;' });
        var st = t.status || 'open';
        metaRow.appendChild(badge(STATUS_LABELS[st] || st, STATUS_COLORS[st] || '#6b7280'));
        if (t.risk_tier) metaRow.appendChild(badge(t.risk_tier, RISK_COLORS[t.risk_tier] || '#6b7280'));
        if (t.category) metaRow.appendChild(el('span', { style: 'font-size:12px;color:var(--muted);', textContent: t.category }));
        if (t.source) {
            var srcLabel = t.source === 'report_button' ? 'Bug Report' : 'Ticket Form';
            metaRow.appendChild(el('span', { style: 'font-size:10px;padding:2px 6px;border-radius:4px;background:var(--bg-alt);color:var(--muted);border:1px solid var(--border);', textContent: srcLabel }));
        }
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

        // Linked tickets (child reports)
        if (t.child_tickets && t.child_tickets.length) {
            var linkedDiv = el('div', { style: 'margin-bottom:16px;' });
            linkedDiv.appendChild(el('strong', { style: 'font-size:12px;display:block;margin-bottom:8px;', textContent: 'Linked Reports (' + t.child_tickets.length + ')' }));
            t.child_tickets.forEach(function(child) {
                var childRow = el('div', {
                    style: 'padding:6px 10px;border:1px solid var(--border);border-radius:6px;margin-bottom:4px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;',
                    onclick: function() { showTicketDetail(child.id); },
                });
                var leftSide = el('div');
                leftSide.appendChild(el('span', { style: 'font-size:11px;color:var(--muted);margin-right:8px;', textContent: child.ticket_number }));
                leftSide.appendChild(el('span', { style: 'font-size:12px;', textContent: child.title }));
                childRow.appendChild(leftSide);
                if (child.similarity_score) {
                    childRow.appendChild(el('span', {
                        style: 'font-size:10px;color:var(--muted);',
                        textContent: Math.round(child.similarity_score * 100) + '% match',
                    }));
                }
                linkedDiv.appendChild(childRow);
            });
            body.appendChild(linkedDiv);
        }

        // Show parent link if this ticket is a child
        if (t.parent_ticket_id) {
            body.appendChild(el('div', {
                style: 'font-size:11px;color:var(--muted);margin-bottom:12px;cursor:pointer;',
                textContent: 'Linked to parent ticket #' + t.parent_ticket_id,
                onclick: function() { showTicketDetail(t.parent_ticket_id); },
            }));
        }

        // Diagnosis section (collapsible)
        if (t.diagnosis) {
            body.appendChild(collapsibleSection('Diagnosis', JSON.stringify(t.diagnosis, null, 2)));
        }

        // Generated prompt section (collapsible — admin only)
        if (t.generated_prompt && window.__isAdmin) {
            body.appendChild(collapsibleSection('Generated Prompt', t.generated_prompt));
        }

        // Fix info (admin only)
        if ((t.fix_branch || t.fix_pr_url) && window.__isAdmin) {
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

        // Browser / screen info (from report_button submissions — admin only)
        if ((t.browser_info || t.screen_size || t.current_view || t.current_page) && window.__isAdmin) {
            var ctxDiv = el('div', { style: 'margin-bottom:16px;font-size:11px;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:8px 12px;' });
            ctxDiv.appendChild(el('strong', { style: 'font-size:12px;color:var(--fg);display:block;margin-bottom:4px;', textContent: 'Browser Context' }));
            if (t.current_page) ctxDiv.appendChild(el('div', {}, ['URL: ' + t.current_page]));
            if (t.current_view) ctxDiv.appendChild(el('div', {}, ['View: ' + t.current_view]));
            if (t.browser_info) ctxDiv.appendChild(el('div', {}, ['Browser: ' + t.browser_info]));
            if (t.screen_size) ctxDiv.appendChild(el('div', {}, ['Screen: ' + t.screen_size]));
            body.appendChild(ctxDiv);
        }

        // Console errors (admin only)
        if (t.console_errors && window.__isAdmin) {
            try {
                var errs = JSON.parse(t.console_errors);
                if (errs.length) {
                    body.appendChild(collapsibleSection('Console Errors', errs.map(function(e) { return e.msg || e; }).join('\n')));
                }
            } catch(e) {
                body.appendChild(collapsibleSection('Console Errors', t.console_errors));
            }
        }

        // AI prompt section with copy/regenerate (admin only)
        if (t.ai_prompt && window.__isAdmin) {
            var promptDiv = el('div', { style: 'margin-bottom:16px;' });
            var promptHeader = el('div', { style: 'display:flex;align-items:center;gap:8px;margin-bottom:4px;' });
            promptHeader.appendChild(el('strong', { style: 'font-size:12px;', textContent: 'AI Prompt' }));
            var copyBtn = el('button', { className: 'btn btn-sm', textContent: 'Copy' });
            copyBtn.onclick = function() {
                var text = t.ai_prompt;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(function() {
                        showToast('Prompt copied to clipboard', 'success');
                    }).catch(function() { showToast('Failed to copy', 'error'); });
                }
            };
            promptHeader.appendChild(copyBtn);
            if (window.__isAdmin) {
                var regenBtn = el('button', { className: 'btn btn-sm btn-ghost', textContent: 'Regenerate' });
                regenBtn.onclick = async function() {
                    regenBtn.disabled = true;
                    regenBtn.textContent = 'Generating...';
                    try {
                        await apiFetch('/api/trouble-tickets/' + ticketId + '/regenerate-prompt', { method: 'POST' });
                        showToast('AI prompt regenerated', 'success');
                        showTicketDetail(ticketId);
                    } catch(e) {
                        showToast('Regeneration failed: ' + e.message, 'error');
                        regenBtn.disabled = false;
                        regenBtn.textContent = 'Regenerate';
                    }
                };
                promptHeader.appendChild(regenBtn);
            }
            promptDiv.appendChild(promptHeader);
            promptDiv.appendChild(el('pre', {
                style: 'background:var(--bg);padding:10px;border-radius:6px;font-size:11px;max-height:300px;overflow:auto;white-space:pre-wrap;word-wrap:break-word;border:1px solid var(--border);',
                textContent: t.ai_prompt,
            }));
            body.appendChild(promptDiv);
        } else if (window.__isAdmin) {
            var genDiv = el('div', { style: 'margin-bottom:16px;display:flex;align-items:center;gap:8px;' });
            genDiv.appendChild(el('span', { style: 'font-size:12px;color:var(--muted);', textContent: 'No AI prompt generated' }));
            var genBtn = el('button', { className: 'btn btn-sm', textContent: 'Generate' });
            genBtn.onclick = async function() {
                genBtn.disabled = true;
                genBtn.textContent = 'Generating...';
                try {
                    await apiFetch('/api/trouble-tickets/' + ticketId + '/regenerate-prompt', { method: 'POST' });
                    showToast('AI prompt generated', 'success');
                    showTicketDetail(ticketId);
                } catch(e) {
                    showToast('Generation failed: ' + e.message, 'error');
                    genBtn.disabled = false;
                    genBtn.textContent = 'Generate';
                }
            };
            genDiv.appendChild(genBtn);
            body.appendChild(genDiv);
        }

        // Screenshot
        if (t.screenshot_b64) {
            var ssDiv = el('div', { style: 'margin-bottom:16px;' });
            ssDiv.appendChild(el('strong', { style: 'font-size:12px;display:block;margin-bottom:4px;', textContent: 'Screenshot' }));
            var img = el('img', {
                src: t.screenshot_b64,
                style: 'max-width:100%;max-height:400px;border:1px solid var(--border);border-radius:6px;',
            });
            ssDiv.appendChild(img);
            body.appendChild(ssDiv);
        }

        // Admin notes
        if (window.__isAdmin) {
            var notesDiv = el('div', { style: 'margin-bottom:16px;border-top:1px solid var(--border);padding-top:12px;' });
            notesDiv.appendChild(el('label', { style: 'display:block;font-size:11px;font-weight:600;color:var(--muted);margin-bottom:4px;', textContent: 'Admin Notes' }));
            var notesArea = el('textarea', {
                id: 'ttAdminNotes',
                rows: '3',
                style: 'width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;resize:vertical;font-family:inherit;',
            });
            notesArea.value = t.admin_notes || '';
            notesDiv.appendChild(notesArea);
            var saveNotesBtn = el('button', { className: 'btn btn-sm', textContent: 'Save Notes', style: 'margin-top:4px;' });
            saveNotesBtn.onclick = async function() {
                try {
                    await apiFetch('/api/trouble-tickets/' + ticketId, {
                        method: 'PATCH', body: { admin_notes: notesArea.value },
                    });
                    showToast('Notes saved', 'success');
                } catch(e) {
                    showToast('Failed to save notes: ' + e.message, 'error');
                }
            };
            notesDiv.appendChild(saveNotesBtn);
            body.appendChild(notesDiv);
        } else if (t.admin_notes) {
            body.appendChild(el('div', { style: 'margin-bottom:16px;' }, [
                el('strong', { textContent: 'Admin Notes' }),
                el('p', { style: 'font-size:13px;white-space:pre-wrap;', textContent: t.admin_notes }),
            ]));
        }

        // Verify buttons (for submitter when awaiting_verification)
        // Admin actions
        if (window.__isAdmin && t.status !== 'resolved') {
            var adminRow = el('div', { style: 'display:flex;gap:8px;margin-top:16px;border-top:1px solid var(--border);padding-top:12px;' });
            adminRow.appendChild(el('strong', { textContent: 'Admin: ', style: 'align-self:center;font-size:12px;' }));

            if (!t.diagnosis) {
                adminRow.appendChild(el('button', {
                    className: 'btn btn-sm', textContent: 'AI Diagnose',
                    style: 'background:var(--blue);color:#fff;',
                    onclick: function() { diagnoseTicket(ticketId, container); },
                }));
            }
            if (t.generated_prompt && t.status !== 'escalated') {
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
                className: 'btn btn-sm', textContent: 'Resolve',
                style: 'background:#16a34a;color:#fff;',
                onclick: function() {
                    if (confirm('Mark this ticket as resolved?')) updateTicketStatus(ticketId, 'resolved', container);
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

// ── Stats Bar (admin dashboard health indicator) ───────────────────────
var _statsCache = null;
var _statsCacheTs = 0;

async function loadStatsBar(container) {
    // Cache for 60s to avoid hammering on every filter pill click
    var now = Date.now();
    if (_statsCache && now - _statsCacheTs < 60000) {
        renderStatsBar(container, _statsCache);
        return;
    }
    try {
        var data = await apiFetch('/api/trouble-tickets/stats');
        _statsCache = data;
        _statsCacheTs = now;
        renderStatsBar(container, data);
    } catch (e) { /* silent — stats bar is optional */ }
}

function renderStatsBar(container, data) {
    clearNode(container);
    var health = data.health || {};
    var stats = data.stats || {};

    var hColors = { green: '#16a34a', yellow: '#d97706', red: '#dc2626' };
    var hColor = hColors[health.status] || '#6b7280';

    var bar = el('div', {
        style: 'display:flex;gap:16px;align-items:center;padding:10px 14px;border-radius:8px;background:var(--bg-alt);border:1px solid var(--border);flex-wrap:wrap;',
    });

    // Health dot + label
    var dot = el('span', {
        style: 'display:inline-block;width:10px;height:10px;border-radius:50%;background:' + hColor + ';flex-shrink:0;',
    });
    bar.appendChild(el('div', { style: 'display:flex;align-items:center;gap:6px;' }, [
        dot,
        el('span', { style: 'font-weight:600;font-size:12px;color:' + hColor, textContent: health.status ? health.status.toUpperCase() : '—' }),
        el('span', { style: 'font-size:11px;color:var(--muted);', textContent: health.message || '' }),
    ]));

    // Stat chips
    var chips = [
        { label: 'Created', value: stats.tickets_created },
        { label: 'Resolved', value: stats.tickets_resolved },
        { label: 'Success', value: stats.success_rate != null ? stats.success_rate + '%' : '—' },
        { label: 'Avg Time', value: stats.avg_resolution_hours != null ? stats.avg_resolution_hours + 'h' : '—' },
        { label: 'Cost', value: stats.total_cost != null ? '$' + stats.total_cost.toFixed(2) : '—' },
    ];
    chips.forEach(function(c) {
        bar.appendChild(el('div', { style: 'text-align:center;min-width:60px;' }, [
            el('div', { style: 'font-size:15px;font-weight:700;color:var(--fg);', textContent: String(c.value != null ? c.value : 0) }),
            el('div', { style: 'font-size:10px;color:var(--muted);', textContent: c.label }),
        ]));
    });

    container.appendChild(bar);
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
                    sidebarNav('tickets', document.getElementById('navSettings'));
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

// ── Close notification panel on outside click ────────────────────────
document.addEventListener('click', function(e) {
    var panel = document.getElementById('sysNotifPanel');
    var bell = document.getElementById('sysNotifBell');
    if (!panel || panel.style.display === 'none') return;
    if (panel.contains(e.target) || (bell && bell.contains(e.target))) return;
    panel.style.display = 'none';
});

// ── Keyboard shortcuts for ticket detail (admin only) ────────────────
document.addEventListener('keydown', function(e) {
    if (!window.__isAdmin) return;
    // Skip if user is typing in an input/textarea/select
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

    var container = document.getElementById('view-tickets');
    if (!container || container.style.display === 'none') return;

    // Only active on ticket detail view (has a Back button)
    var backBtn = container.querySelector('.btn-ghost.btn-sm');
    if (!backBtn || backBtn.textContent !== 'Back') return;

    // Find ticket ID from the URL-like pattern or admin action buttons
    var execBtn = container.querySelector('button[style*="background:#16a34a"]');
    var escBtn = container.querySelector('button[style*="background:#f59e0b"]');
    var rejBtn = container.querySelector('button[style*="color:var(--red)"]');

    if (e.key === 'e' && execBtn && !execBtn.disabled) { execBtn.click(); }
    if (e.key === 's' && escBtn) { escBtn.click(); }
    if (e.key === 'r' && rejBtn) { rejBtn.click(); }
});
