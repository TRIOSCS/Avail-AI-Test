/**
 * HTMX + Alpine.js bootstrap — entry point for the AvailAI frontend.
 * Loaded when USE_HTMX=true. Replaces app.js + crm.js.
 *
 * W4.4: the old 3,292-line single file is split into ordered side-effect
 * modules under ./modules/ — this entry only sequences them and calls
 * Alpine.start(). The entry PATH must not change: the server manifest lookup
 * (app/routers/htmx/_shared.py) keys on 'htmx_app.js', and the vitest suite
 * imports this file directly.
 *
 * ORDER MATTERS — the import list mirrors the original file's top-to-bottom
 * order: globals first (vendor imports, Alpine.plugin calls, window.htmx /
 * window.Alpine, shared helpers), then stores and wiring (module-scope htmx
 * listeners must register before the first request), then the Alpine.data
 * component factories, then tab alerts. Every store/data registration happens
 * at module-eval time, so Alpine.start() stays the LAST statement.
 *
 * What calls it: Vite bundles this as the main entry point; loaded by base.html.
 * Depends on: ./modules/* (which own the vendor deps).
 */

import './modules/globals.js';
import './modules/stores.js';
import './modules/trouble_tickets.js';
import './modules/htmx_wiring.js';
import './modules/outreach_logger.js';
import './modules/layout_components.js';
import './modules/materials_filter.js';
import './modules/requisitions.js';
import './modules/rfq_vendor_modal.js';
import './modules/offer_forms.js';
import './modules/avatar_cropper.js';
import './modules/buy_plan_lines_editor.js';
import './modules/tab_alerts.js';

import Alpine from 'alpinejs';

Alpine.start();
