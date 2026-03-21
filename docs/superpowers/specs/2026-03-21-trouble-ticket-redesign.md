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
- `screenshot` — base64 PNG (max 2MB)
- `page_url` — `window.location.href`
- `user_agent` — `navigator.userAgent`
- `viewport` — width x height
- `error_log` — JSON from existing `Alpine.store('errorLog').entries`
- `network_log` — last 10 HTMX request/response pairs from new Alpine store

### Submit flow:
1. POST JSON to `/api/trouble-tickets/submit`
2. Backend saves ticket, writes screenshot PNG to `uploads/tickets/TT-{id}.png`
3. Fires async Claude call for AI summary (non-blocking)
4. Toast confirmation: "Ticket TT-XXXX created"

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

### TroubleTicket — new columns (Alembic migration):
- `screenshot_path` — `String(255)`, nullable
- `ai_summary` — `Text`, nullable
- `root_cause_group_id` — FK to `root_cause_groups.id`, nullable
- `browser_info` — `Text`, nullable (JSON string)
- `network_log` — `Text`, nullable (JSON string)

Existing columns reused: `description`, `current_page`, `status`, `submitted_by`, `created_at`, `console_errors` (for JS error log).

### New table: `root_cause_groups`
- `id` — PK, auto-increment
- `title` — `String(200)`, not null (AI-generated root cause label)
- `suggested_fix` — `Text`, nullable (AI-generated fix suggestion)
- `status` — `String(30)`, default "open" (open / fixed / wont_fix)
- `created_at` — UTCTimestamp
- `updated_at` — UTCTimestamp

## 5. API Endpoints

### Existing (keep):
- `POST /api/trouble-tickets` — JSON API create
- `GET /api/trouble-tickets` — list
- `GET /api/trouble-tickets/{id}` — detail
- `PATCH /api/trouble-tickets/{id}` — update status

### Modified:
- `POST /api/trouble-tickets/submit` — HTMX form submit, now accepts screenshot + context fields
- `GET /api/trouble-tickets/form` — return the new form partial with screenshot preview

### New:
- `POST /api/trouble-tickets/analyze` — batch AI analysis (summarize + group by root cause)
- `GET /api/trouble-tickets/{id}/screenshot` — serve screenshot PNG from disk
- `GET /v2/partials/trouble-tickets/workspace` — HTMX list view partial
- `GET /v2/partials/trouble-tickets/{id}` — HTMX detail view partial

## 6. Management UI

### List view — `/v2/trouble-tickets`
- Bottom nav tab: "Tickets" (new, 12th tab)
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
- After ticket saved, background task calls Claude
- Prompt: "Summarize this trouble report in one sentence. Context: {description}, {page_url}, {js_errors}, {network_errors}"
- Updates `ai_summary` column
- Graceful failure: if Claude unavailable, summary stays null

### Batch analyze (on-demand):
- Triggered by "Analyze" button on list view
- Gathers all open tickets: descriptions, URLs, JS errors, network logs
- Single Claude call: "Group these trouble tickets by root cause. For each group provide a title and suggested fix. Return JSON: [{title, suggested_fix, ticket_ids}]"
- Creates/updates `RootCauseGroup` records, assigns `root_cause_group_id` on tickets
- Tickets not matching any group stay ungrouped

### No scheduled jobs. Analysis is manual/on-demand only.

## 8. File Storage

- Screenshots saved to `/app/uploads/tickets/TT-{id}.png`
- Directory created on first ticket if not exists
- Served via `GET /api/trouble-tickets/{id}/screenshot` (reads file, returns PNG)
- Docker volume mount needed for persistence: `./uploads:/app/uploads`

## 9. Frontend Dependencies

- `html2canvas` — loaded lazily via CDN or local copy on first button click
- No other new dependencies
- Uses existing: Alpine.js stores, HTMX, Tailwind, DM Sans font, btn-danger class

## 10. Migration Plan

One Alembic migration:
1. Add columns to `trouble_tickets`: `screenshot_path`, `ai_summary`, `root_cause_group_id`, `browser_info`, `network_log`
2. Create `root_cause_groups` table
3. Add FK constraint on `root_cause_group_id`
4. Add index on `root_cause_group_id`
