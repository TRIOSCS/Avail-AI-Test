# Excess Resell Phase 4: Email Bid Solicitations + Auto-Parsing

## Overview

Add email-based bid solicitation workflow to the excess resell module. Users compose and send bid request emails to buyers via Graph API, with an AI polish option. Inbox scanner auto-detects replies and creates Bids as `status="pending"` with `source="email_parsed"` for human confirmation before accepting.

Scope: email solicitations + auto-parsing only. Stats cards, note tooltips, normalization display, and ProactiveMatch button are separate future work.

## Approach

Extend the existing RFQ email pipeline in `email_service.py` rather than creating a standalone module. The inbox scanner gets a new branch for `[EXCESS-BID-{id}]` tags alongside the existing `[ref:{id}]` RFQ tags. Same Graph API send pattern used by `send_batch_rfq()` and outreach.

## Email Sending Flow

1. User selects excess line items on detail page, clicks "Solicit Bids"
2. Modal opens with: recipient picker (search existing contacts OR type free-form email), pre-filled subject, message textarea, AI Clean Up button, and read-only parts table
3. User chooses bundled (default: one email with all items) or split (separate email per item) via checkbox
4. On send: POST to `POST /api/excess-lists/{id}/solicitations` with `require_fresh_token`
5. Service creates one BidSolicitation record per line item, builds HTML email with user message + auto-appended parts table
6. Subject tagged with `[EXCESS-BID-{solicitation_id}]` — for bundled mode, each BidSolicitation gets its own tag but they share one email. The first solicitation ID is used in the subject tag, and all solicitations in the bundle share the same `graph_message_id`
7. Sends via GraphClient. After `sendMail` returns 202 (no body), queries `/me/mailFolders/sentItems/messages?$orderby=sentDateTime desc&$top=5` to find the just-sent message by subject match (same pattern as `_find_sent_message()` in `email_service.py`). Stores the message's `id` as `graph_message_id` and `conversationId` for reply threading. Updates status to "sent"

### Parts Table (appended to user's message)

Simple HTML table matching existing RFQ email style:

| Part Number | Qty | Condition | Date Code | Asking Price |
|-------------|-----|-----------|-----------|--------------|

### AI Clean Up Button

- HTMX POST to `POST /api/excess-lists/polish-email`
- Request: `{"text": "user's draft"}`
- Response: `{"text": "polished version"}`
- Claude prompt: "Polish this business email for grammar and professional tone. Keep it concise. Don't change the meaning."
- Swaps textarea content with polished version
- Muted sparkle icon style — not the primary action

### Bundled vs Split

- **Bundled (default):** One email with all selected items in the parts table. One BidSolicitation record per line item, all sharing the same `graph_message_id`. The subject tag uses the first solicitation's ID: `[EXCESS-BID-{first_id}]`. When a reply is parsed, bids are created for all solicitations in the bundle. This is **new functionality** — the existing `send_bid_solicitation()` only supports split mode and must be extended.
- **Split:** One email per line item, each with its own subject tag and `graph_message_id`. This matches the current `send_bid_solicitation()` loop structure.

**Schema change:** `SendBidSolicitationRequest` gains a `bundled: bool = True` field.

## Solicitation Modal UI

Industrial/utilitarian aesthetic matching existing AvailAI style. Clean, dense, functional.

### Layout

```
┌─────────────────────────────────────────────────┐
│  Solicit Bids                              [X]  │
│─────────────────────────────────────────────────│
│  To:  [🔍 Search contacts or type email...    ] │
│        (dropdown: matching contacts)             │
│                                                  │
│  Subject: [Bid Request: Excess List #42       ] │
│                                                  │
│  ☐ Send separate email per item                 │
│                                                  │
│  Message:                                        │
│  ┌──────────────────────────────────────────┐   │
│  │ (textarea)                                │   │
│  └──────────────────────────────────────────┘   │
│                                    [✨ Clean Up] │
│                                                  │
│  Parts to include (read-only table):             │
│  MPN | Qty | Condition | Date Code | Ask Price  │
│                                                  │
│              [Cancel]  [Send Solicitation]        │
└─────────────────────────────────────────────────┘
```

### Interactions

- **Recipient field:** HTMX search — filters existing contacts by name/email, or accepts raw email address. For free-form emails with no matching contact, `contact_id` is set to `0` (the model uses a generic int FK with no constraint, and `0` is the established sentinel for "unknown/free-text")
- **Clean Up button:** HTMX POST, swaps textarea content
- **Parts table:** Read-only, auto-populated from selection
- **Send button:** Disabled until recipient + message filled. Shows spinner during send.

## Inbox Scanning & Auto-Parsing

**New code** added to the existing `poll_inbox()` in `email_service.py` (30-min scheduler cycle). The current `poll_inbox(token, db, requisition_id=None, scanned_by_user_id=None)` signature is RFQ-oriented — excess bid scanning ignores `requisition_id` and only uses `scanned_by_user_id` to scope which solicitations to check.

### Flow

1. **Tag detection:** After existing RFQ tag matching, check subjects for `[EXCESS-BID-{id}]` regex. This is new code — `poll_inbox()` currently has zero awareness of excess bid tags
2. **Match to BidSolicitation:** Look up by ID from tag. Also try `conversationId` matching against stored `graph_message_id` for replies that strip the subject tag
3. **Dedup:** Skip if `solicitation.status == "responded"` or message already in `ProcessedMessage`
4. **Parse with Claude:** Simple prompt — "Extract bid info from this email reply: unit_price, quantity_wanted, lead_time_days, notes. If declining, return {declined: true}"
5. **Create Bid as pending:** First extract structured fields (unit_price, quantity_wanted, lead_time_days, notes) from Claude's response, then call existing `parse_bid_response()` passing those fields. Creates Bid with `source="email_parsed"`, `status="pending"` (the Bid model's default — there is no "pending_review" status). Link via `solicitation.parsed_bid_id`
6. **Mark solicitation responded:** Update `status="responded"`, set `response_received_at`
7. **ActivityLog notification:** "New bid received (pending review): {vendor} — {part_number}"

### Lookback & Error Handling

- Only scan solicitations sent within last 14 days (configurable via `excess_bid_parse_lookback_days`)
- Parse failures: log warning, leave solicitation as "sent" — user can always manually enter the bid
- No data loss on failure

## Data Model & API Changes

### No New Models

`BidSolicitation` already exists with all required fields: `graph_message_id`, `parsed_bid_id`, `recipient_email`, `status`, etc.

### Service Changes (`app/services/excess_service.py`)

- `send_bid_solicitation()` → `async`, gains `token: str` param, adds Graph API send + sent message lookup. Significant rework: current function only creates records (split mode). Must add bundled mode code path and actual email sending via GraphClient

### Router Changes (`app/routers/excess.py`)

- `api_send_solicitations()` → `async def`, adds `token: str = Depends(require_fresh_token)`
- New endpoint in excess router: `POST /api/excess-lists/polish-email` — accepts `{"text": str}`, returns `{"text": str}`. Kept in excess router since it's specific to the solicitation workflow.

### Email Service Changes (`app/email_service.py`)

- In `poll_inbox()`: add `_EXCESS_BID_RE = re.compile(r"\[EXCESS-BID-(\d+)\]")` check after existing RFQ tag matching
- New function: `_handle_excess_bid_reply(msg, solicitation_id, db)` — calls Claude to extract structured bid fields from email body, then calls `parse_bid_response()` with those fields to create a pending Bid

### Config (`app/config.py`)

- `excess_bid_scan_enabled: bool = True`
- `excess_bid_parse_lookback_days: int = 14`

### Templates

- **New:** `excess/solicit_modal.html` — the solicitation modal
- **Modified:** `excess/detail.html` — add "Solicit Bids" button (enabled when line items selected)

## Testing Strategy

### Single test file: `tests/test_excess_solicitations.py`

**Send tests:**
- Send bundled solicitation — creates BidSolicitation records, one per line item
- Send split solicitation — creates separate records with individual subjects
- Recipient from existing contact vs free-form email
- Missing required fields returns 400
- Graph API send failure → BidSolicitation status="failed", error logged (no error_message column on model)

**AI Clean Up tests:**
- Polish endpoint returns cleaned text
- Mock Claude call, verify prompt includes original text
- Empty/whitespace input returns 400

**Inbox parse tests:**
- Message with `[EXCESS-BID-{id}]` tag → creates Bid with status="pending", source="email_parsed"
- Declined response → solicitation marked "responded", no Bid created
- Already-processed message skipped (dedup)
- Solicitation not found (bad ID) → logged and skipped
- Parse failure → solicitation stays "sent", warning logged
- Lookback window respected — old solicitations ignored

**Integration test:**
- Full round-trip: send solicitation → mock inbox reply → parse → Bid created → ActivityLog notification exists

All tests mock Graph API and Claude calls. No real API hits.
