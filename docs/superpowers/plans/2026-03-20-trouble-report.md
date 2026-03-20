# Trouble Report Button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a floating bug-report button to every page that files GitHub Issues with auto-captured context (URL, browser info, JS errors, user info).

**Architecture:** Fixed-position button in bottom-right corner opens the existing global modal via `$dispatch('open-modal')`. Form partial loaded via HTMX GET, submitted via HTMX POST. Backend calls GitHub API with httpx to create an issue. Feature gated by `GITHUB_TROUBLE_REPORT_TOKEN` env var.

**Tech Stack:** FastAPI, HTMX, Alpine.js, Jinja2, httpx, GitHub Issues API

**Spec:** `docs/superpowers/specs/2026-03-20-trouble-report-design.md`

---

## File Map

### New Files
| File | Responsibility |
|------|---------------|
| `app/routers/trouble_report.py` | GET form partial + POST submit to GitHub API |
| `app/templates/htmx/partials/shared/trouble_report_button.html` | Fixed-position bug icon button |
| `app/templates/htmx/partials/shared/trouble_report_form.html` | Modal form: description + hidden context fields |
| `tests/test_trouble_report.py` | Endpoint tests |

### Modified Files
| File | Change |
|------|--------|
| `app/config.py` | Add `github_trouble_report_token` and `github_trouble_report_repo` |
| `app/main.py` | Register `trouble_report_router` |
| `app/templates/htmx/base.html` | Include button partial before modal section |
| `app/routers/htmx_views.py` | Add `trouble_reporting_enabled` Jinja2 global |
| `app/static/htmx_app.js` | Add `errorLog` Alpine store + `window.onerror` + `window.onunhandledrejection` |

**Note:** An existing `app/routers/error_reports.py` provides DB-backed `/api/trouble-tickets` endpoints with a `TroubleTicket` model. This new feature coexists with it — the floating button files to GitHub Issues for developer convenience during pre-launch testing, while the existing DB system remains for production use.

---

### Task 1: Config + Router Skeleton + Tests

**Files:**
- Modify: `app/config.py`
- Create: `app/routers/trouble_report.py`
- Modify: `app/main.py`
- Create: `tests/test_trouble_report.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_trouble_report.py`. Uses the shared `client` fixture from `conftest.py` (which overrides `require_user` via `dependency_overrides`):
```python
"""test_trouble_report.py — Tests for trouble report endpoints.

Covers: GET /api/trouble-report/form, POST /api/trouble-report.
Uses the shared client fixture from conftest.py with auth overrides.
"""

from unittest.mock import AsyncMock, patch

from tests.conftest import engine  # noqa: F401


def test_form_returns_html(client):
    """GET /api/trouble-report/form returns the form partial."""
    resp = client.get("/api/trouble-report/form")
    assert resp.status_code == 200
    assert "What went wrong?" in resp.text


def test_submit_requires_description(client):
    """POST /api/trouble-report rejects empty description."""
    resp = client.post("/api/trouble-report", data={
        "description": "",
        "current_url": "http://localhost/v2/requisitions",
        "browser_info": "test-agent",
        "viewport": "1920x1080",
        "js_errors": "[]",
    })
    assert resp.status_code == 422 or "required" in resp.text.lower()


@patch("app.routers.trouble_report.httpx.AsyncClient")
def test_submit_creates_github_issue(mock_client_cls, client):
    """POST /api/trouble-report calls GitHub API and returns success."""
    mock_resp = AsyncMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"html_url": "https://github.com/TRIOSCS/Avail-AI-Test/issues/999", "number": 999}

    mock_instance = AsyncMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_instance.post = AsyncMock(return_value=mock_resp)
    mock_client_cls.return_value = mock_instance

    with patch("app.routers.trouble_report.settings") as mock_settings:
        mock_settings.github_trouble_report_token = "test-token"
        mock_settings.github_trouble_report_repo = "TRIOSCS/Avail-AI-Test"

        resp = client.post("/api/trouble-report", data={
            "description": "The search page is broken",
            "current_url": "http://localhost/v2/search",
            "browser_info": "Mozilla/5.0 Test",
            "viewport": "1920x1080",
            "js_errors": "[]",
        })
    assert resp.status_code == 200
    assert "Report filed" in resp.text or "success" in resp.text.lower()


@patch("app.routers.trouble_report.httpx.AsyncClient")
def test_submit_handles_github_failure(mock_client_cls, client):
    """POST /api/trouble-report handles GitHub API errors gracefully."""
    mock_resp = AsyncMock()
    mock_resp.status_code = 403
    mock_resp.text = "Rate limited"

    mock_instance = AsyncMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_instance.post = AsyncMock(return_value=mock_resp)
    mock_client_cls.return_value = mock_instance

    with patch("app.routers.trouble_report.settings") as mock_settings:
        mock_settings.github_trouble_report_token = "test-token"
        mock_settings.github_trouble_report_repo = "TRIOSCS/Avail-AI-Test"

        resp = client.post("/api/trouble-report", data={
            "description": "Something broke",
            "current_url": "http://localhost/v2",
            "browser_info": "Test",
            "viewport": "1920x1080",
            "js_errors": "[]",
        })
    assert resp.status_code == 200
    assert "error" in resp.text.lower() or "failed" in resp.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_report.py -v`
Expected: FAIL — endpoint not found (404) or import error.

- [ ] **Step 3: Add config vars**

In `app/config.py`, add to the `Settings` class (near other API key fields):
```python
    # Trouble reporting — files GitHub Issues when token is set
    github_trouble_report_token: str = ""
    github_trouble_report_repo: str = "TRIOSCS/Avail-AI-Test"
```

- [ ] **Step 4: Create router**

Create `app/routers/trouble_report.py`:
```python
"""trouble_report.py — Bug report widget endpoints.

Provides a form partial and submission endpoint that creates GitHub Issues
with auto-captured context (URL, browser info, JS errors, user info).

Called by: trouble_report_button.html, trouble_report_form.html
Depends on: httpx, app.config, app.dependencies
"""

from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from app.config import settings
from app.dependencies import require_user

router = APIRouter(tags=["trouble-report"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/api/trouble-report/form", response_class=HTMLResponse)
async def trouble_report_form(request: Request, user=Depends(require_user)):
    """Return the trouble report form partial for the modal."""
    return templates.TemplateResponse(
        "htmx/partials/shared/trouble_report_form.html",
        {"request": request, "user": user},
    )


@router.post("/api/trouble-report", response_class=HTMLResponse)
async def submit_trouble_report(
    request: Request,
    user=Depends(require_user),
    description: str = Form(""),
    current_url: str = Form(""),
    browser_info: str = Form(""),
    viewport: str = Form(""),
    js_errors: str = Form("[]"),
):
    """Create a GitHub Issue with the trouble report."""
    if not description.strip():
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm font-medium">'
            "Description is required.</div>",
            status_code=422,
        )

    token = settings.github_trouble_report_token
    repo = settings.github_trouble_report_repo
    if not token:
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm font-medium">'
            "Trouble reporting is not configured.</div>"
        )

    title = f"[Trouble] {description[:60]}"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = (
        f"## Description\n{description}\n\n"
        f"## Context\n"
        f"- **URL**: {current_url}\n"
        f"- **User**: {user.email} ({getattr(user, 'role', 'unknown')})\n"
        f"- **Browser**: {browser_info}\n"
        f"- **Viewport**: {viewport}\n"
        f"- **Timestamp**: {ts}\n\n"
        f"## JS Errors (last 10)\n"
        f"```\n{js_errors}\n```"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"title": title, "body": body, "labels": ["bug", "trouble-report"]},
                timeout=15.0,
            )
        if resp.status_code == 201:
            data = resp.json()
            issue_url = data.get("html_url", "")
            issue_num = data.get("number", "")
            logger.info(f"Trouble report filed: #{issue_num} by {user.email}")
            return HTMLResponse(
                f'<div class="p-6 text-center">'
                f'<div class="text-emerald-600 text-lg font-semibold mb-2">Report filed — thank you!</div>'
                f'<div class="text-sm text-gray-500">Issue <a href="{issue_url}" target="_blank" '
                f'class="text-brand-600 underline">#{issue_num}</a> created.</div>'
                f'<script>setTimeout(() => {{ document.dispatchEvent(new CustomEvent("close-modal")); '
                f"Alpine.store('toast').message = 'Report filed — thank you!'; "
                f"Alpine.store('toast').type = 'success'; "
                f"Alpine.store('toast').show = true; }}, 1500)</script>"
                f"</div>"
            )
        else:
            logger.error(f"GitHub API error {resp.status_code}: {resp.text[:200]}")
            return HTMLResponse(
                '<div class="p-4 text-rose-600 text-sm font-medium">'
                f"Failed to file report (GitHub returned {resp.status_code}). "
                "Please try again or report manually.</div>"
            )
    except Exception as e:
        logger.error(f"Trouble report submission failed: {e}")
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm font-medium">'
            "Failed to file report. Please try again.</div>"
        )
```

- [ ] **Step 5: Register router in main.py**

In `app/main.py`, add with the other router imports:
```python
from .routers.trouble_report import router as trouble_report_router
```

And in the `include_router` block:
```python
app.include_router(trouble_report_router)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_report.py -v`
Expected: Most tests PASS. The form test may need the template to exist (Task 2).

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/routers/trouble_report.py app/main.py tests/test_trouble_report.py
git commit -m "feat: add trouble report backend — config, router, tests"
```

---

### Task 2: Frontend — Button + Form + Error Capture

**Files:**
- Create: `app/templates/htmx/partials/shared/trouble_report_button.html`
- Create: `app/templates/htmx/partials/shared/trouble_report_form.html`
- Modify: `app/templates/htmx/base.html`
- Modify: `app/static/htmx_app.js`

- [ ] **Step 1: Create the button partial**

Create `app/templates/htmx/partials/shared/trouble_report_button.html`:
```html
{#
  trouble_report_button.html — Fixed bug-report button, bottom-right corner.
  Opens the global modal and loads the trouble report form via HTMX.
  Only rendered when github_trouble_report_token is configured.
  Called by: base.html
  Depends on: modal.html, Alpine.js
#}
<button
  hx-get="/api/trouble-report/form"
  hx-target="#modal-content"
  @click="$dispatch('open-modal')"
  title="Report a bug"
  class="fixed bottom-20 right-4 z-40 flex h-10 w-10 items-center justify-center rounded-full bg-rose-500 text-white shadow-lg transition-all hover:bg-rose-400 hover:shadow-xl hover:scale-110 active:scale-95">
  <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
    <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/>
  </svg>
</button>
```

- [ ] **Step 2: Create the form partial**

Create `app/templates/htmx/partials/shared/trouble_report_form.html`:
```html
{#
  trouble_report_form.html — Trouble report modal form.
  Auto-captures URL, browser info, viewport, and JS errors into hidden fields.
  Submits via HTMX POST, response swaps into modal-content.
  Called by: trouble_report_button.html (hx-get loads this)
  Depends on: Alpine.js errorLog store
#}
<div class="p-6" x-data="{
  currentUrl: window.location.href,
  browserInfo: navigator.userAgent,
  viewport: window.innerWidth + 'x' + window.innerHeight,
  jsErrors: JSON.stringify($store.errorLog?.entries || [])
}">
  <div class="flex items-center gap-3 mb-4">
    <div class="flex h-10 w-10 items-center justify-center rounded-full bg-rose-100 text-rose-600">
      <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/>
      </svg>
    </div>
    <div>
      <h3 class="text-lg font-semibold text-gray-900">Report a Problem</h3>
      <p class="text-sm text-gray-500">We'll file this as a bug report</p>
    </div>
  </div>

  <form hx-post="/api/trouble-report"
        hx-target="#modal-content"
        hx-swap="innerHTML"
        hx-indicator="#trouble-submit-spinner">

    <textarea name="description"
              rows="4"
              required
              placeholder="What went wrong? What did you expect to happen?"
              class="w-full rounded-lg border-2 border-gray-200 p-3 text-sm text-gray-900 placeholder-gray-400 outline-none focus:border-brand-500 transition-colors resize-y"></textarea>

    <input type="hidden" name="current_url" :value="currentUrl">
    <input type="hidden" name="browser_info" :value="browserInfo">
    <input type="hidden" name="viewport" :value="viewport">
    <input type="hidden" name="js_errors" :value="jsErrors">

    <div class="mt-3 text-xs text-gray-400">
      Auto-attaching: page URL, browser info, recent JS errors
    </div>

    <div class="mt-4 flex justify-end gap-3">
      <button type="button"
              @click="$dispatch('close-modal')"
              class="rounded-lg border border-gray-200 px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50 transition-colors">
        Cancel
      </button>
      <button type="submit"
              class="rounded-lg bg-rose-500 px-4 py-2 text-sm font-medium text-white hover:bg-rose-400 transition-colors flex items-center gap-2">
        <svg id="trouble-submit-spinner" class="htmx-indicator h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
        </svg>
        Submit Report
      </button>
    </div>
  </form>
</div>
```

- [ ] **Step 3: Add errorLog Alpine store to htmx_app.js**

In `app/static/htmx_app.js`, add after the existing `Alpine.store('shortlist', ...)` block (after the closing `});`):

```javascript
// ── Error capture for trouble reports ────────────────────────
Alpine.store('errorLog', { entries: [] });

window.onerror = function(msg, src, line, col) {
    const log = Alpine.store('errorLog').entries;
    log.push({ msg: String(msg), src, line, col, ts: new Date().toISOString() });
    if (log.length > 10) log.shift();
};

window.onunhandledrejection = function(e) {
    const log = Alpine.store('errorLog').entries;
    log.push({ msg: String(e.reason), ts: new Date().toISOString() });
    if (log.length > 10) log.shift();
};
```

- [ ] **Step 4: Include button in base.html**

In `app/templates/htmx/base.html`, add just before the `{# ── Modal (global) ──` comment (around line 140):

```html
  {# ── Trouble report button (bottom-right) ─────────────────── #}
  {% if trouble_reporting_enabled %}
  {% include "htmx/partials/shared/trouble_report_button.html" %}
  {% endif %}
```

This requires `trouble_reporting_enabled` in the Jinja2 template context. Add it as a global in `app/routers/htmx_views.py` (where the main `templates` object is defined, around line 126 after the existing filter registrations):

```python
from app.config import settings  # already imported
templates.env.globals["trouble_reporting_enabled"] = bool(settings.github_trouble_report_token)
```

Also add the same line in `app/routers/trouble_report.py` after the `templates` definition.

- [ ] **Step 5: Run all trouble report tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_report.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=no -q`
Expected: All tests PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add app/templates/htmx/partials/shared/trouble_report_button.html app/templates/htmx/partials/shared/trouble_report_form.html app/static/htmx_app.js app/templates/htmx/base.html
git commit -m "feat: add trouble report button + form + JS error capture"
```

---

### Task 3: Wire Up, Deploy, Verify

- [ ] **Step 1: Add GitHub PAT to .env**

Add to `/root/availai/.env`:
```
GITHUB_TROUBLE_REPORT_TOKEN=<your-github-pat-with-repo-scope>
```

The user needs to create a GitHub Personal Access Token at https://github.com/settings/tokens with `repo` scope. Set it in `.env`.

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit all remaining changes**

Stage specific remaining modified files and commit:
```bash
git add app/routers/htmx_views.py
git commit -m "feat: trouble report button — wire up template global"
```

- [ ] **Step 4: Push and deploy**

```bash
git push origin main && docker compose up -d --build
```

- [ ] **Step 5: Verify deployment**

```bash
docker compose logs --tail=20 app
```
Expected: Clean startup, no errors.

- [ ] **Step 6: Test end-to-end**

Open the app in browser. Verify:
1. Bug button visible in bottom-right corner
2. Clicking opens modal with form
3. Typing description and submitting creates a GitHub Issue
4. Toast confirms success
5. Modal closes after brief delay
6. Issue appears in GitHub with correct labels and context
