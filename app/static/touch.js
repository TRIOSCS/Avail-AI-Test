/* touch.js — Touch gesture handler for AvailAI mobile
 * Provides SwipeHandler class for sidebar, drawers, and bottom-sheet dismissal.
 * Loaded via Vite alongside app.js/crm.js.
 * Only activates on touch devices (passive listeners, no desktop overhead).
 */

class SwipeHandler {
    constructor(el, opts = {}) {
        this.el = el;
        this.dir = opts.direction || 'left'; // 'left','right','down'
        this.threshold = opts.threshold || 60;
        this.onSwipe = opts.onSwipe || (() => {});
        this.onMove = opts.onMove || null;
        this.onEnd = opts.onEnd || null;
        this.edgeOnly = opts.edgeOnly || false;
        this.edgeWidth = opts.edgeWidth || 20;
        this._startX = 0;
        this._startY = 0;
        this._tracking = false;
        this._bound = {
            start: this._onStart.bind(this),
            move: this._onMove.bind(this),
            end: this._onEnd.bind(this),
        };
        el.addEventListener('touchstart', this._bound.start, { passive: true });
        el.addEventListener('touchmove', this._bound.move, { passive: false });
        el.addEventListener('touchend', this._bound.end, { passive: true });
    }

    _onStart(e) {
        const t = e.touches[0];
        if (this.edgeOnly && t.clientX > this.edgeWidth) return;
        this._startX = t.clientX;
        this._startY = t.clientY;
        this._tracking = true;
    }

    _onMove(e) {
        if (!this._tracking) return;
        const t = e.touches[0];
        const dx = t.clientX - this._startX;
        const dy = t.clientY - this._startY;

        // Determine dominant axis
        if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
        const isHorizontal = Math.abs(dx) > Math.abs(dy);

        if (this.dir === 'down') {
            if (!isHorizontal && dy > 0 && this.onMove) {
                e.preventDefault();
                this.onMove(dy);
            }
        } else if (this.dir === 'left' && isHorizontal && dx < 0 && this.onMove) {
            e.preventDefault();
            this.onMove(dx);
        } else if (this.dir === 'right' && isHorizontal && dx > 0 && this.onMove) {
            e.preventDefault();
            this.onMove(dx);
        }
    }

    _onEnd(e) {
        if (!this._tracking) return;
        this._tracking = false;
        const t = e.changedTouches[0];
        const dx = t.clientX - this._startX;
        const dy = t.clientY - this._startY;

        if (this.onEnd) this.onEnd();

        if (this.dir === 'left' && dx < -this.threshold) this.onSwipe();
        else if (this.dir === 'right' && dx > this.threshold) this.onSwipe();
        else if (this.dir === 'down' && dy > this.threshold) this.onSwipe();
    }

    destroy() {
        this.el.removeEventListener('touchstart', this._bound.start);
        this.el.removeEventListener('touchmove', this._bound.move);
        this.el.removeEventListener('touchend', this._bound.end);
    }
}

// ── Initialize gestures on mobile ─────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    if (!('ontouchstart' in window)) return;

    // Sidebar: swipe-left to close when open
    const sidebar = document.getElementById('sidebar');
    if (sidebar) {
        new SwipeHandler(sidebar, {
            direction: 'left',
            threshold: 50,
            onSwipe: function() {
                if (sidebar.classList.contains('mobile-open')) {
                    if (typeof toggleMobileSidebar === 'function') toggleMobileSidebar();
                }
            },
            onMove: function(dx) {
                if (sidebar.classList.contains('mobile-open')) {
                    sidebar.style.transform = 'translateX(' + Math.min(0, dx) + 'px)';
                }
            },
            onEnd: function() {
                sidebar.style.transform = '';
            }
        });
    }

    // Edge-swipe from left to open sidebar
    new SwipeHandler(document.body, {
        direction: 'right',
        edgeOnly: true,
        edgeWidth: 20,
        threshold: 60,
        onSwipe: function() {
            if (sidebar && !sidebar.classList.contains('mobile-open')) {
                if (typeof toggleMobileSidebar === 'function') toggleMobileSidebar();
            }
        }
    });

    // Drawers: swipe-right to close
    ['custDrawer', 'vendorDrawer', 'prospectDrawer', 'suggestedDetailDrawer'].forEach(function(id) {
        const drawer = document.getElementById(id);
        if (!drawer) return;
        new SwipeHandler(drawer, {
            direction: 'right',
            threshold: 80,
            onSwipe: function() {
                if (!drawer.classList.contains('open')) return;
                var closeMap = {
                    custDrawer: 'closeCustDrawer',
                    vendorDrawer: 'closeVendorDrawer',
                    prospectDrawer: 'closeProspectDrawer',
                    suggestedDetailDrawer: 'closeSuggestedDetail'
                };
                var fn = window[closeMap[id]];
                if (typeof fn === 'function') fn();
            },
            onMove: function(dx) {
                if (drawer.classList.contains('open')) {
                    drawer.style.transform = 'translateX(' + Math.max(0, dx) + 'px)';
                }
            },
            onEnd: function() {
                drawer.style.transform = '';
            }
        });
    });

    // Mobile full-screen drill-down: swipe-down on header to close
    document.addEventListener('touchstart', function(e) {
        var header = e.target.closest && e.target.closest('.m-fullscreen .m-detail-header');
        if (!header) return;
        var startY = e.touches[0].clientY;
        var overlay = header.closest('.m-fullscreen');
        if (!overlay) return;

        function onMove(ev) {
            var dy = ev.touches[0].clientY - startY;
            if (dy > 0) {
                overlay.style.transform = 'translateY(' + dy + 'px)';
                ev.preventDefault();
            }
        }
        function onEnd(ev) {
            var dy = ev.changedTouches[0].clientY - startY;
            document.removeEventListener('touchmove', onMove);
            document.removeEventListener('touchend', onEnd);
            if (dy > 100) {
                if (typeof _closeMobileDrillDown === 'function') _closeMobileDrillDown();
            } else {
                overlay.style.transform = '';
            }
        }
        document.addEventListener('touchmove', onMove, { passive: false });
        document.addEventListener('touchend', onEnd, { passive: true });
    }, { passive: true });
});
