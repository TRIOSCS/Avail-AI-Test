# Trouble Ticket Mass Fix — 15 Open Tickets

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 15 remaining open trouble tickets across data, XSS, UI, API, and performance categories.

**Architecture:** Bug fixes only — no new models, migrations, or endpoints needed. Data cleanup via one-time SQL. Frontend fixes in app.js/crm.js/tickets.js. Backend fixes in proactive_service.py and knowledge.py.

**Tech Stack:** FastAPI, SQLAlchemy, vanilla JS, PostgreSQL

---

### Task 1: Data Cleanup — Epoch-zero Dates (#866, #880)

**Files:**
- Modify: `app/static/app.js:599-602` (fmtDate guard)
- SQL: one-time cleanup in production DB

**Step 1: Add epoch-zero guard to fmtDate()**

In `app/static/app.js`, modify `fmtDate()` to treat dates before 1980 as invalid:

```javascript
export function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime()) || d.getFullYear() < 1980) return '';
    return d.toLocaleDateString('en-US', { year: 'numeric', month: 'numeric', day: 'numeric' });
}
```

**Step 2: Clean up bad deadline values in DB**

```sql
UPDATE requisitions SET deadline = NULL WHERE deadline IS NOT NULL AND deadline NOT IN ('ASAP') AND deadline < '1980-01-01';
UPDATE requisitions SET deadline = NULL WHERE deadline IS NOT NULL AND deadline NOT IN ('ASAP') AND deadline > '2099-01-01';
```

**Step 3: Run tests, commit**

---

### Task 2: XSS Hardening + Test Data Cleanup (#873, #886)

**Files:**
- Modify: `app/static/crm.js` — audit innerHTML assignments
- SQL: delete test accounts from production

**Step 1: Audit and fix innerHTML in crm.js**

Search for `innerHTML =` patterns where user-controlled data isn't wrapped in `esc()`. The codebase already uses `esc()` extensively — verify all dynamic content in innerHTML uses it.

**Step 2: Delete XSS test accounts from production**

```sql
DELETE FROM companies WHERE name LIKE '%<script%' OR name LIKE '%<img%' OR name LIKE '%onerror%';
DELETE FROM companies WHERE name IN ('1', 'test', 'Test Company XSS');
```

**Step 3: Run tests, commit**

---

### Task 3: Proactive Scorecard Profit Fix (#875)

**Files:**
- Modify: `app/services/proactive_service.py:686-694` (_cap_outlier)
- Test: `tests/test_proactive_service.py`

**Step 1: Fix _cap_outlier to handle negatives and exclude whole offers**

Replace the function to clamp absolute outliers and also handle negatives:

```python
def _cap_outlier(value: float, cap: float = 500_000) -> float:
    """Cap unrealistic financial values to prevent test-data pollution."""
    if abs(value) > cap:
        return 0.0
    return value
```

**Step 2: Run tests, commit**

---

### Task 4: Notification Badge Mismatch (#870)

**Files:**
- Modify: `app/static/app.js:13072-13078` (loadNotifications)
- Modify: `app/static/app.js:13145-13157` (loadNotificationBadge)

**Step 1: Fix badge to only count sales notifications (matching the panel)**

The badge counts sales + system notifications, but the panel only shows sales. Either: (a) add system notifications to the panel, or (b) make badge match panel. Option (b) is simpler:

```javascript
async function loadNotificationBadge() {
    const badge = document.getElementById('notifBadge');
    if (!badge) return;
    try {
        const salesData = await apiFetch('/api/sales/notifications/count').catch(() => ({count: 0}));
        const count = salesData.count || 0;
        badge.textContent = count;
        badge.style.display = count > 0 ? 'flex' : 'none';
    } catch { badge.style.display = 'none'; }
}
```

Better approach: include system notifications in the panel too. Add a section header separating sales vs system notifications.

**Step 2: Run tests, commit**

---

### Task 5: Tickets Page Loading Fix (#881)

**Files:**
- Modify: `app/static/tickets.js` — route guard / hash routing

**Step 1: Ensure tickets tab initializes on settings hash route**

The tickets module loads when `switchSettingsTab('tickets')` is called. If the page loads directly on `#settings` with tickets tab active, the initial call may be missed. Add a DOMContentLoaded guard:

```javascript
// In tickets.js init section, add:
if (document.readyState !== 'loading') {
    var container = document.getElementById('settings-tickets');
    if (container && container.offsetParent) showTickets();
}
```

**Step 2: Run tests, commit**

---

### Task 6: Sidebar Nav Buttons Fix (#890)

**Files:**
- Modify: `app/static/app.js` or `app/static/crm.js` — sidebar event handling

**Step 1: Investigate and fix click event propagation**

The RELATIONSHIPS section expanding may create an overlay or z-index issue blocking sidebar clicks. Fix by ensuring sidebar nav buttons use `pointer-events: auto` and have proper z-index, or stop event propagation from relationship section.

**Step 2: Run tests, commit**

---

### Task 7: Excessive Tickets Page Polling (#887)

**Files:**
- Modify: `app/static/tickets.js:309-314`

**Step 1: Increase refresh interval and add proper cleanup**

```javascript
// Change 10s to 30s, and clear on any tab switch
if (_adminRefreshTimer) clearInterval(_adminRefreshTimer);
_adminRefreshTimer = setInterval(function() {
    if (!container.offsetParent) { clearInterval(_adminRefreshTimer); _adminRefreshTimer = null; return; }
    renderAdminDashboard(container);
}, 30000);
```

**Step 2: Run tests, commit**

---

### Task 8: Excessive Alerts Polling (#889)

**Files:**
- Modify: `app/static/app.js:1072-1074`

**Step 1: Guard against duplicate interval registration**

The health timer is created at module load with `const _healthTimer = setInterval(...)`. Since this is at module scope and only runs once, it shouldn't duplicate. The issue is that `pollApiHealth()` runs on every page init. Change to only poll on first load + interval:

```javascript
// Already has beforeunload cleanup. The 60s interval is reasonable.
// The issue is the initial pollApiHealth() call on line 1072 runs on EVERY import.
// Since app.js is a module loaded once, this should be fine.
// Increase interval from 60s to 120s to reduce load:
const _healthTimer = setInterval(pollApiHealth, 120000);
```

**Step 2: Run tests, commit**

---

### Task 9: Materials Insights 422 Error (#888)

**Files:**
- Modify: `app/routers/knowledge.py:354-367`
- Modify: frontend JS that calls this endpoint

**Step 1: Add graceful error handling for empty/invalid MPN**

The 422 is likely from the frontend sending `mpn=` (empty string) or not URL-encoding special chars. Add a guard:

```python
@sprinkles_router.get("/materials/insights")
def get_mpn_insights(
    mpn: str = Query(""),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    if not mpn or not mpn.strip():
        return {"mpn": "", "insights": [], "generated_at": None, "has_expired": False}
    entries = knowledge_service.get_cached_mpn_insights(db, mpn.strip())
    ...
```

**Step 2: Fix frontend to not call endpoint with empty MPN**

**Step 3: Run tests, commit**

---

### Task 10: Distributor QTY Field (#892)

**Files:**
- Modify: `app/connectors/element14.py:74-76`
- Modify: `app/connectors/digikey.py:95`

**Step 1: Ensure qty_available defaults to 0 instead of None for authorized distributors**

Both connectors already parse qty_available correctly. The issue may be that some API responses don't include stock data. Ensure authorized distributors show 0 instead of null when stock isn't available:

```python
# element14.py
qty = safe_int(stock_info.get("level")) if stock_info else 0

# digikey.py
qty = safe_int(prod.get("QuantityAvailable") or prod.get("quantityAvailable") or 0)
```

**Step 2: Run connector tests, commit**

---

### Task 11: Teams Connector Error State (#879)

**Files:**
- Modify: `app/services/teams.py` — connector status display

**Step 1: Make Teams connector show "not configured" instead of "error" when webhook is missing**

The health monitor correctly sets status based on API checks. But Teams isn't a sourcing connector — it's a notification channel. If the webhook URL isn't configured, it should show "not configured" rather than "error" in the settings UI.

**Step 2: Run tests, commit**

---

### Task 12: Non-Vendor Entries Cleanup (#876)

**Files:**
- SQL: one-time cleanup

**Step 1: Clean up junk vendor entries**

```sql
-- Remove single-character vendor names
DELETE FROM vendor_cards WHERE LENGTH(TRIM(name)) <= 1;
-- Remove known non-vendors (newsletters, marketing senders)
DELETE FROM vendor_cards WHERE normalized_name IN ('1password', 'newsletter', 'noreply', 'mailer-daemon');
```

**Step 2: Add minimum name length validation to vendor creation**

---

### Task 13: OEMSecrets/DigiKey Response Times (#893)

**Files:**
- Modify: `app/connectors/element14.py`, `app/connectors/digikey.py`, `app/connectors/oemsecrets.py`

**Step 1: Add per-connector timeout caps**

These are external API latencies. Set connector-level timeouts to 5s (currently may use default 30s):

```python
# In each connector's search() method, ensure timeout is set:
r = self.session.get(url, params=params, timeout=5)
```

**Step 2: Run tests, commit**

---

### Task 14: Resolve all 15 tickets in DB

**Step 1: Mark all 15 tickets as resolved with resolution notes**

---

### Task 15: Final test suite + deploy

**Step 1: Run full test suite with coverage**
**Step 2: Commit all changes**
**Step 3: Deploy**
