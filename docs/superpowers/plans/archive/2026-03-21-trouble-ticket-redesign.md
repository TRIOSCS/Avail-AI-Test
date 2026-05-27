# Trouble Ticket Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the floating bug icon with a header "Trouble Ticket" button that captures screenshots + browser context, adds AI summaries, and provides a management UI with root cause grouping.

**Architecture:** Extend existing `TroubleTicket` model (3 new columns + 1 new table via Alembic). Modify `error_reports.py` router for new submit flow. Add HTMX partials for management workspace. Screenshots saved to disk, AI summary via BackgroundTasks.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Jinja2, HTMX, Alpine.js, html2canvas (lazy-loaded), Claude API (claude_structured), nh3 (HTML sanitization)

**Spec:** `docs/superpowers/specs/2026-03-21-trouble-ticket-redesign.md`

---

### Task 1: Alembic Migration — New Columns + RootCauseGroup Table

**Files:**
- Modify: `app/models/trouble_ticket.py`
- Create: `app/models/root_cause_group.py`
- Modify: `app/models/__init__.py` (add RootCauseGroup import)
- Create: `alembic/versions/XXX_trouble_ticket_redesign.py` (auto-generated)

**Context:** The `TroubleTicket` model already has `screenshot_b64` (Text), `browser_info` (String(512)), `console_errors` (Text), `network_errors` (JSON). We add 3 new columns: `screenshot_path`, `ai_summary`, `root_cause_group_id`. We also create a new `RootCauseGroup` model/table.

- [ ] **Step 1: Create the RootCauseGroup model**

Create `app/models/root_cause_group.py`:

```python
"""Root cause grouping for trouble tickets — AI-generated categories.

Called by: routers/error_reports.py (batch analyze)
Depends on: models/base.py
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from .base import Base


class RootCauseGroup(Base):
    __tablename__ = "root_cause_groups"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    suggested_fix = Column(Text)
    status = Column(String(30), default="open", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
```

- [ ] **Step 2: Add new columns to TroubleTicket model**

In `app/models/trouble_ticket.py`, add after the `reproduction_steps` column (line 85):

```python
    # Trouble Ticket Redesign (2026-03-21)
    screenshot_path = Column(String(255))
    ai_summary = Column(Text)
    root_cause_group_id = Column(Integer, ForeignKey("root_cause_groups.id", ondelete="SET NULL"))

    root_cause_group = relationship("RootCauseGroup", foreign_keys=[root_cause_group_id])
```

- [ ] **Step 3: Add RootCauseGroup to models/__init__.py**

Add import:
```python
from .root_cause_group import RootCauseGroup
```

- [ ] **Step 4: Generate and review Alembic migration**

Run inside Docker:
```bash
docker compose exec app alembic revision --autogenerate -m "trouble ticket redesign: screenshot_path, ai_summary, root_cause_groups"
```

Review the generated migration — it should:
1. Create `root_cause_groups` table
2. Add `screenshot_path`, `ai_summary`, `root_cause_group_id` to `trouble_tickets`
3. Add FK constraint and index on `root_cause_group_id`

- [ ] **Step 5: Test migration round-trip**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

- [ ] **Step 6: Commit**

```bash
git add app/models/root_cause_group.py app/models/trouble_ticket.py app/models/__init__.py alembic/versions/
git commit -m "feat: add trouble ticket redesign schema — screenshot_path, ai_summary, root_cause_groups"
```

---

### Task 2: Docker Volume Mount + Screenshot Storage

**Files:**
- Modify: `docker-compose.yml`
- Modify: `app/routers/error_reports.py` (add screenshot serving endpoint)

**Context:** Screenshots are saved as PNG files to `/app/uploads/tickets/TT-{id}.png`. The `uploads` volume already exists in docker-compose.yml. We add a new endpoint to serve them.

- [ ] **Step 1: Verify uploads volume mount exists in docker-compose.yml**

The volume `uploads:/app/uploads` should already be in the `app` service. If not, add it.

- [ ] **Step 2: Add screenshot serving endpoint to error_reports.py**

Add this endpoint after the existing routes:

```python
import os

from fastapi.responses import FileResponse


@router.get("/api/trouble-tickets/{ticket_id}/screenshot")
async def get_ticket_screenshot(
    ticket_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Serve screenshot PNG from disk; fall back to legacy screenshot_b64."""
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    # Prefer disk file
    if ticket.screenshot_path and os.path.isfile(ticket.screenshot_path):
        return FileResponse(ticket.screenshot_path, media_type="image/png")

    # Fall back to legacy base64
    if ticket.screenshot_b64:
        import base64
        png_bytes = base64.b64decode(ticket.screenshot_b64)
        return Response(content=png_bytes, media_type="image/png")

    raise HTTPException(404, "No screenshot available")
```

Add `Response` to the existing fastapi imports.

- [ ] **Step 3: Add screenshot save helper**

Add to `error_reports.py`:

```python
import base64

UPLOAD_DIR = "/app/uploads/tickets"
MAX_SCREENSHOT_B64_SIZE = 2 * 1024 * 1024  # 2MB base64


def _save_screenshot(ticket_id: int, b64_data: str) -> str | None:
    """Decode base64 PNG and save to disk. Returns path or None on failure."""
    if not b64_data or len(b64_data) > MAX_SCREENSHOT_B64_SIZE:
        return None
    try:
        # Strip data URI prefix if present
        if "," in b64_data[:100]:
            b64_data = b64_data.split(",", 1)[1]
        png_bytes = base64.b64decode(b64_data)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, f"TT-{ticket_id}.png")
        with open(path, "wb") as f:
            f.write(png_bytes)
        return path
    except Exception:
        logger.warning("Failed to save screenshot for ticket %d", ticket_id)
        return None
```

- [ ] **Step 4: Write tests for screenshot endpoint and save helper**

Add to `tests/test_routers_error_reports.py`:

```python
class TestScreenshot:
    def test_screenshot_not_found(self, client):
        resp = client.get("/api/trouble-tickets/99999/screenshot")
        assert resp.status_code == 404

    def test_screenshot_no_screenshot(self, client, sample_report):
        resp = client.get(f"/api/trouble-tickets/{sample_report.id}/screenshot")
        assert resp.status_code == 404

    def test_screenshot_legacy_b64(self, client, sample_report, db_session):
        import base64
        sample_report.screenshot_b64 = base64.b64encode(b"fakepng").decode()
        db_session.commit()
        resp = client.get(f"/api/trouble-tickets/{sample_report.id}/screenshot")
        assert resp.status_code == 200
```

- [ ] **Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_error_reports.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/routers/error_reports.py docker-compose.yml tests/test_routers_error_reports.py
git commit -m "feat: add screenshot storage and serving for trouble tickets"
```

---

### Task 3: Redesign Submit Endpoint — JSON with Screenshot + Context

**Files:**
- Modify: `app/routers/error_reports.py`
- Modify: `tests/test_routers_error_reports.py`

**Context:** The current submit endpoint accepts form-encoded data (message + current_url). The redesign changes it to accept JSON with screenshot, browser context, error log, and network log. The old form endpoint stays for backwards compatibility but the new form will POST JSON.

- [ ] **Step 1: Add new JSON submit schema**

Add to `error_reports.py` after existing schemas:

```python
class TroubleTicketSubmit(BaseModel):
    description: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    screenshot: Optional[str] = Field(None, max_length=3_000_000)  # base64 PNG
    page_url: Optional[str] = Field(None, max_length=500)
    user_agent: Optional[str] = Field(None, max_length=500)
    viewport: Optional[str] = Field(None, max_length=50)
    error_log: Optional[str] = Field(None, max_length=50_000)  # JSON string
    network_log: Optional[str] = Field(None, max_length=50_000)  # JSON string
```

- [ ] **Step 2: Modify _create_ticket to accept new fields**

Update `_create_ticket` signature and body:

```python
def _create_ticket(
    db: Session,
    user_id: int,
    message: str,
    current_url: Optional[str] = None,
    user_agent: Optional[str] = None,
    browser_info: Optional[str] = None,
    console_errors: Optional[str] = None,
    network_errors: Optional[str] = None,
) -> TroubleTicket:
    ticket = TroubleTicket(
        ticket_number="PENDING",
        submitted_by=user_id,
        title=message[:120],
        description=message,
        current_page=current_url or None,
        user_agent=user_agent or None,
        browser_info=browser_info or None,
        console_errors=console_errors or None,
        network_errors=network_errors if network_errors else None,
        source="report_button",
        status="submitted",
        risk_tier="low",
        category="other",
        created_at=datetime.now(timezone.utc),
    )
    db.add(ticket)
    db.flush()
    ticket.ticket_number = f"TT-{ticket.id:04d}"
    db.commit()
    logger.info("Trouble ticket %s created by user %d", ticket.ticket_number, user_id)
    return ticket
```

- [ ] **Step 3: Add new JSON submit route**

Replace the existing `POST /api/trouble-tickets/submit` handler. Keep the old form handler but rename it to avoid conflicts:

```python
@router.post("/api/trouble-tickets/submit")
async def submit_trouble_ticket(
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Handle JSON submission from redesigned trouble ticket form."""
    try:
        body = await request.json()
    except Exception:
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Invalid request.</div>',
            status_code=422,
        )

    description = (body.get("description") or "").strip()
    if not description:
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Please describe the problem.</div>',
            status_code=422,
        )
    if len(description) > MAX_MESSAGE_LEN:
        return HTMLResponse(
            f'<div class="p-4 text-rose-600 text-sm">Message too long (max {MAX_MESSAGE_LEN} characters).</div>',
            status_code=422,
        )

    # Build browser_info JSON
    browser_info = None
    if body.get("user_agent") or body.get("viewport"):
        import json as _json
        browser_info = _json.dumps({"user_agent": body.get("user_agent"), "viewport": body.get("viewport")})

    # Parse network_log as JSON if it's a string
    network_errors = None
    if body.get("network_log"):
        try:
            import json as _json
            network_errors = _json.loads(body["network_log"]) if isinstance(body["network_log"], str) else body["network_log"]
        except (ValueError, TypeError):
            network_errors = None

    try:
        ticket = _create_ticket(
            db, user.id, description,
            current_url=body.get("page_url"),
            user_agent=body.get("user_agent"),
            browser_info=browser_info,
            console_errors=body.get("error_log"),
            network_errors=network_errors,
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to create trouble ticket for user %d", user.id)
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Something went wrong saving your report. Please try again.</div>',
            status_code=500,
        )

    # Save screenshot to disk (non-blocking for the response)
    if body.get("screenshot"):
        path = _save_screenshot(ticket.id, body["screenshot"])
        if path:
            ticket.screenshot_path = path
            db.commit()

    # Queue async AI summary
    background_tasks.add_task(_generate_ai_summary, ticket.id)

    return HTMLResponse(
        '<div class="p-4 text-center">'
        '<div class="text-emerald-600 font-medium mb-2">Report submitted!</div>'
        f'<div class="text-sm text-gray-500 mb-3">Ticket {escape(ticket.ticket_number)}</div>'
        '<button type="button" @click="$dispatch(\'close-modal\')" '
        'class="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Close</button>'
        "</div>"
    )
```

Add `BackgroundTasks` to the fastapi imports.

- [ ] **Step 4: Add AI summary background task**

Add to `error_reports.py`:

```python
async def _generate_ai_summary(ticket_id: int):
    """Generate a one-sentence AI summary for a trouble ticket. Runs as BackgroundTask."""
    from ..database import SessionLocal
    from ..utils.claude_client import claude_text

    db = SessionLocal()
    try:
        ticket = db.get(TroubleTicket, ticket_id)
        if not ticket or ticket.ai_summary:
            return

        prompt = (
            "Summarize this trouble report in one sentence. "
            f"Description: {ticket.description[:500]}. "
            f"Page: {ticket.current_page or 'unknown'}. "
            f"JS errors: {(ticket.console_errors or 'none')[:300]}. "
            f"Network errors: {str(ticket.network_errors or 'none')[:300]}"
        )

        summary = await claude_text(
            prompt=prompt,
            system="You are a bug report summarizer. Return exactly one sentence.",
            model_tier="fast",
        )

        if summary:
            ticket.ai_summary = summary.strip()[:500]
            ticket.updated_at = datetime.now(timezone.utc)
            db.commit()
            logger.debug("AI summary generated for ticket %s", ticket.ticket_number)
    except Exception:
        logger.warning("AI summary failed for ticket %d", ticket_id)
        db.rollback()
    finally:
        db.close()
```

- [ ] **Step 5: Write tests for new submit flow**

Add to `tests/test_routers_error_reports.py`:

```python
class TestNewSubmitFlow:
    def test_json_submit_minimal(self, client):
        resp = client.post("/api/trouble-tickets/submit",
            json={"description": "Button doesn't work"},
            headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        assert "Report submitted" in resp.text

    def test_json_submit_with_context(self, client):
        resp = client.post("/api/trouble-tickets/submit",
            json={
                "description": "Search results empty",
                "page_url": "/v2/search",
                "user_agent": "Mozilla/5.0",
                "viewport": "1920x1080",
                "error_log": '[{"msg":"TypeError","ts":"2026-03-21"}]',
                "network_log": '[{"url":"/api/search","status":500}]',
            },
            headers={"Content-Type": "application/json"})
        assert resp.status_code == 200

    def test_json_submit_empty_description_422(self, client):
        resp = client.post("/api/trouble-tickets/submit",
            json={"description": ""},
            headers={"Content-Type": "application/json"})
        assert resp.status_code == 422

    def test_json_submit_no_body_422(self, client):
        resp = client.post("/api/trouble-tickets/submit",
            content=b"not json",
            headers={"Content-Type": "application/json"})
        assert resp.status_code == 422
```

- [ ] **Step 6: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_error_reports.py -v`
Expected: All PASS (old form tests may need the Content-Type adjusted to avoid conflict)

- [ ] **Step 7: Commit**

```bash
git add app/routers/error_reports.py tests/test_routers_error_reports.py
git commit -m "feat: redesign trouble ticket submit — JSON with screenshot, context, AI summary"
```

---

### Task 4: Network Log Store + Header Button + Redesigned Form

**Files:**
- Modify: `app/static/htmx_app.js` (add network log store)
- Modify: `app/templates/htmx/base.html` (replace spacer with button)
- Modify: `app/templates/htmx/partials/shared/trouble_report_button.html` (remove floating button)
- Modify: `app/templates/htmx/partials/shared/trouble_report_form.html` (redesign form)

**Context:** The floating rose-red button at bottom-right is replaced by a red button in the header's right 140px column. The form now captures a screenshot via html2canvas before opening the modal, and sends JSON with all context fields.

- [ ] **Step 1: Add network log Alpine store to htmx_app.js**

After the existing `Alpine.store('errorLog', ...)` block (~line 113), add:

```javascript
// ── Network log capture for trouble tickets ──────────────────
Alpine.store('networkLog', { entries: [] });

htmx.on('htmx:afterRequest', function(evt) {
    var log = Alpine.store('networkLog').entries;
    log.push({
        url: evt.detail.pathInfo.requestPath,
        method: evt.detail.requestConfig.verb.toUpperCase(),
        status: evt.detail.xhr.status,
        ts: new Date().toISOString()
    });
    if (log.length > 10) log.shift();
});
```

- [ ] **Step 2: Replace header spacer with Trouble Ticket button**

In `app/templates/htmx/base.html`, replace the right-column spacer (line 80-81):

```html
      {# Right: spacer #}
      <div></div>
```

with:

```html
      {# Right: Trouble Ticket button #}
      <div class="flex items-center justify-end"
           x-data="{ capturing: false }">
        <button
          :disabled="capturing"
          @click="
            capturing = true;
            if (!window._html2canvasLoaded) {
              var s = document.createElement('script');
              s.src = 'https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js';
              s.onload = function() { window._html2canvasLoaded = true; $dispatch('_tt-capture'); };
              s.onerror = function() { capturing = false; $dispatch('_tt-capture'); };
              document.head.appendChild(s);
            } else {
              $dispatch('_tt-capture');
            }
          "
          @_tt-capture.window="
            var doCapture = window.html2canvas ? html2canvas(document.body, {scale:0.5, logging:false, useCORS:true}).then(function(c){return c.toDataURL('image/png')}) : Promise.resolve(null);
            doCapture.then(function(img){
              window._ttScreenshot = img;
              htmx.ajax('GET', '/api/trouble-tickets/form', {target:'#modal-content'});
              $dispatch('open-modal');
              capturing = false;
            }).catch(function(){
              window._ttScreenshot = null;
              htmx.ajax('GET', '/api/trouble-tickets/form', {target:'#modal-content'});
              $dispatch('open-modal');
              capturing = false;
            });
          "
          class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-rose-500 text-white hover:bg-rose-600 disabled:opacity-50 transition-colors">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 12.75c1.148 0 2.278.08 3.383.237 1.037.146 1.866.966 1.866 2.013 0 3.728-2.35 6.75-5.25 6.75S6.75 18.728 6.75 15c0-1.046.83-1.867 1.866-2.013A24.204 24.204 0 0112 12.75zm0 0c2.883 0 5.647.508 8.207 1.44a23.91 23.91 0 01-1.152-6.135c-.117-1.09-.564-2.055-1.24-2.647-.676-.593-1.548-.858-2.44-.658C14.26 5.053 13.16 5.25 12 5.25s-2.26-.197-3.375-.5c-.892-.2-1.764.065-2.44.658-.676.592-1.123 1.558-1.24 2.647a23.907 23.907 0 01-1.152 6.135C6.354 13.258 9.118 12.75 12 12.75z"/>
          </svg>
          <span x-show="!capturing">Trouble Ticket</span>
          <span x-show="capturing" x-cloak>Capturing...</span>
        </button>
      </div>
```

- [ ] **Step 3: Remove floating button**

Replace content of `app/templates/htmx/partials/shared/trouble_report_button.html` with:

```html
{# Trouble report button moved to header — this file kept empty for backwards compat #}
```

- [ ] **Step 4: Redesign the form template**

Replace `app/templates/htmx/partials/shared/trouble_report_form.html` with a form that:
- Shows screenshot preview thumbnail from `window._ttScreenshot`
- Has textarea for "What went wrong?"
- On submit, uses `fetch()` to POST JSON to `/api/trouble-tickets/submit` with all captured context
- Uses Alpine.js for submitting state
- Swaps the response HTML into `#modal-content` via DOM assignment (safe — response is server-rendered HTML from our own endpoint)

Key fields in the JSON payload:
- `description` — from textarea
- `screenshot` — from `window._ttScreenshot`
- `page_url` — from `window.location.href`
- `user_agent` — from `navigator.userAgent`
- `viewport` — from `window.innerWidth + 'x' + window.innerHeight`
- `error_log` — JSON string from `Alpine.store('errorLog').entries`
- `network_log` — JSON string from `Alpine.store('networkLog').entries`

- [ ] **Step 5: Commit**

```bash
git add app/static/htmx_app.js app/templates/htmx/base.html app/templates/htmx/partials/shared/trouble_report_button.html app/templates/htmx/partials/shared/trouble_report_form.html
git commit -m "feat: header trouble ticket button with screenshot capture and context"
```

---

### Task 5: Management UI — List View

**Files:**
- Create: `app/templates/htmx/partials/tickets/workspace.html`
- Create: `app/templates/htmx/partials/tickets/list.html`
- Create: `app/templates/htmx/partials/tickets/_row.html`
- Modify: `app/routers/htmx_views.py` (add HTMX routes)
- Modify: `app/templates/htmx/base.html` (add bottom nav tab)

**Context:** The management UI follows the existing workspace pattern — a full-page route that renders the workspace partial, which contains the list partial. Filter pills for status, table rows clickable to detail view. Root cause groups shown as collapsible headers.

- [ ] **Step 1: Add HTMX routes for ticket workspace**

Add to `app/routers/htmx_views.py`:

```python
# ── Trouble Tickets ──────────────────────────────────────────────
@router.get("/v2/trouble-tickets", response_class=HTMLResponse)
async def trouble_tickets_page(request: Request, user: User = Depends(require_user)):
    return _full_page(request, "htmx/partials/tickets/workspace.html", current_view="tickets")

@router.get("/v2/partials/trouble-tickets/workspace", response_class=HTMLResponse)
async def trouble_tickets_workspace(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse("htmx/partials/tickets/workspace.html", {"request": request})

@router.get("/v2/partials/trouble-tickets/list", response_class=HTMLResponse)
async def trouble_tickets_list(
    request: Request,
    status: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from app.models.trouble_ticket import TroubleTicket
    from app.models.root_cause_group import RootCauseGroup

    q = db.query(TroubleTicket).filter(TroubleTicket.source == "report_button")
    if status:
        q = q.filter(TroubleTicket.status == status)
    q = q.order_by(desc(TroubleTicket.created_at))
    tickets = q.limit(200).all()
    total = q.count()

    # Load root cause groups for grouped display
    groups = db.query(RootCauseGroup).order_by(RootCauseGroup.title).all()
    grouped = {}
    ungrouped = []
    for t in tickets:
        if t.root_cause_group_id:
            grouped.setdefault(t.root_cause_group_id, []).append(t)
        else:
            ungrouped.append(t)

    return templates.TemplateResponse("htmx/partials/tickets/list.html", {
        "request": request,
        "tickets": tickets,
        "total": total,
        "groups": groups,
        "grouped": grouped,
        "ungrouped": ungrouped,
        "current_status": status,
    })
```

- [ ] **Step 2: Create workspace template**

Create `app/templates/htmx/partials/tickets/workspace.html` with:
- Title "Trouble Tickets"
- "Analyze" button (POST to `/api/trouble-tickets/analyze`, targets `#ticket-list`)
- Filter pills: All / Open / Resolved / Won't Fix
- `#ticket-list` div that loads on init via `hx-get="/v2/partials/trouble-tickets/list"`

- [ ] **Step 3: Create list template**

Create `app/templates/htmx/partials/tickets/list.html` with:
- Total count
- Root cause groups as collapsible sections (amber styling, chevron toggle)
- Each group shows title, ticket count, suggested fix preview
- Ungrouped tickets listed below
- Each ticket rendered via `_row.html` include

Create `app/templates/htmx/partials/tickets/_row.html` with:
- Clickable row (hx-get to detail, hx-push-url)
- Ticket number (mono font), AI summary or title, status badge (color-coded), date

- [ ] **Step 4: Add bottom nav tab**

In `app/templates/htmx/base.html`, add a "Tickets" entry to the `bottom_items` list (before `settings`):

```python
        ('tickets', 'Tickets', '/v2/trouble-tickets', '/v2/partials/trouble-tickets/workspace', 'M12 12.75c1.148 0 2.278.08 3.383.237 ...'),
```

Use a bug icon SVG path for the tab.

- [ ] **Step 5: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/base.html app/templates/htmx/partials/tickets/
git commit -m "feat: trouble ticket management UI — list view with grouping and filters"
```

---

### Task 6: Management UI — Detail View

**Files:**
- Create: `app/templates/htmx/partials/tickets/detail.html`
- Modify: `app/routers/htmx_views.py` (add detail route)

**Context:** Detail view shows screenshot, AI summary, description, captured context (collapsible), and a status dropdown. Follows existing detail view patterns (e.g., excess detail, vendor detail).

- [ ] **Step 1: Add detail routes**

Add to `app/routers/htmx_views.py`:

```python
@router.get("/v2/trouble-tickets/{ticket_id}", response_class=HTMLResponse)
async def trouble_ticket_detail_page(
    request: Request, ticket_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return _full_page(request, "htmx/partials/tickets/detail.html",
                      current_view="tickets", ticket=ticket)

@router.get("/v2/partials/trouble-tickets/{ticket_id}", response_class=HTMLResponse)
async def trouble_ticket_detail(
    request: Request, ticket_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return templates.TemplateResponse("htmx/partials/tickets/detail.html",
                                      {"request": request, "ticket": ticket})
```

- [ ] **Step 2: Create detail template**

Create `app/templates/htmx/partials/tickets/detail.html` with:
- Back button to ticket list
- Header: ticket number, submitted date/by
- Status dropdown (select element, fetches PATCH on change)
- AI summary card (brand-50 background, sparkle icon)
- Root cause group badge (if assigned)
- Screenshot image (from `/api/trouble-tickets/{id}/screenshot`, click to open full size)
- Description block
- Collapsible "Captured Context" section: page URL, browser info, JS errors, network log (pre-formatted)

- [ ] **Step 3: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/tickets/detail.html
git commit -m "feat: trouble ticket detail view with screenshot, AI summary, context"
```

---

### Task 7: Batch AI Analysis Endpoint

**Files:**
- Modify: `app/routers/error_reports.py` (add analyze endpoint)
- Modify: `tests/test_routers_error_reports.py`

**Context:** The "Analyze" button on the list view POSTs to `/api/trouble-tickets/analyze`. This gathers up to 50 open tickets, sends them to Claude for root cause grouping, and creates/updates RootCauseGroup records.

- [ ] **Step 1: Add analyze endpoint**

Add to `app/routers/error_reports.py`:

```python
@router.post("/api/trouble-tickets/analyze", response_class=HTMLResponse)
async def analyze_tickets(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Batch AI analysis — group open tickets by root cause."""
    from ..models.root_cause_group import RootCauseGroup
    from ..utils.claude_client import claude_structured

    tickets = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.status.in_(["submitted", "in_progress"]))
        .filter(TroubleTicket.source == "report_button")
        .order_by(desc(TroubleTicket.created_at))
        .limit(50)
        .all()
    )

    if not tickets:
        return HTMLResponse(
            '<div class="text-center py-4 text-sm text-gray-500">No open tickets to analyze.</div>'
        )

    # Build ticket summaries for Claude
    ticket_data = []
    for t in tickets:
        ticket_data.append({
            "id": t.id,
            "description": (t.description or "")[:300],
            "page": t.current_page or "",
            "js_errors": (t.console_errors or "")[:200],
            "network": str(t.network_errors or "")[:200],
        })

    tool_schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                        "ticket_ids": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["title", "ticket_ids"],
                },
            }
        },
        "required": ["groups"],
    }

    import json as _json
    result = await claude_structured(
        prompt=(
            "Group these trouble tickets by root cause. For each group, provide a short title "
            "and a suggested fix. Return JSON with a 'groups' array.\n\n"
            f"Tickets:\n{_json.dumps(ticket_data, indent=2)}"
        ),
        system="You are a bug triage assistant. Group related bug reports by their likely root cause.",
        output_schema=tool_schema,
        model_tier="fast",
    )

    if not result or "groups" not in result:
        return HTMLResponse(
            '<div class="text-center py-4 text-sm text-amber-600">AI analysis returned no results. Try again later.</div>'
        )

    # Create/update groups and assign tickets
    ticket_map = {t.id: t for t in tickets}
    for group_data in result["groups"]:
        title = (group_data.get("title") or "Unknown")[:200]
        fix = group_data.get("suggested_fix")
        ticket_ids = group_data.get("ticket_ids", [])

        # Find or create group by title
        group = db.query(RootCauseGroup).filter(RootCauseGroup.title == title).first()
        if not group:
            group = RootCauseGroup(title=title, suggested_fix=fix)
            db.add(group)
            db.flush()
        elif fix and not group.suggested_fix:
            group.suggested_fix = fix

        for tid in ticket_ids:
            if tid in ticket_map:
                ticket_map[tid].root_cause_group_id = group.id

    db.commit()
    logger.info("AI analysis grouped %d tickets into %d groups", len(tickets), len(result["groups"]))

    # Trigger HTMX to reload the list
    resp = HTMLResponse("")
    resp.headers["HX-Trigger"] = "ticketsUpdated"
    return resp
```

- [ ] **Step 2: Write tests**

Add to `tests/test_routers_error_reports.py`:

```python
class TestBatchAnalyze:
    def test_analyze_no_tickets(self, client):
        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "No open tickets" in resp.text

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_analyze_groups_tickets(self, mock_claude, client, sample_report, db_session):
        mock_claude.return_value = {
            "groups": [{"title": "Search Bug", "suggested_fix": "Fix query", "ticket_ids": [sample_report.id]}]
        }
        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_error_reports.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add app/routers/error_reports.py tests/test_routers_error_reports.py
git commit -m "feat: batch AI analysis endpoint for trouble ticket root cause grouping"
```

---

### Task 8: Final Integration Tests + Cleanup

**Files:**
- Modify: `tests/test_routers_error_reports.py`

**Context:** Run full test suite, verify no regressions, clean up any issues.

- [ ] **Step 1: Run targeted tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_error_reports.py tests/test_error_reports.py -v`
Expected: All PASS

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=line`
Expected: No new failures

- [ ] **Step 3: Commit any fixes**

```bash
git add -u && git commit -m "test: fix any regressions from trouble ticket redesign"
```

- [ ] **Step 4: Deploy**

```bash
cd /root/availai && git push origin main && docker compose up -d --build
```
