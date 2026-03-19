# Proactive Part Match Redesign — Design Spec

**Date:** 2026-03-19
**Status:** Approved
**Scope:** Backend cleanup + batch UI + prepare/send workflow + sent tab improvements

---

## Problem Statement

The proactive matching system has two duplicate matching engines, N+1 query patterns, fragile global state, and a single-match-at-a-time UI. Salespeople cannot select multiple parts per customer, cannot send batch offers, and have no visibility into deduplication or throttle protections. The system also matches against sightings and archived requisitions, which should be removed — only confirmed buyer-entered Offers should trigger proactive matches.

## Design Decisions

These decisions were made during brainstorming and should not be revisited without explicit discussion:

1. **Confirmed offers only** — Proactive matching triggers only from buyer-entered `Offer` records with a `material_card_id`, matched against Customer Purchase History (CPH). Sightings and archived requisition matching are removed entirely.
2. **All parts → all contacts** — Selected parts go to all selected contacts. Each contact gets their own separate email with the full set of parts. Per-part contact assignment is out of scope (add later if users request it).
3. **MPN-level throttle only** — Don't re-offer the same part number to the same customer site within the throttle window (21 days default). No customer-level frequency cap for MVP.
4. **Account owner visibility** — Matches are visible only to the `company.account_owner_id` for the customer site. No cross-salesperson visibility (admins excepted).
5. **Full-page prepare, not drawer** — The "Prepare" step navigates to a full page, not a slide-over drawer. Simpler HTMX, back button works natively, no Alpine state coordination.
6. **AI draft is opt-in** — "Generate AI Draft" button, not auto-triggered. Avoids blocking the page on Claude API latency.
7. **Selection is per-group** — No cross-customer-group batch operations. Prepare and dismiss operate within a single customer group.

## Out of Scope (Future)

- Per-part contact assignment (matrix mode)
- Contact-level communication preferences (do_not_email, frequency caps)
- Customer exhaustion badges ("X offers / 30d")
- Filtered-out summary bar ("Y throttled | Z suppressed")
- Cross-customer dedup indicator ("+N others")
- Follow-up indicator on sent tab
- Toast notification system
- Sticky bottom toolbar for cross-group batch actions
- Undo dismiss
- Template library for emails
- Customer conversion likelihood scoring

---

## Phase 1: Backend Cleanup & Consolidation

All backend changes are prerequisites for the new UI. They pay down real tech debt and fix performance/correctness bugs.

### 1a. Remove Legacy & Sighting Matching

**Delete:**
- `scan_new_offers_for_matches()` from `app/services/proactive_service.py` (lines 42-164) — the legacy archived-requisition matching engine
- `find_matches_for_sighting()` from `app/services/proactive_matching.py` (lines 113-123)
- Sighting scan in `run_proactive_scan()` (lines 296-305 in proactive_matching.py)
- The dual-scan in `POST /api/proactive/refresh` (router lines 42-63) — replace with single call to `run_proactive_scan()`
- Module-level `_last_proactive_scan` global from proactive_service.py (line 36)
- `proactive_archive_age_days` config setting from `app/config.py` (only used by deleted legacy engine)

**Keep:**
- `run_proactive_scan()` as the single batch scan entry point (offers only)
- `find_matches_for_offer()` as the single-offer trigger
- `_find_matches()` as the core matching logic

### 1a-migration. Make `requirement_id` and `requisition_id` nullable on ProactiveMatch

The current model (`app/models/intelligence.py`) has `offer_id`, `requirement_id`, and `requisition_id` as NOT NULL FKs. When matching Offers against CPH, many CPH rows will have no corresponding requisition/requirement for the customer+part combo. The current code silently skips these — losing valid matches.

**Fix:** Alembic migration to make `requirement_id` and `requisition_id` nullable:

```python
# alembic migration
op.alter_column('proactive_matches', 'requirement_id', nullable=True)
op.alter_column('proactive_matches', 'requisition_id', nullable=True)
```

Update the model in `app/models/intelligence.py` to match. Remove the fallback requisition query from `_find_matches()` — matches without a historical requisition are now valid.

### 1b. Fix N+1 Queries in `_find_matches()`

Current: 6 individual queries per CPH row (Company, CustomerSite, DNO, Throttle, Requirement, existing Match). With 100 CPH rows = ~600 queries.

**Fix:** Before the loop, batch-load all needed data:

```python
company_ids = {cph.company_id for cph in cph_rows}
mpn_upper = mpn.upper().strip()

# 6 batch queries instead of 600 individual
companies = {c.id: c for c in db.query(Company).filter(Company.id.in_(company_ids)).all()}
sites = {}  # company_id → first active site
for s in db.query(CustomerSite).filter(
    CustomerSite.company_id.in_(company_ids), CustomerSite.is_active == True
).all():
    sites.setdefault(s.company_id, s)

dno_company_ids = {
    row[0] for row in db.query(ProactiveDoNotOffer.company_id)
    .filter(ProactiveDoNotOffer.mpn == mpn_upper, ProactiveDoNotOffer.company_id.in_(company_ids))
    .all()
}

site_ids = {s.id for s in sites.values()}
throttled_site_ids = {
    row[0] for row in db.query(ProactiveThrottle.customer_site_id)
    .filter(ProactiveThrottle.mpn == mpn_upper, ProactiveThrottle.customer_site_id.in_(site_ids),
            ProactiveThrottle.last_offered_at > throttle_cutoff)
    .all()
}

existing_match_company_ids = {
    row[0] for row in db.query(ProactiveMatch.company_id)
    .filter(ProactiveMatch.material_card_id == material_card_id,
            ProactiveMatch.status.in_(["new", "sent"]))
    .all()
}

req_by_site = {}
for req_item, requisition in (
    db.query(Requirement, Requisition)
    .join(Requisition, Requirement.requisition_id == Requisition.id)
    .filter(Requirement.material_card_id == material_card_id,
            Requisition.customer_site_id.in_(site_ids))
    .order_by(Requisition.created_at.desc())
    .all()
):
    req_by_site.setdefault(requisition.customer_site_id, (req_item, requisition))
```

Then the inner loop uses dict lookups instead of queries.

### 1c. Persist Scan Watermark

**Replace:** Module-level `_last_scan_at = datetime.min` global.

**With:** `SystemConfig` table entry, matching the established pattern from `app/jobs/eight_by_eight_jobs.py`:

```python
def _get_watermark(db: Session, key: str = "proactive_last_scan") -> datetime:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row and row.value:
        return datetime.fromisoformat(row.value)
    return datetime.now(timezone.utc) - timedelta(hours=settings.proactive_scan_interval_hours)

def _set_watermark(db: Session, ts: datetime, key: str = "proactive_last_scan"):
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row:
        row.value = ts.isoformat()
    else:
        db.add(SystemConfig(key=key, value=ts.isoformat(), description="Proactive scan watermark"))
    db.flush()
```

Also add `.limit(5000)` safety cap on the offer scan query, with batch processing if more exist.

### 1d. Add Status Enums

Define in `app/enums.py`:

```python
class ProactiveMatchStatus(str, Enum):
    new = "new"
    sent = "sent"
    dismissed = "dismissed"
    converted = "converted"
    expired = "expired"

class ProactiveOfferStatus(str, Enum):
    sent = "sent"
    converted = "converted"
```

Migrate incrementally — update files as they are touched during this project. No separate 12-file sweep.

### 1e. Extract Shared Helpers

Create `app/services/proactive_helpers.py`:

```python
def is_do_not_offer(db: Session, mpn: str, company_id: int) -> bool:
    """Check if MPN is permanently suppressed for a company."""
    ...

def is_throttled(db: Session, mpn: str, site_id: int, days: int | None = None) -> bool:
    """Check if MPN was recently offered to a customer site."""
    ...
```

**Remove** the duplicate HTML email builder from `proactive_service.py` (lines 405-447). Use `proactive_email.py:_build_html()` as the single source.

**Fix** the htmx_views do-not-offer creation (missing dedup check) to use `is_do_not_offer()` before inserting.

### 1f. Fix Deduplication

**Tighten dedup filter** in `_find_matches()`:

Current: `material_card_id + company_id + status IN (new, sent)` — but also checks `offer_id` when source_offer is present. This allows duplicate matches from different offers for the same part+customer.

New: `material_card_id + company_id + status IN (new, sent)` only. Remove the `offer_id` filter. One active match per part per customer, regardless of which offer triggered it.

**Enforce account owner visibility:**

When creating matches in `_find_matches()`, set `salesperson_id = company.account_owner_id`. If `account_owner_id` is null, skip the match (no owner = no one to show it to). The existing filter in `get_matches_for_user()` on `ProactiveMatch.salesperson_id == user_id` continues to work correctly since `salesperson_id` is always set to the account owner. No join change needed.

### 1g. Fix Scorecard (if actively used)

Replace Python-side aggregation with SQL:

```python
from sqlalchemy import func, case

stats = db.query(
    func.count(ProactiveOffer.id).label("sent"),
    func.count(case((ProactiveOffer.status == "converted", 1))).label("converted"),
    func.sum(case((ProactiveOffer.status == "converted",
                    func.least(ProactiveOffer.total_sell, 500000)), else_=0)).label("conv_rev"),
    func.sum(case((ProactiveOffer.status == "converted",
                    func.least(ProactiveOffer.total_cost, 500000)), else_=0)).label("conv_cost"),
    func.sum(case((ProactiveOffer.status == "sent",
                    func.least(ProactiveOffer.total_sell, 500000)), else_=0)).label("pending"),
).filter(...).one()
```

Replace `_cap_outlier` zero-out with `func.least(value, cap)`. Log warnings for capped values.

### 1h. Expire Matches — Single UPDATE

Replace load-then-loop with:

```python
count = db.query(ProactiveMatch).filter(
    ProactiveMatch.status == "new",
    ProactiveMatch.created_at < cutoff,
).update({"status": "expired"}, synchronize_session=False)
db.commit()
return count
```

---

## Phase 2: Table UI + Prepare Page

### 2a. Table Layout Per Customer Group

Replace card grid (`_match_card.html`) with a compact table per customer group.

**Columns (7):**

| # | Column | Width | Content |
|---|--------|-------|---------|
| 1 | Checkbox | 40px | Select for prepare/dismiss |
| 2 | MPN | flex | Part number (monospace), manufacturer below in gray |
| 3 | Vendor | 140px | Vendor name + reliability tag ("trusted"/"unreliable") |
| 4 | Qty | 80px | Formatted number |
| 5 | Unit Price | 90px | Dollar amount |
| 6 | Margin | 70px | Colored pill: green ≥20%, amber ≥10%, rose <10% |
| 7 | Score | 60px | Colored number pill: green ≥75, amber ≥50, gray <50 |

**Purchase history:** Small repeat-purchase icon with count (`3×`), full detail (last purchased date, avg price) in hover popover via `title` attribute or Alpine popover. Not a full column.

**Vendor reliability tags** (requires join: `ProactiveMatch → Offer → VendorCard` via `offer.vendor_card_id`):
- Ghost rate >30%: red "unreliable" text tag
- Vendor score ≥70: green "trusted" text tag
- No VendorCard linked (nullable FK): no tag shown
- Otherwise: no tag

**Margin when null:** Show gray "N/A" pill when `margin_pct` is null (unknown cost/price data).

### 2b. Customer Group Header

```
┌──────────────────────────────────────────────────────────────────────┐
│ [▼] Acme Electronics — Main Site    12 matches    [☐ All] [Prepare (0)] [Dismiss (0)] │
└──────────────────────────────────────────────────────────────────────┘
```

- **Left:** Collapse/expand toggle + company name + site name + match count
- **Right:** Select-all checkbox + "Prepare (N)" button + "Dismiss (N)" button
- Prepare and Dismiss buttons show selected count, disabled when count is 0
- Collapsible groups — default expanded, collapse icon (▼/▶)

**Group sort dropdown** (above all groups):
- By opportunity (highest total margin first) — default
- By customer name (A-Z)
- By match count (most first)

### 2c. Responsive Behavior

Desktop-first. Below `md` breakpoint, show horizontal scroll with frozen checkbox+MPN column. No mobile-specific card layout for MVP (B2B desktop tool).

### 2d. Alpine.js State (Per-Group)

Each customer group manages its own selection state. Note: Alpine.js does not deeply track `Set` mutations. Use a reactive object (plain object with ID keys) instead:

```javascript
function proactiveGroup(groupData) {
  return {
    selected: {},  // { matchId: true } — plain object for Alpine reactivity
    get selectedCount() { return Object.keys(this.selected).length },
    get selectedIds() { return Object.keys(this.selected).map(Number) },
    toggle(id) {
      if (this.selected[id]) delete this.selected[id];
      else this.selected[id] = true;
    },
    selectAll() {
      groupData.matchIds.forEach(id => this.selected[id] = true);
    },
    deselectAll() { this.selected = {} },
    toggleAll() {
      this.selectedCount === groupData.matchIds.length ? this.deselectAll() : this.selectAll();
    },
  }
}
```

No cross-group state. Each group is self-contained.

### 2e. Row Sort Order Within Groups

Rows within each customer group sorted by `match_score` descending (highest opportunity first).

### 2f. Empty States

- **No matches at all:** Centered icon + "No new matches today. Matches appear when vendor offers align with customer purchase history."
- **Sent tab empty:** "No offers sent yet. Select matches and prepare offers from the Matches tab."

---

## Phase 3: Prepare & Send Workflow

### 3a. Prepare Flow

1. Salesperson checks parts in a customer group
2. "Prepare (N)" button becomes active, showing count
3. Click submits via `POST /v2/proactive/prepare/{site_id}` with match_ids in form body (avoids URL length limits for large selections)
4. Full-page layout renders with selected parts and contacts

### 3b. Prepare Page Layout

```
┌─────────────────────────────────────────────────────┐
│ ← Back to Matches                                   │
│                                                     │
│ Prepare Offer — Acme Electronics (Main Site)        │
│                                                     │
│ ┌─ Selected Parts ────────────────────────────────┐ │
│ │ MPN          │ Vendor    │ Qty   │ Price │ Margin│ │
│ │ LM358N       │ Arrow     │ 5,000 │ $0.42 │ 23%  │ │
│ │ TL072CP      │ DigiKey   │ 2,500 │ $0.89 │ 18%  │ │
│ │ NE555P       │ Mouser    │10,000 │ $0.31 │ 31%  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ┌─ Send To ──────────────────────────────────────┐  │
│ │ ☑ Jane Doe — jane@acme.com — Buyer (Primary)  │  │
│ │ ☐ Bob Smith — bob@acme.com — Technical         │  │
│ │ ☐ Lisa Chen — (no email) [disabled]            │  │
│ │                                                │  │
│ │ No contacts? [Add Contact →]                   │  │
│ └────────────────────────────────────────────────┘  │
│                                                     │
│ ┌─ Email ────────────────────────────────────────┐  │
│ │ Subject: [____________________________]        │  │
│ │                                                │  │
│ │ Body:                                          │  │
│ │ [                                    ]         │  │
│ │ [                                    ]         │  │
│ │ [                                    ]         │  │
│ │                                                │  │
│ │ [Generate AI Draft]  (loading skeleton if busy)│  │
│ │                                                │  │
│ │ On failure: "Auto-draft unavailable.           │  │
│ │  Write your message manually." [Retry]         │  │
│ └────────────────────────────────────────────────┘  │
│                                                     │
│              [Cancel]  [Send to 1 Contact]          │
└─────────────────────────────────────────────────────┘
```

### 3c. Contact Picker Details

- Checkboxes for each `SiteContact` at the customer site
- Primary contact pre-selected
- Contacts without email: shown but disabled, with "(no email)" label
- No contacts at all: empty state with "No contacts on file. Add Contact →" link (opens customer detail in new tab)
- Send button label updates dynamically: "Send to N Contact(s)"
- Send button disabled when no contacts selected

### 3d. AI Draft

- **Not auto-triggered.** Salesperson sees empty subject/body fields.
- **"Generate AI Draft" button** calls `POST /v2/partials/proactive/draft` with match_ids and contact info.
- **Loading state:** Skeleton pulse animation in the subject/body fields, button disabled with spinner.
- **On success:** Subject and body populated. Salesperson can edit.
- **On failure:** Banner above email fields: "Auto-draft unavailable. Write your message manually." + "Retry" button. Subject/body remain editable.
- **Timeout:** 10 seconds max. Falls back to failure state.

### 3e. Send Flow

1. Salesperson clicks "Send to N Contact(s)"
2. `POST /v2/partials/proactive/send` with `match_ids`, `contact_ids`, `sell_prices`, `subject`, `email_html`
3. Backend sends separate email per contact (not CC) via Graph API
4. Creates throttle records per MPN+site
5. Updates match statuses to "sent"
6. Creates ProactiveOffer record

**On success:** Redirect to matches list with inline success banner: "Offer sent to N contact(s) at Acme Electronics (3 parts)." Sent matches removed from list via normal re-render.

**On failure:** Stay on prepare page. Show error banner: "Failed to send to jane@acme.com: [reason]. Other emails sent successfully." Match status unchanged for failed sends.

**Partial failure:** Track send status per contact on the ProactiveOffer record (e.g., `send_status: dict[str, str]` mapping email → "sent"/"failed"/"pending"). Matches marked "sent" when at least one email succeeds. Failed contacts shown with retry option — retry only re-sends to failed contacts, not all. One `ProactiveOffer` record per send operation (not per contact), with `recipient_contact_ids` as the full list and `send_status` tracking individual results.

### 3f. Per-Group Dismiss

- "Dismiss (N)" button in group header, active when items selected
- `POST /v2/partials/proactive/dismiss` with match_ids
- On success: re-render the group via HTMX swap. Group count updates.
- If all matches in group dismissed, show empty state for that group or remove it.
- No cross-group dismiss in v1.
- `hx-confirm="Dismiss N matches?"` for safety.

---

## Phase 4: Sent Tab Improvements

### 4a. Group by Customer

Match the Matches tab structure — customer group headers with company name + site name.

### 4b. Expandable Line Items

Each sent offer row shows a part count badge ("3 parts") that toggles a sub-table on click via Alpine `x-show`:

```html
<button @click="expanded = !expanded">
  3 parts <span :class="expanded ? 'rotate-90' : ''">▶</span>
</button>
<div x-show="expanded" x-collapse>
  <table><!-- MPN, Manufacturer, Qty, Sell Price per line item --></table>
</div>
```

### 4c. Revenue Column

Show `total_sell` formatted as currency inline in the sent offers table.

### 4d. Relative Timestamps

Server-side helper to convert ISO timestamps to relative format ("2h ago", "3d ago", "2w ago"). Use a Jinja filter, no JS library. Note: timestamps won't update without page refresh — acceptable tradeoff for a B2B tool.

---

## Data Flow Summary

```
Buyer enters Offer
    ↓
Scheduler (every 4h) or Manual Refresh
    ↓
run_proactive_scan(db)
    ↓
For each new Offer with material_card_id:
    → Query CPH for companies that bought this part
    → Batch-load: companies, sites, DNO, throttles, existing matches, reqs
    → Score each: recency (40%) + frequency (30%) + margin (30%)
    → Filter: DNO suppression, throttle window, min margin, existing match dedup
    → Create ProactiveMatch (status=new, salesperson=account_owner)
    → ActivityLog notification for salesperson
    ↓
Salesperson views /v2/proactive (Matches tab)
    → Matches grouped by customer, sorted by opportunity
    → Table with checkboxes per group
    ↓
Salesperson selects parts → clicks "Prepare (N)"
    → Full page: POST /v2/proactive/prepare/{site_id} (match_ids in form body)
    → Parts summary + contact picker + email compose
    → Optional: "Generate AI Draft" button
    ↓
Salesperson clicks "Send to N Contact(s)"
    → Separate email per contact via Graph API
    → Throttle records created per MPN+site
    → Match status → sent
    → ProactiveOffer record created
    → Redirect to matches list with success banner
```

## File Impact

### Modified Files
- `app/services/proactive_matching.py` — Remove sighting matching, fix N+1, persist watermark
- `app/services/proactive_service.py` — Remove legacy engine, remove duplicate email builder, fix scorecard, fix expire
- `app/services/proactive_email.py` — Becomes single source for email HTML building
- `app/routers/proactive.py` — Simplify refresh endpoint, update send endpoint
- `app/routers/htmx_views.py` — New prepare page route, updated list partial, fix DNO dedup
- `app/schemas/proactive.py` — Minor updates for batch operations
- `app/models/intelligence.py` — Use status enum on model
- `app/enums.py` — Add ProactiveMatchStatus, ProactiveOfferStatus
- `app/templates/htmx/partials/proactive/list.html` — Complete rewrite (table layout)
- `app/templates/htmx/partials/proactive/_match_card.html` — Replace with `_match_row.html`
- `app/templates/htmx/partials/proactive/draft_form.html` — Remove (replaced by prepare page)
- `app/templates/htmx/partials/proactive/send_success.html` — Inline banner instead
- `app/config.py` — Remove `proactive_archive_age_days` (only used by deleted legacy engine)

### New Files
- `app/services/proactive_helpers.py` — Shared helpers (is_do_not_offer, is_throttled)
- `app/templates/htmx/partials/proactive/_match_row.html` — Table row partial
- `app/templates/htmx/partials/proactive/prepare.html` — Full prepare page
- Tests for all new/modified code

### Unchanged Files (kept as-is)
- `app/templates/htmx/partials/proactive/scorecard.html` — Scorecard panel (no changes)
- `app/templates/htmx/partials/proactive/convert_success.html` — Conversion confirmation (no changes)

### Deleted Files
- `app/templates/htmx/partials/proactive/_match_card.html` — Replaced by `_match_row.html`
- `app/templates/htmx/partials/proactive/draft_form.html` — Replaced by prepare page
- `app/templates/htmx/partials/proactive/send_success.html` — Replaced by inline banner on matches list
