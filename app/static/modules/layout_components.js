/**
 * layout_components.js — layout/chrome Alpine.data components: splitPanel
 * (resizable split-panel), resizableModal (global modal wrapper with
 * drag-move/drag-resize + per-bucket geometry persistence), contactsView
 * (CRM contacts client-side filter), and dedupSelect (Data Ops multi-select).
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs, ../modal_geometry.js.
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';
// Pure geometry math for the resizable/movable modal wrapper (see base.html).
import { resizeGeometry, moveGeometry, clampToViewport } from '../modal_geometry.js';

/**
 * splitPanel — Alpine.js component for resizable split-panel layout.
 * Left panel is a scrollable list; right panel is a detail view.
 * User can drag the divider to resize. Position is persisted to localStorage.
 *
 * Called by: partials/shared/split_panel.html
 * Depends on: Alpine.js
 */
Alpine.data('splitPanel', (panelId, defaultPct) => ({
    leftWidth: parseInt(localStorage.getItem('avail_split_' + panelId) || defaultPct),
    _resizing: false,
    _startX: 0,
    _startWidth: 0,

    // Shared resize math for both mouse and touch drags: clamp leftWidth to 20–70%
    // based on the pointer's distance from the drag start.
    _applyDrag(clientX) {
        if (!this._resizing) return;
        const container = document.getElementById('split-' + panelId);
        if (!container) return;
        const dx = clientX - this._startX;
        const newPct = this._startWidth + (dx / container.offsetWidth) * 100;
        this.leftWidth = Math.max(20, Math.min(70, Math.round(newPct)));
    },

    startResize(e) {
        this._resizing = true;
        this._startX = e.clientX;
        this._startWidth = this.leftWidth;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        const onMove = (ev) => this._applyDrag(ev.clientX);

        const onUp = () => {
            this._resizing = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            localStorage.setItem('avail_split_' + panelId, this.leftWidth);
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    },

    startTouchResize(e) {
        const touch = e.touches[0];
        this._resizing = true;
        this._startX = touch.clientX;
        this._startWidth = this.leftWidth;

        const onTouchMove = (ev) => this._applyDrag(ev.touches[0].clientX);

        const onTouchEnd = () => {
            this._resizing = false;
            localStorage.setItem('avail_split_' + panelId, this.leftWidth);
            document.removeEventListener('touchmove', onTouchMove);
            document.removeEventListener('touchend', onTouchEnd);
        };

        document.addEventListener('touchmove', onTouchMove);
        document.addEventListener('touchend', onTouchEnd);
    }
}));

/**
 * resizableModal — global modal wrapper behavior: open/close state plus, on
 * desktop, drag-to-move and drag-to-resize (4 edges + 4 corners) with the
 * chosen size/position remembered per size-bucket.
 *
 * Bound to the single modal wrapper in htmx/base.html. Every modal loads into
 * #modal-content inside this one panel, so geometry is owned here ONCE and
 * survives HTMX content swaps (the panel persists; only #modal-content's
 * innerHTML changes). Mirrors splitPanel's raw-localStorage idiom (per-drag
 * document listeners, no permanent global handler) but uses Pointer Events +
 * setPointerCapture so an embedded iframe can't swallow drag events mid-resize.
 *
 * Desktop only (>=1024px). Below that, isDesktop is false, panelStyle() returns
 * '' and the CSS handles responsive centering / the mobile bottom-sheet layout.
 *
 * Persistence: localStorage 'avail_modal_geom' -> { lg:{w,h,l,t}, wide:{...} },
 * keyed by the two existing size buckets; clamped to the live viewport on
 * restore. Double-clicking any handle or the drag-bar resets the current bucket.
 *
 * Called by: app/templates/htmx/base.html (x-data="resizableModal()").
 * Depends on: Alpine.js, ./modal_geometry.js.
 */
const MODAL_GEOM_KEY = 'avail_modal_geom';
const MODAL_DESKTOP_MQ = '(min-width: 1024px)';
// Saved panel sizes below this are junk (a bar, not a modal) — ignore on restore.
const MODAL_MIN_SAVED_W = 320;
const MODAL_MIN_SAVED_H = 240;

// Geometry memory is keyed per modal (numeric path segments normalized), so a size
// the user gave one modal never cramps a different one that shares the host.
function modalGeomBucketKey(url) {
    if (!url) return '';
    return url.split('?')[0].replace(/\/\d+(?=\/|$)/g, '/:id');
}

Alpine.data('resizableModal', () => ({
    open: false,
    wide: false,
    custom: false,            // true once the user has dragged/resized this bucket
    width: 0, height: 0, left: 0, top: 0,
    isDesktop: window.matchMedia(MODAL_DESKTOP_MQ).matches,
    bucketKey: '',            // per-modal geometry identity, from the opened url
    _drag: null,
    _mq: null,
    _onMQ: null,
    _onResize: null,
    _boundMove: null,
    _boundUp: null,
    _boundCancel: null,

    get bucket() {
        const size = this.wide ? 'wide' : 'lg';
        return this.bucketKey ? `${size}:${this.bucketKey}` : size;
    },

    init() {
        this._mq = window.matchMedia(MODAL_DESKTOP_MQ);
        this._onMQ = (e) => {
            this.isDesktop = e.matches;
            if (!e.matches) this.custom = false;  // drop floating geometry on shrink to mobile
        };
        this._mq.addEventListener('change', this._onMQ);
        // Re-clamp a custom (floating) panel when the window itself shrinks, so a panel
        // sized/positioned on a larger viewport can't end up partly or fully off-screen
        // while still desktop-width. _restore() only clamps on open; this covers live resize.
        this._onResize = () => {
            if (!this.custom || !this.isDesktop) return;
            const g = clampToViewport(
                { w: this.width, h: this.height, l: this.left, t: this.top },
                window.innerWidth,
                window.innerHeight,
            );
            this.width = g.w; this.height = g.h; this.left = g.l; this.top = g.t;
        };
        window.addEventListener('resize', this._onResize);
    },

    destroy() {
        if (this._mq && this._onMQ) this._mq.removeEventListener('change', this._onMQ);
        if (this._onResize) window.removeEventListener('resize', this._onResize);
        this._teardownDrag();
    },

    // Called from @open-modal — preserves the existing {url, wide} dispatch contract.
    onOpen(detail) {
        this.wide = !!(detail && detail.wide);
        this.bucketKey = modalGeomBucketKey(detail && detail.url);
        this.open = true;
        this.isDesktop = this._mq ? this._mq.matches : window.matchMedia(MODAL_DESKTOP_MQ).matches;
        this._restore();
        // Drop the previous modal's DOM so the panel never flashes stale content
        // while the new form is in flight (the :empty min-height keeps the panel
        // from collapsing to a grip+close sliver meanwhile).
        const content = document.getElementById('modal-content');
        if (content) content.replaceChildren();
        if (detail && detail.url) {
            // indicator: toggles #modal-loading's htmx-request class for the spinner.
            Promise.resolve(
                htmx.ajax('GET', detail.url, {
                    target: '#modal-content',
                    swap: 'innerHTML',
                    indicator: '#modal-loading',
                }),
            ).then(() => {
                // Land the user in the form: focus its first field unless the loaded
                // content already claimed focus (or we're on mobile, where focusing
                // would pop the keyboard).
                if (!this.isDesktop || !content || content.contains(document.activeElement)) return;
                const field = content.querySelector(
                    'input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled])',
                );
                if (field) field.focus();
            });
        }
    },

    onClose() {
        this.open = false;  // keep wide + geometry; the next open re-reads the bucket
    },

    // ── Persistence ──────────────────────────────────────────
    _readAll() {
        try {
            return JSON.parse(localStorage.getItem(MODAL_GEOM_KEY) || '{}');
        } catch {
            return {};
        }
    },

    _restore() {
        if (!this.isDesktop) {
            this.custom = false;
            return;
        }
        const saved = this._readAll()[this.bucket];
        if (saved && saved.w >= MODAL_MIN_SAVED_W && saved.h >= MODAL_MIN_SAVED_H) {
            const g = clampToViewport(saved, window.innerWidth, window.innerHeight);
            this.width = g.w; this.height = g.h; this.left = g.l; this.top = g.t;
            this.custom = true;
        } else {
            this.custom = false;
        }
    },

    _persist() {
        const all = this._readAll();
        all[this.bucket] = { w: this.width, h: this.height, l: this.left, t: this.top };
        localStorage.setItem(MODAL_GEOM_KEY, JSON.stringify(all));
    },

    // Seed numeric geometry from the panel's current rendered box, so the first
    // drag continues from exactly where the centered layout placed it (no jump).
    _seed() {
        const r = this.$refs.panel.getBoundingClientRect();
        this.width = r.width; this.height = r.height; this.left = r.left; this.top = r.top;
        this.custom = true;
    },

    // ── Drag lifecycle (pointer events, bound only for the drag's duration) ──
    startMove(e) {
        if (!this.isDesktop || e.button !== 0) return;
        if (!this.custom) this._seed();
        this._begin(e, 'move', '');
    },

    startResize(e, edge) {
        if (!this.isDesktop || e.button !== 0) return;
        if (!this.custom) this._seed();
        this._begin(e, 'resize', edge);
    },

    _begin(e, mode, edge) {
        e.preventDefault();
        this._drag = {
            mode, edge,
            sx: e.clientX, sy: e.clientY,
            start: { w: this.width, h: this.height, l: this.left, t: this.top },
            pid: e.pointerId, target: e.target,
        };
        if (e.target.setPointerCapture) {
            try { e.target.setPointerCapture(e.pointerId); } catch { /* capture unsupported */ }
        }
        document.body.style.userSelect = 'none';
        this._boundMove = (ev) => this._onMove(ev);
        this._boundUp = () => this._onUp();
        this._boundCancel = () => this._onUp();
        document.addEventListener('pointermove', this._boundMove);
        document.addEventListener('pointerup', this._boundUp);
        // pointercancel (touch interrupted, capture lost, context menu, etc.) fires INSTEAD
        // of pointerup — without this the move/up listeners and user-select:none would leak.
        document.addEventListener('pointercancel', this._boundCancel);
    },

    _onMove(e) {
        const d = this._drag;
        if (!d) return;
        const dx = e.clientX - d.sx;
        const dy = e.clientY - d.sy;
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const g = d.mode === 'move'
            ? moveGeometry(d.start, dx, dy, vw, vh)
            : resizeGeometry(d.start, d.edge, dx, dy, vw, vh);
        this.width = g.w; this.height = g.h; this.left = g.l; this.top = g.t;
    },

    _onUp() {
        const d = this._drag;
        if (!d) return;
        if (d.target && d.target.releasePointerCapture) {
            try { d.target.releasePointerCapture(d.pid); } catch { /* already released */ }
        }
        this._teardownDrag();
        this._persist();
    },

    _teardownDrag() {
        if (this._boundMove) document.removeEventListener('pointermove', this._boundMove);
        if (this._boundUp) document.removeEventListener('pointerup', this._boundUp);
        if (this._boundCancel) document.removeEventListener('pointercancel', this._boundCancel);
        this._boundMove = null;
        this._boundUp = null;
        this._boundCancel = null;
        this._drag = null;
        document.body.style.userSelect = '';
    },

    // Double-click any handle / the drag-bar → forget this bucket, re-center.
    reset() {
        const all = this._readAll();
        delete all[this.bucket];
        localStorage.setItem(MODAL_GEOM_KEY, JSON.stringify(all));
        this.custom = false;
    },

    // Inline style for the panel: an explicit fixed box when the user has a custom
    // size on desktop, otherwise '' so the centered/responsive CSS layout wins.
    panelStyle() {
        if (!this.custom || !this.isDesktop) return '';
        return 'position:fixed;'
            + 'left:' + this.left + 'px;'
            + 'top:' + this.top + 'px;'
            + 'width:' + this.width + 'px;'
            + 'height:' + this.height + 'px;'
            + 'max-width:none;max-height:none;margin:0;';
    },
}));

/**
 * contactsView — Alpine component for the CRM account Contacts surface
 * (contacts_tab.html). Owns the people-search (`q`) + site filter (`siteFilter`)
 * and filters the rendered contact rows CLIENT-SIDE by toggling a `hidden` class
 * — no round-trip. The controls live OUTSIDE the #contacts-tab-list swap target,
 * so a CRUD re-render replaces only the rows; re-applies on htmx:afterSettle.
 */
Alpine.data('contactsView', () => ({
  q: '',
  siteFilter: '',
  // Label state for the Expand/Collapse-all toggle. The actual per-row `open` state
  // lives on each <tbody data-contact-row>; the toggle drives them via window events.
  allOpen: false,
  init() {
    // Pre-select site filter when the tab was opened via a "View N contacts →" link.
    const initialSite = this.$root.getAttribute('data-initial-site');
    if (initialSite) this.siteFilter = initialSite;
    this.apply();
    // Re-filter after a CRUD swap replaces the inner #contacts-tab-list rows. The
    // fresh rows render collapsed, so reset the toggle label to match.
    this._onSettle = () => { this.allOpen = false; this.apply(); };
    this.$root.addEventListener('htmx:afterSettle', this._onSettle);
  },
  destroy() {
    if (this._onSettle) this.$root.removeEventListener('htmx:afterSettle', this._onSettle);
  },
  apply() {
    this.$nextTick(() => {
      const root = this.$root;
      const needle = this.q.trim().toLowerCase();
      const site = this.siteFilter;
      let visible = 0;
      root.querySelectorAll('[data-contact-row]').forEach((row) => {
        const nameMatch = !needle || (row.getAttribute('data-contact-search') || '').includes(needle);
        const siteMatch = !site || row.getAttribute('data-site-id') === site;
        const show = nameMatch && siteMatch;
        row.classList.toggle('hidden', !show);
        if (show) visible += 1;
      });
      // Hide a whole site section when none of its rows survive the filter.
      root.querySelectorAll('[data-contacts-section]').forEach((sec) => {
        const anyVisible = sec.querySelector('[data-contact-row]:not(.hidden)');
        sec.classList.toggle('hidden', !anyVisible);
      });
      const emptyHint = root.querySelector('[data-contacts-empty]');
      if (emptyHint) {
        const hasRows = root.querySelector('[data-contact-row]');
        emptyHint.classList.toggle('hidden', visible > 0 || !hasRows);
      }
    });
  },
}));

// Data Ops dedup multi-select — one instance per dedup section (vendor / company).
// Selection unit is a PAIR token "<keeperId>-<loserId>" (keeper-first so bulk-merge
// keeps the suggested side). Uses the reassign-the-Set idiom to trigger Alpine
// reactivity. Lives inside #settings-content, which the htmx:afterSwap
// handler re-initTrees, so it rebinds cleanly after each merge/delete re-render.
Alpine.data('dedupSelect', () => ({
  selected: new Set(),
  toggle(token, checked) {
    if (checked) { this.selected.add(token); } else { this.selected.delete(token); }
    this.selected = new Set(this.selected);
  },
  toggleAll(checked, tokens) {
    if (checked) { tokens.forEach(t => this.selected.add(t)); } else { this.selected.clear(); }
    this.selected = new Set(this.selected);
  },
  clear() { this.selected = new Set(); },
  has(token) { return this.selected.has(token); },
  get count() { return this.selected.size; },
  // Comma-joined "a-b,c-d" string the bulk endpoint parses.
  get pairsStr() { return [...this.selected].join(','); },
  // Hide the dismissed rows immediately (client-only); the form re-renders the list.
  hideSelected() {
    this.selected.forEach(token => {
      const row = this.$root.querySelector('[data-pair="' + token + '"]');
      if (row) { row.style.display = 'none'; }
    });
  },
}));
