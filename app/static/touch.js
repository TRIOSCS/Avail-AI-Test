/* touch.js — Touch gesture handler for AvailAI mobile
 * Provides SwipeHandler class for sidebar, drawers, and bottom-sheet dismissal.
 * Also provides the mobile Alerts Feed (loadAlertsFeed, _renderAlertsFeed,
 * _markAlertRead) and swipe-to-dismiss on alert cards.
 * Loaded via Vite alongside app.js/crm.js.
 * Only activates on touch devices (passive listeners, no desktop overhead).
 */

import { apiFetch, esc } from 'app';

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

// ── Mobile Alerts Feed ───────────────────────────────────────────────

/** Cached alerts data from last fetch */
var _alertsData = [];

/** Time-ago formatter (local, avoids dependency on app.js _timeAgo being on window at load time) */
function _alertTimeAgo(iso) {
    if (!iso) return '';
    var s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}

/** Map event_type to an icon SVG string */
function _alertIcon(eventType) {
    switch (eventType) {
        case 'diagnosed':
            return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#3b82f6" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
        case 'prompt_ready':
            return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#8b5cf6" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
        case 'fixed':
            return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#22c55e" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
        case 'failed':
            return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#ef4444" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';
        case 'escalated':
            return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#f59e0b" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
        default:
            return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#64748b" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';
    }
}

/**
 * loadAlertsFeed — Fetch notifications from /api/notifications and render.
 * Called by mobileTabNav('alerts', ...) in app.js.
 */
async function loadAlertsFeed() {
    var list = document.getElementById('alertsFeedList');
    if (!list) return;
    list.innerHTML = '<div style="padding:24px;text-align:center;color:var(--fg3)">Loading alerts\u2026</div>';
    try {
        var data = await apiFetch('/api/notifications?limit=200');
        _alertsData = data.items || [];
        _renderAlertsFeed();
        _updateAlertsBadge(data.unread_count || 0);
    } catch (err) {
        list.innerHTML = '<div style="padding:24px;text-align:center;color:var(--fg3)">Failed to load alerts</div>';
    }
}

/**
 * _renderAlertsFeed — Render alerts into #alertsFeedList.
 * Groups into "Needs Attention" (unread) and "Recent" (read).
 * Attaches swipe-to-dismiss handlers on each card.
 */
function _renderAlertsFeed() {
    var list = document.getElementById('alertsFeedList');
    if (!list) return;

    var unread = _alertsData.filter(function(n) { return !n.is_read; });
    var read = _alertsData.filter(function(n) { return n.is_read; });

    if (!_alertsData.length) {
        list.innerHTML = '<div style="padding:40px 24px;text-align:center;color:var(--fg3)">'
            + '<svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.5" style="margin-bottom:12px;opacity:0.4"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>'
            + '<div style="font-size:15px;font-weight:600;margin-bottom:4px">No alerts</div>'
            + '<div style="font-size:13px">You are all caught up</div>'
            + '</div>';
        return;
    }

    var html = '';

    // Mark all read button (only if there are unread)
    if (unread.length) {
        html += '<div style="display:flex;justify-content:flex-end;padding:8px 16px 0">'
            + '<button type="button" onclick="_markAllAlertsRead()" style="font-size:12px;color:var(--teal);background:none;border:none;cursor:pointer;padding:4px 8px">Mark all read</button>'
            + '</div>';
    }

    // Needs Attention section
    if (unread.length) {
        html += '<div class="m-alert-section">'
            + '<div style="padding:8px 16px 4px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--fg3)">Needs Attention (' + unread.length + ')</div>';
        unread.forEach(function(n) { html += _renderAlertCard(n); });
        html += '</div>';
    }

    // Recent section
    if (read.length) {
        html += '<div class="m-alert-section">'
            + '<div style="padding:8px 16px 4px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--fg3)">Recent</div>';
        read.forEach(function(n) { html += _renderAlertCard(n); });
        html += '</div>';
    }

    list.innerHTML = html;

    // Attach swipe-to-dismiss on each card
    _attachAlertSwipeHandlers();
}

/** Render a single alert card — all user content escaped via esc() */
function _renderAlertCard(n) {
    var isUnread = !n.is_read;
    var borderStyle = isUnread ? 'border-left:3px solid var(--teal)' : 'border-left:3px solid transparent';
    var titleWeight = isUnread ? 'font-weight:600' : 'font-weight:400';
    return '<div class="m-alert-card" data-notif-id="' + n.id + '" style="' + borderStyle + ';padding:12px 16px;background:var(--card1);margin:4px 8px;border-radius:8px;display:flex;gap:10px;align-items:flex-start;cursor:pointer;transition:transform 0.2s,opacity 0.2s;position:relative;overflow:hidden" onclick="_onAlertCardTap(' + n.id + ')">'
        + '<div style="flex-shrink:0;margin-top:2px">' + _alertIcon(n.event_type) + '</div>'
        + '<div style="flex:1;min-width:0">'
        + '<div style="font-size:14px;' + titleWeight + ';color:var(--fg1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(n.title) + '</div>'
        + (n.body ? '<div style="font-size:12px;color:var(--fg2);margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">' + esc(n.body) + '</div>' : '')
        + '<div style="font-size:11px;color:var(--fg3);margin-top:4px">' + _alertTimeAgo(n.created_at) + '</div>'
        + '</div>'
        + '</div>';
}

/**
 * _onAlertCardTap — Handle tap on an alert card: mark as read and navigate.
 */
function _onAlertCardTap(notifId) {
    var notif = _alertsData.find(function(n) { return n.id === notifId; });
    if (!notif) return;

    // Mark as read
    if (!notif.is_read) {
        _markAlertRead(notifId);
    }

    // Navigate to the relevant ticket if present
    if (notif.ticket_id && typeof window.showView === 'function') {
        window.showView('view-settings');
        if (typeof window.sidebarNav === 'function') {
            window.sidebarNav('settings', document.getElementById('navSettings'));
        }
    }
}

/**
 * _markAlertRead — POST to mark a notification as read, update card visually.
 */
async function _markAlertRead(notifId) {
    try {
        await apiFetch('/api/notifications/' + notifId + '/read', { method: 'POST' });
    } catch (e) { /* ignore */ }

    // Update local data
    var notif = _alertsData.find(function(n) { return n.id === notifId; });
    if (notif) notif.is_read = true;

    // Update card visually
    var card = document.querySelector('.m-alert-card[data-notif-id="' + notifId + '"]');
    if (card) {
        card.style.borderLeftColor = 'transparent';
        var titleEl = card.querySelector('div > div:first-child');
        if (titleEl) titleEl.style.fontWeight = '400';
    }

    // Update badge
    var unreadCount = _alertsData.filter(function(n) { return !n.is_read; }).length;
    _updateAlertsBadge(unreadCount);
}

/**
 * _markAllAlertsRead — Mark all notifications read via API, re-render.
 */
async function _markAllAlertsRead() {
    try {
        await apiFetch('/api/notifications/read-all', { method: 'POST' });
    } catch (e) { /* ignore */ }

    // Update local data
    _alertsData.forEach(function(n) { n.is_read = true; });
    _renderAlertsFeed();
    _updateAlertsBadge(0);
}

/**
 * _updateAlertsBadge — Update the #bnBadgeAlerts badge with unread count.
 */
function _updateAlertsBadge(count) {
    var badge = document.getElementById('bnBadgeAlerts');
    if (!badge) return;
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.style.display = '';
    } else {
        badge.textContent = '';
        badge.style.display = 'none';
    }
}

/**
 * _attachAlertSwipeHandlers — Add swipe-left-to-dismiss on each .m-alert-card.
 * Uses direct touch listeners (same pattern as the rest of touch.js).
 */
function _attachAlertSwipeHandlers() {
    var cards = document.querySelectorAll('.m-alert-card');
    cards.forEach(function(card) {
        var startX = 0;
        var startY = 0;
        var tracking = false;

        card.addEventListener('touchstart', function(e) {
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
            tracking = true;
        }, { passive: true });

        card.addEventListener('touchmove', function(e) {
            if (!tracking) return;
            var dx = e.touches[0].clientX - startX;
            var dy = e.touches[0].clientY - startY;

            // Only handle horizontal swipes
            if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
            if (Math.abs(dy) > Math.abs(dx)) { tracking = false; return; }

            // Only handle left swipes
            if (dx < 0) {
                e.preventDefault();
                card.style.transform = 'translateX(' + dx + 'px)';
                card.style.opacity = String(Math.max(0, 1 + dx / 200));
            }
        }, { passive: false });

        card.addEventListener('touchend', function(e) {
            if (!tracking) return;
            tracking = false;
            var dx = e.changedTouches[0].clientX - startX;

            if (dx < -100) {
                // Dismiss: animate out then remove
                card.style.transition = 'transform 0.2s ease-out, opacity 0.2s ease-out';
                card.style.transform = 'translateX(-110%)';
                card.style.opacity = '0';

                var notifId = parseInt(card.getAttribute('data-notif-id'), 10);
                if (notifId) _markAlertRead(notifId);

                setTimeout(function() {
                    card.style.height = card.offsetHeight + 'px';
                    card.style.overflow = 'hidden';
                    card.style.transition = 'height 0.15s ease-out, margin 0.15s ease-out, padding 0.15s ease-out';
                    card.style.height = '0';
                    card.style.margin = '0 8px';
                    card.style.padding = '0 16px';
                    card.style.borderWidth = '0';
                    setTimeout(function() {
                        card.remove();
                        // Remove from local data
                        _alertsData = _alertsData.filter(function(n) { return n.id !== notifId; });
                        // Check if sections are now empty and re-render if needed
                        var remaining = document.querySelectorAll('.m-alert-card');
                        if (!remaining.length) _renderAlertsFeed();
                    }, 150);
                }, 200);
            } else {
                // Snap back
                card.style.transition = 'transform 0.15s ease-out, opacity 0.15s ease-out';
                card.style.transform = '';
                card.style.opacity = '';
                setTimeout(function() { card.style.transition = ''; }, 150);
            }
        }, { passive: true });
    });
}

// ── Pull-to-Refresh ──────────────────────────────────────────────────

/** Default no-op; app.js mobileTabNav overrides per active tab */
window._mobileRefreshCallback = function() {};

/**
 * _initPullToRefresh — Attach pull-to-refresh gesture on .main-scroll.
 * When the user pulls down from scrollTop===0 past 80px, fires
 * window._mobileRefreshCallback() and shows a transient indicator.
 */
function _initPullToRefresh() {
    var scroller = document.querySelector('.main-scroll');
    if (!scroller) return;

    var indicator = null;
    var startX = 0;
    var startY = 0;
    var pulling = false;
    var refreshing = false;
    var pullDist = 0;
    var dirLocked = false;
    var THRESHOLD = 80;

    function _ensureIndicator() {
        if (indicator) return indicator;
        indicator = document.createElement('div');
        indicator.className = 'm-ptr-indicator';
        indicator.textContent = '\u2193 Pull to refresh';
        scroller.parentNode.insertBefore(indicator, scroller);
        return indicator;
    }

    scroller.addEventListener('touchstart', function(e) {
        if (refreshing) return;
        if (scroller.scrollTop > 0) return;
        startX = e.touches[0].clientX;
        startY = e.touches[0].clientY;
        pulling = true;
        pullDist = 0;
        dirLocked = false;
    }, { passive: true });

    scroller.addEventListener('touchmove', function(e) {
        if (!pulling || refreshing) return;
        var dy = e.touches[0].clientY - startY;

        // If scrolled down or pulling upward, cancel
        if (scroller.scrollTop > 0 || dy <= 0) {
            pulling = false;
            if (indicator) {
                indicator.style.height = '0';
                indicator.style.opacity = '0';
            }
            return;
        }

        // Lock direction on first significant movement
        if (!dirLocked) {
            var dx = Math.abs(e.touches[0].clientX - startX);
            if (dx > 10 && dx > dy) {
                // Horizontal swipe — cancel pull
                pulling = false;
                return;
            }
            if (dy > 10) dirLocked = true;
        }
        pullDist = Math.min(dy * 0.5, 120); // dampen

        e.preventDefault();

        var ind = _ensureIndicator();
        ind.style.height = pullDist + 'px';
        ind.style.opacity = pullDist > 20 ? '1' : '0';
        ind.textContent = pullDist >= THRESHOLD ? '\u2191 Release to refresh' : '\u2193 Pull to refresh';
    }, { passive: false });

    scroller.addEventListener('touchend', function() {
        if (!pulling || refreshing) return;
        pulling = false;

        if (pullDist >= THRESHOLD) {
            // Trigger refresh
            refreshing = true;
            var ind = _ensureIndicator();
            ind.style.height = '44px';
            ind.style.opacity = '1';
            ind.textContent = 'Refreshing\u2026';

            // Call the callback (may be async)
            try {
                var result = window._mobileRefreshCallback();
                if (result && typeof result.then === 'function') {
                    result.then(_hideIndicator).catch(_hideIndicator);
                } else {
                    // Not a promise — hide after a short delay
                    setTimeout(_hideIndicator, 600);
                }
            } catch (err) {
                _hideIndicator();
            }
        } else {
            // Snap back
            if (indicator) {
                indicator.style.height = '0';
                indicator.style.opacity = '0';
            }
        }
    }, { passive: true });

    function _hideIndicator() {
        refreshing = false;
        if (indicator) {
            indicator.style.height = '0';
            indicator.style.opacity = '0';
        }
    }
}

// Initialize pull-to-refresh on mobile
document.addEventListener('DOMContentLoaded', function() {
    if ('ontouchstart' in window) _initPullToRefresh();
});

// ── Expose alert functions to window ─────────────────────────────────
window.loadAlertsFeed = loadAlertsFeed;
window._renderAlertsFeed = _renderAlertsFeed;
window._markAlertRead = _markAlertRead;
window._markAllAlertsRead = _markAllAlertsRead;
window._onAlertCardTap = _onAlertCardTap;
window._updateAlertsBadge = _updateAlertsBadge;
