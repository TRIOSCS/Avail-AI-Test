/**
 * Alpine.js component for Requisitions 2 All page.
 *
 * Manages transient UI state only. All data fetching is handled by HTMX.
 * All business logic is server-side.
 *
 * Called by: app/templates/requisitions2/page.html
 * Depends on: Alpine.js 3.x (loaded via CDN)
 */

document.addEventListener('alpine:init', () => {
  Alpine.data('rq2Page', () => ({
    selectedIds: new Set(),
    toasts: [],

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
