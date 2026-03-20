# Excess Resell Phase 4 — Email Solicitations, Stats, Polish

## Problem

Phase 3 delivered the core excess resell workflow (import, demand matching, bid recording). But the team still has to manually email potential buyers and manually enter bid responses. The list view lacks summary stats, line items don't show notes or normalized MPNs, and closed lists can't trigger proactive matching from the UI.

## Design

### 1. Email Bid Solicitations via Graph API

**Flow:** User selects line items on detail page → clicks "Solicit Bids" → modal with recipient email/name, subject, message → sends real email via Graph API → stores `graph_message_id` on BidSolicitation → shows solicitation count badge on line items.

**Service changes (`app/services/excess_service.py`):**

`send_bid_solicitation()` gains an `async` signature and `token: str` parameter:

```python
async def send_bid_solicitation(
    db, *, token: str, list_id, line_item_ids, recipient_email,
    recipient_name, contact_id, user_id, subject=None, message=None,
) -> list[BidSolicitation]:
```

**Auth wiring:** The HTMX route uses `token: str = Depends(require_fresh_token)` to get a valid M365 OAuth token, then passes it to the service. This is the same pattern used by `send_batch_rfq()` in `email_service.py`.

**`contact_id` handling:** `BidSolicitation.contact_id` is NOT NULL in the model. When the user provides a free-text email without a known contact, pass `contact_id=0` as a sentinel value. The field is a generic integer (no FK constraint), so 0 is safe and distinguishable from real contact IDs.

For each line item:
1. Create BidSolicitation record first (to get the ID for subject tagging)
2. Build HTML email body using the same `_build_html_body()` pattern from `email_service.py`
3. Tag subject with `[EXCESS-BID-{solicitation_id}]` for response matching
4. Send via `GraphClient(token).post_json("/me/sendMail", payload)`
5. Store `graph_message_id` on the BidSolicitation record
6. On send failure: set `status="failed"`, log error, continue to next item

**Email template:** Plain HTML with a parts table (MPN, Qty, Condition, Date Code, Asking Price) and a message body. No separate template file — inline HTML construction like RFQ emails. Include company signature if available.

**Subject format:** `"Bid Request: {part_number} x {qty} — {list_title} [EXCESS-BID-{solicitation_id}]"`

**No multi-item grouping** — one email per line item per recipient. This keeps the `[EXCESS-BID-{id}]` tag 1:1 with a BidSolicitation record, making inbox response matching unambiguous. If the user selects 3 items, 3 emails are sent with 3 separate tags.

**UI — Solicit Modal (`app/templates/htmx/partials/excess/solicit_modal.html`):**

- Trigger: "Solicit Bids" button in detail page action bar (visible when list status is active or bidding)
- Form fields:
  - Recipient Email (required, type=email)
  - Recipient Name (optional)
  - Subject (pre-filled with default, editable)
  - Message (textarea, pre-filled with default bid request text, editable)
- Selected parts summary table (read-only: MPN, Qty, Condition, Price)
- Submit sends `POST /api/excess-lists/{id}/solicitations`
- On success: close modal, re-render detail page, show success banner

**UI — Line item row enhancement:**

Add solicitation count badge next to bids badge in `line_item_row.html`:
```
📧 2 sent  |  💰 3 bids
```
Show as a small pill with envelope icon when `item.solicitations|length > 0`.

**Router changes (`app/routers/excess.py`):**

- Update `POST /api/excess-lists/{list_id}/solicitations` to accept `token` from request auth and pass to service
- Add HTMX route: `GET /v2/partials/excess/{list_id}/solicit-form?item_ids=1,2,3` → renders solicit modal with selected items
- Add HTMX route: `POST /v2/partials/excess/{list_id}/solicit` → calls service, re-renders detail

### 2. Auto-Parse Bid Responses from Inbox

**Flow:** Scheduler inbox scan finds reply with `[EXCESS-BID-{id}]` in subject → match to BidSolicitation → extract bid data via Claude → auto-create Bid with `source="email_parsed"`.

**New function in `app/jobs/email_jobs.py`:**

```python
_EXCESS_BID_RE = re.compile(r"\[EXCESS-BID-(\d+)\]")

async def _scan_excess_bid_responses(user, db):
    """Scan inbox for replies to excess bid solicitations."""
```

Called from `_scan_user_inbox()` after existing scans. Steps:
1. Query BidSolicitations where `sent_by=user.id`, `status="sent"`, `sent_at` within last 14 days
2. If none, return early
3. Use Graph API to search user's inbox: `$filter=contains(subject, '[EXCESS-BID-')&$top=50`
4. For each message matching `_EXCESS_BID_RE`:
   a. Look up BidSolicitation by ID
   b. Skip if already `status="responded"`
   c. Extract email body text
   d. Call Claude with focused prompt to extract: `unit_price`, `quantity_wanted`, `lead_time_days`, `notes`
   e. Call `parse_bid_response()` to create Bid and update solicitation status
   f. Log result

**New service function (`app/services/excess_service.py`):**

```python
async def parse_bid_from_email(db, solicitation_id: int, email_body: str) -> Bid | None:
    """Use Claude to extract bid data from email body, create Bid record."""
```

Claude prompt (simple, focused):
```
Extract the bid information from this email response to a parts solicitation.
Return JSON: {"unit_price": float|null, "quantity_wanted": int|null, "lead_time_days": int|null, "notes": str|null}
If the email is not a bid response or declines to bid, return {"declined": true}.
```

On success: call `parse_bid_response()`. On decline: update solicitation `status="responded"` with no bid. On parse failure: log warning, leave solicitation as "sent".

**Config (`app/config.py`):**

```python
excess_bid_scan_enabled: bool = True
excess_bid_parse_lookback_days: int = 14
```

### 3. Stats Cards on List View

Add 4 stat cards above the filter bar in `list.html`. Data comes from existing `get_excess_stats()`.

Cards:
| Card | Value | Icon |
|------|-------|------|
| Total Lists | `stats.total_lists` | clipboard-list |
| Total Items | `stats.total_line_items` | cube |
| Pending Bids | `stats.pending_bids` | clock |
| Awarded | `stats.awarded_items` | check-circle |

Style: compact inline cards matching the proactive scorecard pattern. White background, border, icon + number + label.

**Router:** `stats` is already passed to the template context in `partial_excess_list` — this is frontend-only.

### 4. Note Tooltips on Line Items

In `line_item_row.html`, when `item.notes` is truthy:
- Show a small note icon (📝 or SVG) after the part number
- `title="{{ item.notes }}"` for browser tooltip
- Truncate display to first 50 chars with ellipsis if needed

No modal needed — tooltip is sufficient for the 3-person team.

### 5. Normalization Display

In `line_item_row.html`, below the part number cell:
- When `item.normalized_part_number` and `item.normalized_part_number != item.part_number.strip().lower()`:
  - Show `→ {normalized}` in `text-[10px] text-gray-400 font-mono`
- When they match: show nothing (no noise)

Same pattern as materials tab MPN display.

### 6. ProactiveMatch Trigger Button

On the detail page header, when list status is `closed` or `expired`:
- Show "Create Proactive Matches" button (brand outline style)
- `hx-post="/api/excess-lists/{list_id}/proactive-matches"` → calls `create_proactive_matches_for_excess()`
- On success: show banner "Created N proactive matches"
- On zero matches: show banner "No matching archived requirements found"

**Router:** Add POST endpoint in `excess.py` that calls the existing service function.

## Migration

None needed — all model columns already exist from Phase 3 migrations.

## Testing

- **Email solicitation send:** Mock GraphClient, verify email payload structure, verify `graph_message_id` stored, verify failure handling
- **Bid response parsing:** Mock Claude response, verify Bid created with correct fields, verify solicitation status updated
- **Inbox scan integration:** Mock Graph inbox query, verify `_EXCESS_BID_RE` matching, verify end-to-end flow
- **Stats cards:** Verify `get_excess_stats()` called and passed to template
- **Note tooltip:** Verify notes rendered in title attribute
- **Normalization display:** Verify normalized MPN shown when different
- **ProactiveMatch button:** Verify endpoint calls service, returns correct count

## Files to Modify

1. `app/services/excess_service.py` — make `send_bid_solicitation` async, add Graph API send, add `parse_bid_from_email`
2. `app/routers/excess.py` — update solicitation endpoints for auth token, add proactive match endpoint, add HTMX solicit routes
3. `app/jobs/email_jobs.py` — add `_scan_excess_bid_responses()`, call from `_scan_user_inbox()`
4. `app/config.py` — add `excess_bid_scan_enabled`, `excess_bid_parse_lookback_days`
5. `app/templates/htmx/partials/excess/solicit_modal.html` — NEW: bid solicitation form modal
6. `app/templates/htmx/partials/excess/list.html` — add stats cards
7. `app/templates/htmx/partials/excess/line_item_row.html` — add solicitation badge, note tooltip, normalization display
8. `app/templates/htmx/partials/excess/detail.html` — add "Solicit Bids" button, ProactiveMatch trigger button
9. `tests/test_excess_phase4_email.py` — NEW: email send + parse tests
10. `tests/test_excess_phase4_inbox.py` — NEW: inbox scan integration tests

## What We're NOT Building

- No separate HTML email template file — inline construction (same as RFQ pattern)
- No batch solicitation UI with contact picker — single recipient per solicitation
- No custom Claude prompt editor — hardcoded extraction prompt
- No real-time email webhook — polling via existing scheduler interval (30 min)
- No separate solicitation list view — shown as badge count on line items, full list accessible via API
