/**
 * HTMX + Alpine.js bootstrap — entry point for the new frontend.
 * Loaded when USE_HTMX=true. Replaces app.js + crm.js.
 * Depends on: htmx.org, alpinejs, @alpinejs/trap (npm packages)
 */
import htmx from 'htmx.org';
import Alpine from 'alpinejs';
import trap from '@alpinejs/trap';
import './styles.css';
import './htmx_mobile.css';

Alpine.plugin(trap);

window.htmx = htmx;
window.Alpine = Alpine;

// Global Alpine stores
Alpine.store('sidebar', { open: true, collapsed: false, active: '' });
Alpine.store('toast', { message: '', type: 'info', show: false });

// HTMX config
htmx.config.defaultSwapStyle = 'innerHTML';
htmx.config.historyCacheSize = 0;
htmx.config.selfRequestsOnly = true;

// HTMX error handler — show toast on failed requests
htmx.on('htmx:responseError', (evt) => {
    Alpine.store('toast').message = 'Request failed. Please try again.';
    Alpine.store('toast').type = 'error';
    Alpine.store('toast').show = true;
});

// 401 → redirect to login
document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.detail.xhr.status === 401) {
        window.location.href = '/auth/login';
    }
});

/**
 * sourcingProgress — Alpine component for SSE sourcing search progress.
 * Listens for SSE events from the HTMX SSE extension and updates the
 * per-source progress UI (count, elapsed time, status icon, progress bar).
 * On search-complete, loads the full sourcing results via HTMX ajax.
 *
 * Called by: partials/sourcing/search_progress.html
 * Depends on: htmx SSE extension, tpl-icon-check/tpl-icon-fail templates in base.html
 */
Alpine.data('sourcingProgress', (requirementId, totalSources) => ({
    completed: 0,
    init() {
        document.body.addEventListener('htmx:sseMessage', (evt) => {
            if (evt.detail.type === 'source-complete') {
                this.handleSourceComplete(JSON.parse(evt.detail.data));
            }
            if (evt.detail.type === 'search-complete') {
                this.handleSearchComplete(JSON.parse(evt.detail.data));
            }
        });
    },
    handleSourceComplete(data) {
        this.completed++;
        var source = data.source.toLowerCase();
        // Update count text
        var countEl = document.getElementById('source-count-' + source + '-' + requirementId);
        if (countEl) countEl.textContent = data.count + ' results';
        // Update elapsed time text
        var timeEl = document.getElementById('source-time-' + source + '-' + requirementId);
        if (timeEl) timeEl.textContent = (data.elapsed_ms / 1000).toFixed(1) + 's';
        // Update status icon — clone from template SVGs
        var statusEl = document.getElementById('source-status-' + source + '-' + requirementId);
        if (statusEl) {
            var tplId = data.status === 'done' ? 'tpl-icon-check' : 'tpl-icon-fail';
            var tpl = document.getElementById(tplId);
            if (tpl) {
                statusEl.replaceChildren(tpl.content.cloneNode(true));
            }
        }
        // Update progress bar width
        var pct = Math.round((this.completed / totalSources) * 100);
        var bar = document.getElementById('progress-bar-' + requirementId);
        if (bar) bar.style.width = pct + '%';
        var counter = document.getElementById('progress-count-' + requirementId);
        if (counter) counter.textContent = this.completed + ' / ' + totalSources + ' complete';
    },
    handleSearchComplete(data) {
        setTimeout(function() {
            htmx.ajax('GET', '/v2/partials/sourcing/' + requirementId, '#main-content');
        }, 500);
    }
}));

Alpine.start();
