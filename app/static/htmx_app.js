/**
 * HTMX + Alpine.js bootstrap — entry point for the new frontend.
 * Loaded when USE_HTMX=true. Replaces app.js + crm.js.
 * Depends on: htmx.org, alpinejs (npm packages)
 */
import htmx from 'htmx.org';
import Alpine from 'alpinejs';

window.htmx = htmx;
window.Alpine = Alpine;

// Global Alpine stores
Alpine.store('sidebar', { open: true, active: '' });
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

// HTMX afterSettle — re-init Alpine on swapped content
htmx.on('htmx:afterSettle', () => {
    // Alpine auto-discovers new x-data elements
});

Alpine.start();
