# Trouble Report Button — Design Spec

> Created: 2026-03-20

## Purpose

A built-in bug reporting widget so the team can quickly file issues while testing AvailAI before go-live. Reports are filed as GitHub Issues on `TRIOSCS/Avail-AI-Test` with auto-captured context (URL, browser info, JS errors, user info).

## User Flow

1. User sees a small bug-icon button fixed in the bottom-right corner of every page
2. User clicks it — a modal opens with a description textarea
3. User types what went wrong, clicks "Submit Report"
4. Backend creates a GitHub Issue with the description + auto-captured context
5. Toast confirms: "Report filed — thank you!"
6. Modal closes

## Architecture

### Frontend (no new dependencies)

**Button**: Fixed-position icon button, bottom-right corner. Rose-500 background, white bug icon (inline SVG). Hover brightens to rose-400. `z-index: 40` (above content, below modals at 50).

**Modal**: Reuse existing `modal.html` pattern. Open via `$dispatch('open-modal')`. Modal content loaded via HTMX GET to `/api/trouble-report/form` which returns the form partial.

**Form partial** (`trouble_report_form.html`):
- Textarea: "What went wrong?" (required, 3 rows min)
- Hidden inputs auto-populated by Alpine on form load:
  - `current_url` — `window.location.href`
  - `browser_info` — `navigator.userAgent`
  - `viewport` — `${window.innerWidth}x${window.innerHeight}`
  - `js_errors` — JSON from error capture store (see below)
- Submit via HTMX POST to `/api/trouble-report`
- On success: swap empty div + dispatch `close-modal` + toast success

**JS Error Capture**: Add to `htmx_app.js`:
```javascript
Alpine.store('errorLog', { entries: [] });

window.onerror = function(msg, src, line, col, err) {
    const log = Alpine.store('errorLog').entries;
    log.push({ msg, src, line, col, ts: new Date().toISOString() });
    if (log.length > 10) log.shift();
};

window.onunhandledrejection = function(e) {
    const log = Alpine.store('errorLog').entries;
    log.push({ msg: String(e.reason), ts: new Date().toISOString() });
    if (log.length > 10) log.shift();
};
```

### Backend

**Config** (`app/config.py`):
```python
github_trouble_report_token: str = ""
github_trouble_report_repo: str = "TRIOSCS/Avail-AI-Test"
```

Feature is active when `github_trouble_report_token` is non-empty. Button hidden otherwise.

**Router** (`app/routers/trouble_report.py`):

Two endpoints:
- `GET /api/trouble-report/form` — returns the form partial (requires auth)
- `POST /api/trouble-report` — creates GitHub Issue (requires auth)

POST handler:
1. Validates description is non-empty
2. Builds issue body as markdown:
   ```markdown
   ## Description
   {user description}

   ## Context
   - **URL**: {current_url}
   - **User**: {user.email} ({user.role})
   - **Browser**: {browser_info}
   - **Viewport**: {viewport}
   - **Timestamp**: {ISO timestamp}

   ## JS Errors (last 10)
   {formatted error list or "None captured"}
   ```
3. Calls GitHub API via `httpx`:
   ```
   POST https://api.github.com/repos/{repo}/issues
   Authorization: Bearer {token}
   Body: { title, body, labels: ["bug", "trouble-report"] }
   ```
4. Returns success partial (triggers modal close + toast)

### Files

| Action | File |
|--------|------|
| Create | `app/routers/trouble_report.py` — GET form + POST submit |
| Create | `app/templates/htmx/partials/shared/trouble_report_button.html` — fixed button |
| Create | `app/templates/htmx/partials/shared/trouble_report_form.html` — modal form |
| Modify | `app/config.py` — add 2 config vars |
| Modify | `app/main.py` — register router |
| Modify | `app/templates/htmx/base_page.html` — include button partial |
| Modify | `app/static/htmx_app.js` — add errorLog store + window.onerror |
| Create | `tests/test_trouble_report.py` — endpoint tests |

### What's NOT in v1

- No screenshot capture (html2canvas adds 40KB + complexity)
- No console.log/warn capture (only actual errors via onerror)
- No ticket tracking UI in AvailAI (just GitHub Issues)
- No file attachments
- No priority/severity fields

### Dependencies

- `httpx` (already in requirements.txt) for GitHub API calls
- GitHub Personal Access Token with `repo` scope in `.env`
- No new frontend dependencies

### Success Criteria

1. Red bug button visible on every page when token is configured
2. Clicking opens modal with description field
3. Submitting creates a GitHub Issue with correct labels and context
4. Toast confirms success
5. Modal closes after submit
6. Works when no JS errors have been captured (empty error log)
7. Graceful error if GitHub API fails (toast with error message)
