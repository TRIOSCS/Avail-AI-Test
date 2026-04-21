/**
 * Alpine.js components for Requisitions 2 split-screen workspace.
 *
 * Manages transient UI state only. All data fetching is handled by HTMX.
 * All business logic is server-side.
 *
 * Called by: app/templates/requisitions2/page.html
 * Depends on: Alpine.js 3.x (loaded via CDN)
 *
 * splitPanel mirrors the component in app/static/htmx_app.js, duplicated here
 * because page.html is a standalone template that does not load the Vite
 * bundle. Keep behavior in sync with htmx_app.js:splitPanel.
 */

document.addEventListener('alpine:init', () => {
  Alpine.data('splitPanel', (panelId, defaultPct) => ({
    leftWidth: parseInt(localStorage.getItem('avail_split_' + panelId) || defaultPct),
    _resizing: false,
    _startX: 0,
    _startWidth: 0,

    startResize(e) {
      this._resizing = true;
      this._startX = e.clientX;
      this._startWidth = this.leftWidth;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMove = (ev) => {
        if (!this._resizing) return;
        const container = document.getElementById('split-' + panelId);
        if (!container) return;
        const dx = ev.clientX - this._startX;
        const containerW = container.offsetWidth;
        const newPct = this._startWidth + (dx / containerW) * 100;
        this.leftWidth = Math.max(20, Math.min(70, Math.round(newPct)));
      };

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

      const onTouchMove = (ev) => {
        if (!this._resizing) return;
        const t = ev.touches[0];
        const container = document.getElementById('split-' + panelId);
        if (!container) return;
        const dx = t.clientX - this._startX;
        const containerW = container.offsetWidth;
        const newPct = this._startWidth + (dx / containerW) * 100;
        this.leftWidth = Math.max(20, Math.min(70, Math.round(newPct)));
      };

      const onTouchEnd = () => {
        this._resizing = false;
        localStorage.setItem('avail_split_' + panelId, this.leftWidth);
        document.removeEventListener('touchmove', onTouchMove);
        document.removeEventListener('touchend', onTouchEnd);
      };

      document.addEventListener('touchmove', onTouchMove);
      document.addEventListener('touchend', onTouchEnd);
    },
  }));

  Alpine.data('rq2Page', () => ({
    selectedIds: new Set(),
    selectedReqId: null,
    toasts: [],

    selectReq(id) {
      this.selectedReqId = id;
    },

    toggleSelection(id, checked) {
      if (checked) {
        this.selectedIds.add(id);
      } else {
        this.selectedIds.delete(id);
      }
      this.selectedIds = new Set(this.selectedIds);
    },

    toggleAll(checked, ids) {
      if (checked) {
        ids.forEach(id => this.selectedIds.add(id));
      } else {
        this.selectedIds.clear();
      }
      this.selectedIds = new Set(this.selectedIds);
    },

    clearSelection() {
      this.selectedIds = new Set();
    },

    onTableSwap(event) {
      if (event.detail && event.detail.target && event.detail.target.id === 'rq2-table') {
        this.selectedIds = new Set();
      }
    },

    showToast(event) {
      const msg = event.detail && event.detail.message ? event.detail.message : 'Done';
      this.toasts.push(msg);
      setTimeout(() => {
        const idx = this.toasts.indexOf(msg);
        if (idx > -1) this.toasts.splice(idx, 1);
      }, 3000);
    },

    getSelectedIdsString() {
      return [...this.selectedIds].join(',');
    }
  }));

  /**
   * resizableTable — User-resizable table columns via <colgroup> widths.
   * Drag right-edge handle on <th>, persist to localStorage, restore across HTMX swaps.
   *
   * Template contract:
   *   <div id="..." x-data="resizableTable('<key>', {col1:N, col2:N})">
   *     <table class="resizable-cols">
   *       <colgroup><col :style="colStyle('col1')">...</colgroup>
   *       <thead><tr>
   *         <th class="resizable">Col1
   *           <span class="col-resize-handle"
   *                 @mousedown="startColResize($event,'col1')"
   *                 @dblclick="autoFitCol('col1')"></span>
   *         </th>
   *         <th>Col2</th>  {# last col, no handle #}
   *       </tr></thead>
   *     </table>
   *   </div>
   *
   * Consolidate into app/static/htmx_app.js when page.html migrates to base.html.
   */
  Alpine.data('resizableTable', (tableKey, defaults) => ({
    widths: {},
    _resizing: null,
    _storageKey: 'avail_table_cols_' + tableKey,
    _defaults: defaults,

    init() {
      const saved = JSON.parse(localStorage.getItem(this._storageKey) || '{}');
      this.widths = { ...this._defaults, ...saved };
      this.$el.addEventListener('htmx:afterSwap', () => {
        this.widths = { ...this.widths };
      });
    },

    colStyle(key) {
      const w = this.widths[key];
      return w ? `width:${w}px;min-width:${w}px` : '';
    },

    startColResize(e, key) {
      e.preventDefault();
      e.stopPropagation();
      const th = e.target.closest('th');
      const startWidth = this.widths[key] || (th ? th.offsetWidth : 100);
      this._resizing = { key, startX: e.clientX, startWidth };
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMove = (ev) => {
        if (!this._resizing) return;
        const dx = ev.clientX - this._resizing.startX;
        this.widths[this._resizing.key] = Math.max(40, this._resizing.startWidth + dx);
      };
      const onUp = () => {
        this._resizing = null;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        localStorage.setItem(this._storageKey, JSON.stringify(this.widths));
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    },

    autoFitCol(key) {
      delete this.widths[key];
      this.widths = { ...this._defaults, ...this.widths };
      localStorage.setItem(this._storageKey, JSON.stringify(this.widths));
    },

    resetAll() {
      this.widths = { ...this._defaults };
      localStorage.removeItem(this._storageKey);
    },
  }));

  /**
   * x-truncate-tip — Hover tooltip that appears only when the element is
   * visually clipped (scrollWidth > clientWidth). Tooltip is appended to
   * document.body so parent overflow:hidden cannot clip it.
   *
   * Usage: <span class="truncate" x-truncate-tip>{{ value }}</span>
   */
  Alpine.directive('truncate-tip', (el) => {
    let tip = null;

    const show = () => {
      if (el.scrollWidth <= el.clientWidth) return;
      const text = el.textContent.trim();
      if (!text) return;

      tip = document.createElement('div');
      tip.className = 'truncate-tip';
      tip.textContent = text;
      document.body.appendChild(tip);

      const r = el.getBoundingClientRect();
      const tr = tip.getBoundingClientRect();
      let top = r.top - tr.height - 6;
      if (top < 4) top = r.bottom + 6;
      let left = r.left + (r.width - tr.width) / 2;
      left = Math.max(4, Math.min(left, window.innerWidth - tr.width - 4));
      tip.style.top = top + 'px';
      tip.style.left = left + 'px';
      requestAnimationFrame(() => tip && tip.classList.add('visible'));
    };

    const hide = () => {
      if (tip) {
        tip.remove();
        tip = null;
      }
    };

    el.addEventListener('mouseenter', show);
    el.addEventListener('mouseleave', hide);
    el.addEventListener('focusout', hide);
  });
});
