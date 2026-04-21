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
});
