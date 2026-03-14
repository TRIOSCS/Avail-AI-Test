# Frontend Security & Code Quality Review

**Files analyzed:**
- `app/static/app.js` (15,766 lines)
- `app/static/crm.js` (8,468 lines)
- `app/templates/index.html` (1,487 lines)

**Total:** 25,721 lines of frontend code across 3 files.

---

## 1. XSS Vulnerabilities (innerHTML Usage, Unsanitized User Input)

### Severity: HIGH

The codebase has **263 innerHTML assignments in app.js** and **194 in crm.js**. While an `esc()` function exists (line 437, app.js) and is used in many places, several dangerous patterns remain:

### 1a. Incomplete escaping in onclick attribute injection

**Pattern:** Vendor names and user names are "sanitized" only by escaping single quotes, not HTML entities, before injection into onclick attributes.

| File | Line | Code | Risk |
|------|------|------|------|
| `app.js` | 7755 | `const safeHVName = (ho.vendor_name\|\|'').replace(/'/g, "\\'");` | Vendor name with `"` or `>` characters breaks out of onclick attribute |
| `app.js` | 7779 | `const safeVName = (s.vendor_name\|\|'').replace(/'/g, "\\'");` | Same issue — vendor names come from external APIs |
| `app.js` | 8114 | `vendorName.replace(/'/g,"\\'")` | Same pattern in email save handler |
| `app.js` | 4035 | `esc(v.display_name).replace(/'/g, "\\'")` | Better (esc + quote escape) but still ad-hoc |
| `crm.js` | 6585 | `(u.name\|\|u.email).replace(/'/g,"\\'")`| User name/email only has quote escaping |
| `crm.js` | 1271 | `esc(c.name \|\| 'Account').replace(/'/g, "\\'")` | Mixed pattern — esc + manual quote escape |

**Why this matters:** A vendor named `"><img src=x onerror=alert(1)>` would break out of the single-quoted onclick attribute at lines 7755/7779, since only `'` is escaped but not `"`, `<`, or `>`. These vendor names come from external supplier APIs (Mouser, DigiKey, BrokerBin, etc.) and are not guaranteed to be safe.

**Fix:** Replace all `.replace(/'/g, "\\'")` patterns with `escAttr()` which properly escapes `&`, `"`, `'`, `<`, and `>`.

### 1b. Unescaped user data in select options

| File | Line | Code |
|------|------|------|
| `crm.js` | 6660 | `` `<option value="${u.id}">${u.name} (${u.email})</option>` `` |

User name and email are injected directly into option innerHTML without `esc()`. A user with a name containing `<script>` tags would execute.

### 1c. JSON serialized into onclick attribute

| File | Line | Code |
|------|------|------|
| `app.js` | 7754, 7768 | `const hoJson = esc(JSON.stringify(ho));` then `onclick='...${hoJson}...'` |

The `ho` object (historical offer from API) is JSON-serialized, HTML-escaped, then placed in a single-quoted onclick attribute. While `esc()` handles `<`, `>`, `&`, JSON.stringify uses double quotes which `esc()` does not escape. The onclick uses single quotes, so double quotes in the JSON don't directly break it, but this is fragile and relies on implementation details.

### 1d. `_formatEmailBody` regex URL auto-linking

| File | Lines | Description |
|------|-------|-------------|
| `app.js` | 3264-3265 | After `esc()`, URLs are converted back to `<a>` tags via regex |

```javascript
let safe = esc(cleaned);
safe = safe.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" ...>$1</a>');
```

The regex `[^\s<]+` could match URL-encoded characters. After `esc()`, `&` becomes `&amp;`, `"` becomes `&quot;` — these are safe in href. However a URL like `https://evil.com/x"onmouseover="alert(1)` would get `"` escaped to `&quot;` by `esc()` first, so this is actually safe. Low risk but fragile.

### 1e. innerHTML with template-built HTML (not user data but still risky)

| File | Lines | Count | Description |
|------|-------|-------|-------------|
| `app.js` | 38 instances | `innerHTML = \`...\`` | Template literals with interpolated data |
| `crm.js` | 24 instances | `innerHTML = \`...\`` | Template literals with interpolated data |

Most use `esc()` or `escAttr()` correctly, but the sheer volume (457 total innerHTML usages) makes auditing difficult and increases the chance of missing a case.

### 1f. sanitizeRichHtml strengths and weaknesses

| File | Lines | Description |
|------|-------|-------------|
| `app.js` | 445-486 | DOMParser-based HTML sanitizer |

**Good:** Uses an allowlist of tags and attributes, strips `on*` event handlers, validates `href` protocols (only `https:`, `mailto:`, `tel:`), adds `rel="noopener noreferrer"`.

**Weakness:** Allows `<a>` tags with `href` — while protocols are checked, `data:` and `blob:` are not explicitly blocked in the regex (the test is `/^(https?:|mailto:|tel:)/i` which correctly rejects them). Style attributes are stripped. This is a reasonable sanitizer.

---

## 2. CSRF Token Handling

### Severity: MEDIUM

### 2a. CSRF implementation (GOOD)

| File | Lines | Description |
|------|-------|-------------|
| `app.js` | 342-344 | Double-submit cookie pattern in `apiFetch()` |
| `main.py` | 279-296 | `starlette_csrf.CSRFMiddleware` with secret key |

The `apiFetch()` wrapper reads a `csrftoken` cookie and sends it as an `x-csrftoken` header. The backend middleware validates this on all non-exempt routes.

### 2b. Fire-and-forget fetch bypasses CSRF

| File | Line | Code |
|------|------|------|
| `app.js` | 576-581 | `fetch('/api/activity/call-initiated', {...})` |

This direct `fetch()` call does NOT go through `apiFetch()` and therefore does NOT include the CSRF token header. While it's a low-risk analytics endpoint, it sets a precedent for bypassing CSRF protection.

### 2c. CSRF exemptions may be too broad

| File | Lines | Patterns |
|------|-------|----------|
| `main.py` | 290 | `r"/auth/.*"` — all auth routes exempt |
| `main.py` | 294 | `r"/v2/.*"` — all v2 HTMX views exempt |
| `main.py` | 293 | `r"/api/buy-plans/token/.*"` — external approval links |

The `/v2/.*` exemption is concerning if those views perform state-changing operations. The comment says "HTMX views use session auth, not CSRF tokens" but HTMX requests should still validate CSRF.

---

## 3. Authentication Token/Session Management

### Severity: LOW (well-implemented)

### 3a. Session configuration

| File | Lines | Setting | Assessment |
|------|-------|---------|------------|
| `main.py` | 271 | `https_only=settings.app_url.startswith("https")` | Good — enforces Secure flag in production |
| `main.py` | 272 | `same_site="lax"` | Good — prevents most CSRF via cookies |
| `main.py` | 273 | `max_age=86400` | 24-hour session — reasonable |

**Missing:** No `httponly` parameter is set on the session cookie. The `SessionMiddleware` from Starlette sets `httponly=True` by default, but this should be explicitly verified.

### 3b. Session expiry handling (GOOD)

| File | Lines | Description |
|------|-------|-------------|
| `app.js` | 374-378 | 401 response → toast + redirect to `/auth/login` |

### 3c. User config bootstrap

| File | Lines | Description |
|------|-------|-------------|
| `index.html` | 1472 | `<script type="application/json" id="app-config">` with `tojson` filter |
| `app.js` | 16-28 | Parsed with try-catch in IIFE |

**Good:** Uses `tojson` Jinja2 filter which safely serializes to JSON (no XSS via `</script>` injection). Parsed with error handling.

### 3d. Sensitive data on window object

| File | Lines | Variables |
|------|-------|-----------|
| `app.js` | 1006-1008 | `window.userRole`, `window.userName`, `window.userEmail` |

User role, name, and email are stored on the `window` object, accessible to any script on the page. Not a critical issue since the CSP restricts script sources, but could be a concern if CSP is bypassed.

---

## 4. Error Handling in Fetch Calls

### Severity: MEDIUM

### 4a. apiFetch error handling (GOOD)

| File | Lines | Feature |
|------|-------|---------|
| `app.js` | 341-401 | Centralized fetch wrapper |
| `app.js` | 350-352 | GET request deduplication |
| `app.js` | 354-358 | POST/PUT/DELETE double-click protection (1s cooldown) |
| `app.js` | 361-362 | Offline detection |
| `app.js` | 364-388 | Retry logic (2 retries for GET, 0 for mutations) |
| `app.js` | 382-386 | Rate limit handling with `retry-after` header |
| `app.js` | 14285-14290 | Global `unhandledrejection` handler |

This is well-designed. Good practices throughout.

### 4b. Silent catch blocks

| File | Lines | Pattern |
|------|-------|---------|
| `app.js` | 57-58 | `try { localStorage.setItem(key, val); } catch(e) {}` — acceptable for localStorage |
| `app.js` | 4052 | `try { msg = (await e.json ? e.json() : e).detail \|\| msg; } catch(ex) {}` — swallows parse error |
| `crm.js` | 923 | `try { c = await apiFetch(...); } catch(_e) {}` — silently ignores API failure |
| `crm.js` | 1474 | `try { c = await apiFetch(...); } catch(_e) {}` — same |
| `crm.js` | 7042, 7060 | Empty catch blocks — silently ignores errors |

While the localStorage wrappers are fine (lines 57-58), the API call silences at crm.js lines 923, 1474, 7042, 7060 could hide legitimate failures from the user.

### 4c. Uncaught JSON.parse

| File | Line | Code |
|------|------|------|
| `app.js` | 2620 | `let _hiddenCols = JSON.parse(localStorage.getItem('reqHiddenCols') \|\| '{}');` |

This JSON.parse is not wrapped in try-catch. If localStorage contains corrupted data, this will throw and potentially break the entire app initialization. The `safeGet()` wrapper handles localStorage access failure but not JSON parse failure.

---

## 5. DOM Manipulation Safety

### Severity: MEDIUM

### 5a. Massive use of inline event handlers

| File | Count | Pattern |
|------|-------|---------|
| `app.js` | 296 | `onclick=` in template strings |
| `crm.js` | 182 | `onclick=` in template strings |
| `index.html` | 227 | `onclick=` on HTML elements |

Total: **705 inline onclick handlers** across the frontend. This:
- Forces the CSP to include `'unsafe-inline'` for script-src (line 325, main.py), which significantly weakens XSS protection
- Makes the code harder to maintain and debug
- Prevents using CSP nonces or strict-dynamic

### 5b. insertAdjacentHTML usage

| File | Lines | Count |
|------|-------|-------|
| `app.js` | 4855, 4857, 4996, 5156, 5678, 8202, 8780 | 7 instances |

These inject HTML built from template literals directly into the DOM. Same XSS risks as innerHTML.

### 5c. document.body.appendChild with dynamically created elements

Many modal overlays and popovers are created by building HTML strings and appending to `document.body`. While the data is generally escaped, building DOM via strings rather than `document.createElement()` is inherently less safe.

### 5d. Object.assign(window, {...}) global namespace pollution

| File | Lines | Description |
|------|-------|-------------|
| `app.js` | 15664-15766 | ~150+ functions assigned to `window` |

Because inline onclick handlers can only call global functions, the entire app's function namespace is dumped onto `window`. This exposes internal functions to the global scope and could enable prototype pollution attacks.

---

## 6. Hardcoded Secrets or URLs

### Severity: LOW (none found)

- No hardcoded API keys, secrets, or passwords found in any frontend file
- No hardcoded IP addresses or internal URLs
- All API calls use relative URLs (e.g., `/api/...`)
- Config is loaded from server-rendered JSON block at `index.html:1472`
- External resources are limited to CDNs: `fonts.googleapis.com`, `cdnjs.cloudflare.com`, `unpkg.com`, `cdn.jsdelivr.net`

---

## 7. Memory Leaks (Event Listeners)

### Severity: MEDIUM

### 7a. Event listener imbalance

| File | addEventListener | removeEventListener | Ratio |
|------|-----------------|---------------------|-------|
| `app.js` | 72 | 5 | 14:1 |
| `crm.js` | 11 | 2 | 5.5:1 |

Most addEventListener calls are on document-level (global) handlers that run once at startup, which is acceptable. However:

### 7b. Event listeners in frequently-called functions

| File | Line | Description |
|------|------|-------------|
| `app.js` | 873-875 | `document.addEventListener('click', ...)` inside `initNameAutocomplete` — called each time a typeahead is initialized, adds a new global click listener each time |
| `app.js` | 4680 | `document.addEventListener('click', function _cl(e) {...})` inside `ddShowChangelog` — adds listener per changelog view but does clean up properly |
| `app.js` | 4861 | `document.addEventListener('click', function _closeCopy(e) {...})` inside copy-terms dropdown — cleaned up on close |
| `app.js` | 8384 | `document.addEventListener('click', ...)` inside vendor autocomplete — adds on every render |

### 7c. setInterval without cleanup

| File | Line | Interval | Cleanup |
|------|------|----------|---------|
| `app.js` | 1051 | `checkM365Status` every 5 min | Cleared on beforeunload (line 1053) |
| `app.js` | 1161 | `pollApiHealth` every 60s | Cleared on beforeunload (line 1162) |
| `crm.js` | 5062 | `refreshProactiveBadge` every 5 min | **Never cleared** |

The crm.js interval at line 5062 is never cleared. Not a huge issue in a SPA that lives for the session, but worth noting.

### 7d. AbortController usage (GOOD)

| File | Count | Description |
|------|-------|-------------|
| `app.js` | 13 | Used for canceling in-flight requests on view changes |
| `crm.js` | 2 | Used for customer list loading |

Good use of AbortController to cancel stale requests.

---

## 8. Accessibility Issues

### Severity: HIGH

### 8a. Missing form labels

| Metric | Count |
|--------|-------|
| `<input>` elements in index.html | 113 |
| `<label>` elements | 114 |
| `<label for=...>` elements | 44 |
| `aria-label` attributes | 19 |

**~50 input elements lack any associated label** (no `<label for>`, no `aria-label`, no `aria-labelledby`). This makes the app unusable for screen reader users.

### 8b. Missing ARIA roles and states

| Metric | Count |
|--------|-------|
| `role=` attributes in index.html | 7 |
| `aria-` attributes in app.js | 4 |
| `aria-` attributes in crm.js | 1 |

For an app with 171 buttons, 113 inputs, modal dialogs, tabs, drawers, and dropdown menus, 12 total ARIA attributes is severely insufficient.

### 8c. Missing keyboard navigation

- Buttons created via innerHTML with `onclick=` handlers but no `tabindex` or keyboard event support
- Only 4 `tabindex` attributes in the entire index.html
- Modal dialogs don't trap focus
- Drawer/sidebar components have no keyboard close (Escape) support at the component level
- Sort headers on tables use `onclick` but no keyboard interaction

### 8d. Color-only indicators

- Many status indicators use only color (e.g., `var(--green)`, `var(--red)`) without text alternatives
- Score rings and progress bars lack text descriptions
- M365 connection status uses colored dots only (`m365-dot red`, `m365-dot green`)

### 8e. Dynamic content announcements

- Toast notifications (line 2452+, app.js) don't use `role="alert"` or `aria-live`
- The offline banner at line 14299 correctly uses `role="alert"` (good)
- Loading states and search results don't announce to screen readers

### 8f. Images missing alt text (GOOD)

All `<img>` tags in index.html have `alt` attributes.

---

## 9. Code Organization and Maintainability

### Severity: HIGH (technical debt)

### 9a. File size

| File | Lines | Assessment |
|------|-------|------------|
| `app.js` | 15,766 | Extremely large — should be split into modules |
| `crm.js` | 8,468 | Very large |
| `index.html` | 1,487 | Large but acceptable |
| **Total** | **25,721** | Enormous single-page app |

A 15,766-line JavaScript file is very difficult to maintain, debug, or review for security issues.

### 9b. Global state proliferation

| File | Pattern | Count |
|------|---------|-------|
| `app.js` | `window.xyz = ...` | 36 explicit assignments |
| `app.js` | `Object.assign(window, {...})` | ~150 functions at lines 15664-15766 |
| `app.js` | Module-level `let`/`const` caches | 30+ (e.g., `_ddTabCache`, `_ddSelectedOffers`, `_ddQuoteData`) |

### 9c. Naming conventions inconsistency

Functions use a mix of:
- `camelCase`: `loadRequisitions`, `showToast`
- `_underscorePrefix`: `_renderDdActivity`, `_ddTabCache`
- `ddPrefix` (drill-down): `ddBuildQuote`, `ddSaveEditOffer`
- Abbreviations: `dd`, `bp`, `rfq`, `vp`, `req`

No consistent convention across the codebase.

### 9d. Code duplication

| Pattern | Locations | Description |
|---------|-----------|-------------|
| Vendor name escaping | app.js lines 7755, 7779, 8114, 4035, 4847 | Each does its own `.replace(/'/g, "\\'")` |
| Modal creation | app.js lines 2111, 4287, 4354, 6073, 6320, 13761, 13818 | Each builds modal HTML from scratch |
| Error display | app.js lines 13987, 14051, 14135 | Nearly identical error HTML patterns |
| Score ring rendering | app.js + crm.js | Duplicated in both files |

### 9e. Inline styles

| File | Pattern | Count |
|------|---------|-------|
| `app.js` | `style="..."` in template strings | 500+ instances |
| `crm.js` | `style="..."` in template strings | 300+ instances |

Extensive use of inline styles instead of CSS classes makes the UI inconsistent and hard to theme/maintain.

### 9f. `javascript:void(0)` usage

| File | Lines |
|------|-------|
| `crm.js` | 1034, 3307, 3353, 3371 |

Using `javascript:void(0)` in href attributes is an anti-pattern. Should use `href="#"` with `event.preventDefault()` or button elements.

### 9g. console.log statements in production

| File | Count |
|------|-------|
| `app.js` | 36 console.log/warn/error calls |
| `crm.js` | 26 console.log/warn/error calls |

62 total console statements that ship to production. While most are `console.warn` or `console.error` (appropriate), `console.log` calls should be removed.

---

## Summary of Risk Levels

| Category | Severity | Key Issue |
|----------|----------|-----------|
| XSS — innerHTML | HIGH | 457 innerHTML usages; ~10 with incomplete escaping |
| XSS — onclick injection | HIGH | Vendor names (external API data) with inadequate escaping in attributes |
| CSRF | MEDIUM | One fetch bypasses CSRF; `/v2/` routes exempt |
| Auth/Session | LOW | Well-implemented; minor concern about sensitive data on window |
| Error handling | MEDIUM | Silent catch blocks hide failures; one uncaught JSON.parse |
| DOM safety | MEDIUM | 705 inline handlers force `unsafe-inline` CSP |
| Hardcoded secrets | LOW | None found |
| Memory leaks | MEDIUM | 1 uncleaned interval; event listeners in re-callable functions |
| Accessibility | HIGH | ~50 unlabeled inputs; 12 ARIA attributes for 700+ interactive elements |
| Code organization | HIGH | 15K+ line file; 150+ globals; extensive duplication |

---

## Top 5 Priority Fixes

1. **Replace all `.replace(/'/g, "\\'")` with `escAttr()`** — Lines: app.js 7755, 7779, 8114, crm.js 6585. External vendor names could contain XSS payloads.

2. **Add `esc()` to crm.js line 6660** — User names/emails rendered in select options without escaping.

3. **Route the fire-and-forget fetch through `apiFetch()`** — app.js line 576. Ensures CSRF token is included.

4. **Wrap JSON.parse(localStorage) in try-catch** — app.js line 2620. Corrupted localStorage crashes the app.

5. **Add `aria-label` attributes to inputs and buttons** — Start with the most interactive elements: search bars, filter inputs, action buttons.
