# Trouble Ticket Redesign — Spec

**Date**: 2026-03-21
**Status**: Approved

## Overview

Replace the floating bug icon with a red "Trouble Ticket" button in the header. On click, capture a screenshot + browser context, let the user describe the issue, save to DB with AI summary. Provide a management UI with AI-powered root cause grouping.

## 1. Header Button

- Replace the empty `<div></div>` spacer in the header's right 140px column
- Red button using existing `btn-danger` style, small size
- Text: "Trouble Ticket" with a bug icon (Heroicons)
- On click: capture screenshot FIRST, then open modal

## 2. Capture & Modal Flow

### Click sequence:
1. `html2canvas(document.body)` captures the page as base64 PNG
2. Modal opens with existing `@open-modal` / `#modal-content` pattern
3. Form shows:
   - Screenshot preview thumbnail
   - Textarea: "What went wrong?"
   - Submit button

### Auto-captured hidden fields:
- `screenshot` — base64 PNG (max 2MB, validated server-side)
- `page_url` — `window.location.href`
- `user_agent` — `navigator.userAgent`
- `viewport` — width x height
- `error_log` — JSON from existing `Alpine.store('errorLog').entries`
- `network_log` — last 10 HTMX request/response pairs from new Alpine store

### Submit flow:
1. POST JSON to `/api/trouble-tickets/submit` (content type: `application/json`)
2. Backend validates screenshot size (max 2MB base64), decodes to PNG, saves to `uploads/tickets/TT-{id}.png`
3. If screenshot write fails (disk full, permissions), ticket still saves — `screenshot_path` stays null
4. Title auto-generated: `description[:120]` (same as current behavior)
5. Fires async Claude call for AI summary via `FastAPI BackgroundTasks`
6. Returns HTML response for HTMX swap: toast confirmation "Ticket TT-XXXX created"

### html2canvas:
- Loaded lazily on first button click (~40KB)
- Not bundled in main JS

## 3. Network Log Capture

New Alpine store in `htmx_app.js`:

```javascript
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

## 4. Data Model Changes

### TroubleTicket — column changes (Alembic migration):

**New columns:**
- `screenshot_path` — `String(255)`, nullable (replaces legacy `screenshot_b64`; old column left as-is for backwards compat)
- `ai_summary` — `Text`, nullable
- `root_cause_group_id` — FK to `root_cause_groups.id`, nullable

**Existing columns reused (no migration needed):**
- `description`, `current_page`, `status`, `submitted_by`, `created_at` — as-is
- `console_errors` — `Text`, stores JS error log JSON
- `browser_info` — `String(512)`, already exists, stores user agent + viewport JSON
- `network_errors` — `JSON`, already exists, reused for network log capture (renamed conceptually to "network log" in the UI but same column)

### New table: `root_cause_groups`
- `id` — PK, auto-increment
- `title` — `String(200)`, not null (AI-generated root cause label)
- `suggested_fix` — `Text`, nullable (AI-generated fix suggestion)
- `status` — `String(30)`, default "open" (open / fixed / wont_fix)
- `created_at` — UTCTimestamp
- `updated_at` — UTCTimestamp

## 5. API Endpoints

### Existing (keep as-is):
- `POST /api/error-reports` / `POST /api/trouble-tickets` — JSON API create (legacy)
- `GET /api/error-reports` / `GET /api/trouble-tickets` — list
- `GET /api/error-reports/{id}` / `GET /api/trouble-tickets/{id}` — detail
- `PATCH /api/error-reports/{id}` / `PATCH /api/trouble-tickets/{id}` — update status

### Modified:
- `POST /api/trouble-tickets/submit` — now accepts JSON with screenshot + context fields (was form-encoded; old form contract replaced)
- `GET /api/trouble-tickets/form` — return the new form partial with screenshot preview slot

### New:
- `POST /api/trouble-tickets/analyze` — batch AI analysis (summarize + group), max 50 tickets per call
- `GET /api/trouble-tickets/{id}/screenshot` — serve screenshot PNG from disk (falls back to `screenshot_b64` for legacy tickets)
- `GET /v2/trouble-tickets` — full-page route, renders `base.html` with workspace partial
- `GET /v2/partials/trouble-tickets/workspace` — HTMX list view partial
- `GET /v2/partials/trouble-tickets/{id}` — HTMX detail view partial

## 6. Management UI

### List view — `/v2/trouble-tickets`
- Bottom nav tab: "Tickets" (new tab; consider consolidating with an existing low-use tab if 12 is too many)
- Table columns: Ticket #, AI Summary, Status, Page, Submitted By, Date
- Filter pills: All / Open / Resolved / Won't Fix
- Root cause grouping: collapsible headers showing group title + ticket count + suggested fix
- Ungrouped tickets listed individually
- "Analyze" button in header — triggers `POST /api/trouble-tickets/analyze`
- Click row → detail view

### Detail view — `/v2/trouble-tickets/{id}`
- Screenshot (full size, click to expand in lightbox)
- AI summary (prominent, top)
- User description
- Collapsible "Captured Context" section: page URL, browser info, JS errors, network log
- Status dropdown: submitted → in_progress → resolved / wont_fix
- Root cause group badge (if assigned, links to group)

## 7. AI Integration

### Per-ticket summary (on creation, async):
- After ticket saved, `FastAPI BackgroundTasks` calls Claude
- Prompt: "Summarize this trouble report in one sentence. Context: {description}, {page_url}, {js_errors}, {network_errors}"
- Updates `ai_summary` column
- Graceful failure: if Claude unavailable, summary stays null — no retry

### Batch analyze (on-demand):
- Triggered by "Analyze" button on list view
- Gathers open tickets (max 50 most recent) with descriptions, URLs, JS errors, network logs
- Single Claude call: "Group these trouble tickets by root cause. For each group provide a title and suggested fix. Return JSON: [{title, suggested_fix, ticket_ids}]"
- Creates/updates `RootCauseGroup` records, assigns `root_cause_group_id` on tickets
- Tickets not matching any group stay ungrouped
- If >50 open tickets, UI shows warning "Analyzing 50 most recent tickets"

### No scheduled jobs. Analysis is manual/on-demand only.

## 8. File Storage

- Screenshots saved to `/app/uploads/tickets/TT-{id}.png`
- Directory created on first ticket if not exists
- Served via `GET /api/trouble-tickets/{id}/screenshot` (reads file, returns PNG; falls back to `screenshot_b64` for legacy)
- If screenshot write fails, ticket still saves — screenshot_path stays null, logged as warning
- Server-side validation: reject base64 payloads > 2MB before decoding
- Docker volume mount for persistence: `./uploads:/app/uploads` (added to docker-compose.yml)

## 9. Frontend Dependencies

- `html2canvas` — loaded lazily via CDN or local copy on first button click
- No other new dependencies
- Uses existing: Alpine.js stores, HTMX, Tailwind, DM Sans font, btn-danger class

## 10. Migration & Deployment

### Alembic migration:
1. Add columns to `trouble_tickets`: `screenshot_path`, `ai_summary`, `root_cause_group_id`
2. Create `root_cause_groups` table
3. Add FK constraint on `root_cause_group_id`
4. Add index on `root_cause_group_id`

(Note: `browser_info`, `network_errors`, `console_errors` already exist — no migration needed)

### Docker:
- Add volume mount `./uploads:/app/uploads` to `docker-compose.yml`

### Deployment:
- Standard: `docker compose up -d --build` (entrypoint runs `alembic upgrade head`)
