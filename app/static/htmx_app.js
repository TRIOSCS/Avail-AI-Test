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

Alpine.start();
