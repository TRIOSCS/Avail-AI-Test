# Strategic Vendors Feature Plan

## What It Does
Each buyer can claim up to **10 strategic vendors** — their personal vendor relationships. Only strategic vendor responses get tracked for follow-ups and alerts. If a strategic vendor doesn't enter an offer within **39 days**, they lose strategic status and return to the open pool. Hard cap of 10 — to add one, you must drop one.

## Why
Most vendors don't respond to RFQs. Tracking all of them creates overwhelming follow-up noise. Strategic vendors focus buyer attention on the relationships that actually matter.

---

## Existing Code Context

- **VendorCard** already tracks `total_outreach`, `total_responses`, `ghost_rate`, `last_contact_at`, `engagement_score`
- **BuyerVendorStats** tracks per-buyer-vendor metrics but has NO assignment concept
- **Company** has `is_strategic` flag + `account_owner_id` — same pattern we'll follow
- **Offer** links to `vendor_card_id` + `entered_by_id` — we hook offer creation
- **Contact** model tracks outbound RFQs per vendor per req
- Current migration head: **065**
- Scheduler uses APScheduler with CronTrigger pattern in `app/jobs/`

---

## Step 1: Database Model + Migration (066)

**New file:** `app/models/strategic.py`

```
strategic_vendors table:
  id              INT PK
  user_id         INT FK(users) NOT NULL        — the buyer
  vendor_card_id  INT FK(vendor_cards) NOT NULL  — the vendor
  claimed_at      DATETIME NOT NULL              — when buyer claimed
  last_offer_at   DATETIME NULL                  — last offer from this vendor (any req)
  expires_at      DATETIME NOT NULL              — resets to now+39 days on each offer
  released_at     DATETIME NULL                  — NULL = active, set when released
  release_reason  VARCHAR(20) NULL               — 'expired' | 'dropped' | 'replaced'

  UNIQUE(user_id, vendor_card_id)
  INDEX(user_id, released_at)      — "my active strategic vendors"
  INDEX(expires_at, released_at)   — scheduler expiry scan
  INDEX(vendor_card_id, released_at) — "who owns this vendor?"
```

**Migration:** `alembic/versions/066_strategic_vendors.py`

---

## Step 2: Service Layer

**New file:** `app/services/strategic_vendor_service.py`

| Function | What |
|----------|------|
| `get_my_strategic(db, user_id)` | Active vendors for this buyer (released_at IS NULL) |
| `claim_vendor(db, user_id, vendor_card_id)` | Claim vendor. Fail if buyer at 10 or vendor already claimed. |
| `drop_vendor(db, user_id, vendor_card_id)` | Release with reason='dropped' |
| `replace_vendor(db, user_id, drop_id, claim_id)` | Atomic: drop old + claim new in one transaction |
| `record_offer(db, vendor_card_id)` | Update last_offer_at, reset expires_at to now+39d |
| `expire_stale(db)` | Set released_at+reason on expired rows. Returns count. |
| `get_expiring_soon(db, days=7)` | Vendors expiring within N days (for warnings) |
| `get_vendor_status(db, vendor_card_id)` | Who has this vendor, days left. None if open pool. |
| `get_open_pool(db, limit, offset)` | Vendors not claimed by anyone |

**Business rules:**
- Hard cap: 10 per buyer (COUNT WHERE user_id=X AND released_at IS NULL)
- One buyer per vendor (UNIQUE constraint)
- 39-day TTL resets on every offer entry for that vendor
- Expired vendors go to open pool (released_at set, release_reason='expired')

---

## Step 3: API Endpoints

**New file:** `app/routers/strategic.py` — prefix `/api/strategic-vendors`

| Method | Path | Auth | What |
|--------|------|------|------|
| GET | `/mine` | require_user | My strategic vendors with days remaining, last offer date |
| POST | `/claim/{vendor_card_id}` | require_buyer | Claim. 409 if at cap or taken. |
| DELETE | `/drop/{vendor_card_id}` | require_buyer | Drop to open pool |
| POST | `/replace` | require_buyer | Body: `{drop_id, claim_id}` — atomic swap |
| GET | `/open-pool` | require_user | Unclaimed vendors (paginated, searchable) |
| GET | `/status/{vendor_card_id}` | require_user | Who owns this vendor + days left |

---

## Step 4: Hook Into Offer Creation

**Edit:** `app/routers/crm/offers.py`

After successful offer creation, add:
```python
from app.services import strategic_vendor_service
strategic_vendor_service.record_offer(db, offer.vendor_card_id)
```

**Edit:** `app/email_service.py` — in `_apply_parsed_result()`

Same hook when AI-parsed offer is auto-created from vendor email.

Both reset the 39-day clock for whichever buyer has that vendor as strategic.

---

## Step 5: Scheduler Jobs

**Edit:** `app/jobs/offers_jobs.py`

**Job 1 — Daily expiry (6 AM):**
```
_job_expire_strategic_vendors:
  - Find WHERE expires_at < now AND released_at IS NULL
  - Set released_at = now, release_reason = 'expired'
  - Create in-app notification for affected buyer
  - Log count
```

**Job 2 — Warning alerts (8 AM):**
```
_job_warn_strategic_expiring:
  - Find WHERE expires_at < now + 7 days AND released_at IS NULL
  - Send notification: "Vendor X expires in N days — get an offer or lose them"
  - Only send once per vendor per warning cycle (check if notification already exists)
```

---

## Step 6: Frontend

### 6a. Vendor Detail — Strategic Badge
When viewing any vendor (vendor drawer/detail), show:
- **Your vendor:** "Strategic (You) — 23 days left" + "Drop" button
- **Someone else's:** "Strategic (John)" — no action
- **Open pool:** "Claim as Strategic" button (disabled if you're at 10)

### 6b. Left Sidebar Nav — "My Vendors" Button
New nav button below existing nav items.

Click shows "view-strategic" panel:
- List of your strategic vendors (up to 10)
- Each row: vendor name, days remaining (red <7), last offer date
- Progress bar showing 10-slot usage (e.g., "7/10 slots used")
- "Drop" action per vendor
- "Claim Vendor" button → opens vendor search typeahead
- Search disabled at 10/10 with message "Drop a vendor first"

### 6c. Response Tracking Filter
Modify follow-up/response alerts to only fire for strategic vendors:
- RFQ follow-up notifications
- "No response" warnings
- Ghost rate alerts
- Morning briefing vendor items

Non-strategic vendors still get searched and contacted, just no follow-up noise.

---

## Step 7: Tests

**New file:** `tests/test_strategic_vendors.py`

- `test_claim_vendor_success` — claim works, record created
- `test_claim_at_10_fails` — 10 cap enforced, returns 409
- `test_claim_already_taken` — vendor owned by another buyer, returns 409
- `test_drop_vendor` — released_at set, reason='dropped'
- `test_replace_vendor` — atomic drop+claim in one call
- `test_offer_resets_clock` — record_offer updates expires_at to now+39d
- `test_expire_stale` — expired vendor released, reason='expired'
- `test_expiring_soon_query` — warning query returns correct vendors
- `test_open_pool` — unclaimed vendors returned
- `test_api_mine_endpoint` — GET /mine returns buyer's vendors
- `test_api_claim_endpoint` — POST /claim creates record
- `test_api_requires_auth` — unauthenticated returns 401

---

## File Summary

| File | Action |
|------|--------|
| `app/models/strategic.py` | **NEW** — StrategicVendor model |
| `app/models/__init__.py` | EDIT — import |
| `alembic/versions/066_strategic_vendors.py` | **NEW** — migration |
| `app/services/strategic_vendor_service.py` | **NEW** — all business logic |
| `app/routers/strategic.py` | **NEW** — API endpoints |
| `app/main.py` | EDIT — register router |
| `app/routers/crm/offers.py` | EDIT — hook record_offer |
| `app/email_service.py` | EDIT — hook record_offer on AI-parsed offers |
| `app/jobs/offers_jobs.py` | EDIT — expiry + warning jobs |
| `app/static/crm.js` | EDIT — My Vendors panel + vendor badge |
| `app/templates/index.html` | EDIT — nav button + view panel |
| `app/static/styles.css` | EDIT — strategic vendor styles |
| `tests/test_strategic_vendors.py` | **NEW** — tests |

## Implementation Order

1. Model + migration (foundation)
2. Service layer (business logic)
3. Tests for service + API
4. API endpoints + register router
5. Hook into offer creation (both manual + AI-parsed)
6. Scheduler jobs (expiry + warnings)
7. Frontend (nav, panel, vendor badges)
