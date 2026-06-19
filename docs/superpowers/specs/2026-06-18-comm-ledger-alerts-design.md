# Communication Ledger + Cross-App Alerts — Design Spec (2026-06-18)

## Problem & goal

Today only the **Proactive** tab notifies a salesperson of new matches (an emerald
count bubble on the nav icon, polled every 60s, counting `ProactiveMatch.status == "new"`).
We want the same *quiet, useful* nudge on the other tabs — **without becoming noisy or
overdone** — and, underneath it, a **complete communication ledger**: every interaction
between a TRIO buyer/salesperson and a customer or vendor, in both directions, across all
channels, surfaced on the account, the vendor, and in Sales Hub.

The alerts are a thin layer; the ledger is the substrate. Get the ledger right and every
timeline, cadence clock, and badge downstream becomes trustworthy.

## Decided design (locked in brainstorming)

1. **Per-tab green count bubble** on each nav icon, reusing the Proactive badge pattern
   verbatim (`hx-trigger="load, every 60s"` → emerald pill or empty). **No central inbox.**
2. **In-tab fluid spotlight** on entry: smoothly glide to the first new item; each new row
   wears an emerald **accent rail**; a sticky **"N new ↓" jump-pill** hops between them.
   As each row scrolls into view (IntersectionObserver) it is marked seen and the badge
   ticks down live.
3. **Two temperaments** (one flag per source):
   - **FYI** (`clears_on_see`): new confirmed offers, inbound customer comms, vendor inbound
     awaiting response. Count = relevant-&-mine-&-recent items **not yet seen**. The rail
     pulses once then fades; seeing drains the count.
   - **Action** (`clears_on_act`): buy-plan steps assigned to me (and, later, Proactive).
     Count = **open work items**, derived purely from work-state — `alert_seen` never
     affects the count, it only suppresses re-pulsing a row you've already looked at. The
     rail is a calm steady emerald and stays until the work is done.
4. **Relevance = strictly mine** — owner / assignee / next-actor. A teammate's items light
   *their* badges, never mine.
5. **Alert = the inbound / needs-response slice only.** Your own outbound shows in the feed
   for context but never badges you. Feed = comprehensive; alert = a filtered view of it.

## Architecture — the `AlertSource` primitive

New package `app/services/alerts/`. One reusable abstraction so every current and future
alert is "implement a count-query + a new-items-query, register it, done."

```
class Temperament(StrEnum):
    FYI = "fyi"        # clears on see — count excludes seen
    ACTION = "action"  # clears on act — count from work-state; seen only gates the pulse

class AlertItem:        # what the spotlight needs
    ref_id: int         # offer.id / activity_log.id / buy_plan_line.id
    anchor: str         # DOM id / data attr the tab template stamps on the row

class AlertSource(ABC):
    key: str            # nav-tab source key, e.g. "sales_hub_offers"
    kind: str           # alert_seen.alert_kind, e.g. "offer_confirmed"
    temperament: Temperament
    def count_for_user(self, db, user) -> int: ...
    def new_items_for_user(self, db, user) -> list[AlertItem]: ...
```

- A **registry** maps each nav **tab key** → list of `AlertSource` (a tab may carry more
  than one: Sales Hub = offers + vendor-inbound). The badge for a tab is the **sum** of its
  sources' counts.
- `count_for_user` / `new_items_for_user` are the only things a new alert must implement.
- Helpers in the base class compute the FYI exclusion (`id NOT IN alert_seen`), the recency
  window, and the launch epoch so each source stays tiny.

## Data model — one new table

`app/models/alert_seen.py` → table `alert_seen`:

| column | type | notes |
|---|---|---|
| `id` | Integer PK | |
| `user_id` | Integer FK `users.id` | indexed |
| `alert_kind` | String(40) | `offer_confirmed`, `inbound_customer`, `inbound_vendor`, `buyplan_action` |
| `ref_id` | Integer | the source item's id |
| `seen_at` | `UTCDateTime` | default now |

- `UniqueConstraint(user_id, alert_kind, ref_id)` — idempotent upsert.
- `Index(user_id, alert_kind)` — the hot read path.

No other new tables. Action counts derive from existing work-state columns.

Alembic migration adds the table (with rollback). `seen_at`/recency keep the table bounded;
a lightweight nightly prune (rows whose `ref_id` is outside the recency window) is optional
housekeeping, not load-bearing.

## Recency window & launch epoch

- `ALERT_RECENCY_DAYS = 30` (config). FYI sources count only items whose "new" timestamp is
  within the window **and** `>= ALERTS_EPOCH`.
- `ALERTS_EPOCH` (config datetime, default = first deploy of this feature) prevents a wall
  of historical badges at launch — only items dated after go-live are eligible.
- **Action** sources have **no** window: an open to-do is always counted, however old.

## The sources

### 1. Sales Hub — confirmed offers — `kind=offer_confirmed`, FYI  *(Phase 1, no ledger dep)*
- "new" timestamp = `Offer.approved_at`.
- eligible offer: `status == OfferStatus.APPROVED` **and**
  `qualification_status IN (QualificationStatus.ESSENTIALS, QualificationStatus.COMPLETE)`.
- mine: `offer.requirement.assigned_buyer_id == user.id`, falling back when that is NULL to
  `offer.requirement.requisition.created_by == user.id`.
- count = eligible & mine & `approved_at` in window & `offer.id NOT IN alert_seen(user, offer_confirmed)`.
- spotlight anchors = the requirement rows carrying those offers in the Sales Hub list.

### 2. Buy Plans — my action queue — `kind=buyplan_action`, ACTION  *(Phase 1, no ledger dep)*
Count = union of my **open** steps (derived from work-state, no recency):
- **Buyer PO:** `BuyPlanLine.buyer_id == user.id` and `BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO`.
- **Manager approval:** `BuyPlan.status == SUBMITTED` and `approved_by_id IS NULL` and the
  user is permitted to approve (reuse the existing approver/admin check).
- **Ops verify:** `BuyPlan.so_status == PENDING` and `so_verified_by_id IS NULL` and the user
  is in the Ops verification group (reuse the Settings → Ops Group membership from PR #344).
- `alert_seen(user, buyplan_action, ref_id=line/plan id)` only records the one-time pulse;
  it does **not** subtract from the count. Steps leave the count when the work-state changes.

### 3. CRM — inbound customer — `kind=inbound_customer`, FYI  *(Phase 3, needs ledger)*
- "new" timestamp = `ActivityLog.occurred_at` (fallback `created_at`).
- eligible: `direction == Direction.INBOUND`, `channel IN (EMAIL, PHONE, TEAMS, WECHAT)`,
  `company_id` → a `Company` with `account_type == "Customer"`, `dismissed_at IS NULL`.
- mine: that `Company.account_owner_id == user.id`.
- count = eligible & mine & in window & `activity.id NOT IN alert_seen(user, inbound_customer)`.

### 4. Sales Hub — vendor inbound awaiting response — `kind=inbound_vendor`, FYI  *(Phase 3)*
- eligible: `ActivityLog.direction == INBOUND`, `vendor_card_id NOT NULL`,
  `requisition_id NOT NULL`, `dismissed_at IS NULL`.
- mine: `requisition.created_by == user.id` or the linked requirement's
  `assigned_buyer_id == user.id`.
- **De-dup vs offers:** when a confirmed offer is created from a vendor email, that email's
  `ActivityLog` row is auto-marked seen for the owner (`alert_seen(inbound_vendor)`), so one
  vendor reply never lights both bubbles for the same handled state. A raw reply that hasn't
  produced an offer still counts.

## Endpoints

- `GET /v2/partials/alerts/{tab_key}/badge` → emerald pill summing the tab's sources, or
  empty string at 0. Mirrors the Proactive badge HTML exactly. Polled `load, every 60s`.
  **Fail-quiet:** any query error logs and renders empty — the nav must never break.
- `POST /v2/partials/alerts/{kind}/seen` body `ref_id` → idempotent upsert into `alert_seen`;
  returns the refreshed nav badge as an OOB swap. Authn via `require_user`; a user can only
  write their own `user_id`.
- Tab partials stamp `data-alert-new` + `data-alert-kind` + `data-ref-id` on new rows and
  expose the count to the shared Alpine component.

## Frontend

- One shared module: an Alpine component (`tabAlerts`) + minimal JS. On tab load: find
  `[data-alert-new]`, glide to the first, attach the emerald accent rail, set the jump-pill
  count, and register an IntersectionObserver. As each new row enters the viewport →
  `POST .../seen` → FYI rows fade their rail, Action rows keep it → decrement the pill →
  the OOB response refreshes the nav badge.
- Emerald palette identical to the Proactive badge/banner. No new design conventions.
- HTMX-render rule: the lazy/observer container carries an explicit `hx-target` so it never
  inherits `#main-content`'s `hx-target="this"`.

## Phase 2 — the Communication Ledger (the substrate)

Today's inbox sync is **RFQ-centric** (logs RFQ sends; matches *vendor* replies to
requisitions). Customer inbound is **never** logged (mined only as `EmailIntelligence`).
We flip to **counterparty-centric** capture:

- New/extended service: for each TRIO buyer/salesperson mailbox (sent + received), match the
  counterparty email/domain → `Company` (customer) or `VendorCard` (vendor), `JUNK_DOMAINS`
  filtered. On a match, write an `ActivityLog` row: `direction` from sent/received,
  `channel=EMAIL`, `event_type=EMAIL`, `external_id=<message id>` for dedup,
  `company_id`/`vendor_card_id` set, `requisition_id` when the thread matches an RFQ ref.
- Reuses the existing inbox-poll job; broadens matching beyond RFQ replies. Feeds the
  existing dual-clock cadence engine automatically via `bump_clocks_from_activity`.
- **Privacy/scope:** only messages whose counterparty matches a *known* customer/vendor are
  logged. Personal/unknown correspondence is never captured.
- Surfaces are already built (account Activity tab, requisition Activity tab) and get richer
  for free; Phase 3 adds the missing **vendor** activity timeline UI.

## Phase plan

| Phase | Delivers |
|---|---|
| **0** | This spec; `alert_seen` table + migration; `AlertSource` primitive + registry; unit tests. |
| **1** | `offer_confirmed` + `buyplan_action` sources; badge + seen endpoints; nav wiring; shared spotlight frontend; unit + Playwright tests. |
| **2** | Communication Ledger: counterparty-centric capture into `ActivityLog`; matching/dedup/attribution; tests. |
| **3** | `inbound_customer` + `inbound_vendor` sources; vendor activity timeline UI; de-dup vs offers; tests. |
| **4** | Verify: full suite, `/simplify`, PR-review fleet, live-verify on real PG; APP_MAP doc updates; optional Proactive→primitive migration + `user:{id}` SSE for instant badges. |

Phases 1 and 2 are independent; 3 depends on both.

## Testing

- **Unit (per source):** ownership scoping (teammate's items excluded), the status +
  qualification combo, recency/epoch boundaries, seen-set exclusion (FYI) vs work-state
  derivation (Action). Idempotent `seen` endpoint. Fail-quiet badge endpoint.
- **Playwright (Phase 1):** glide-to-first, live badge decrement, FYI rail fade vs Action
  rail persistence — IntersectionObserver only verifies truthfully in a headless browser.
- **Ledger (Phase 2):** counterparty matching, dedup by `external_id`, correct
  customer-vs-vendor attribution, junk filtering, cadence-clock advance.

## Non-goals (v1)

- No central notification inbox/bell.
- No realtime push in Phase 1 (60s polling, like Proactive); `user:{id}` SSE is a Phase 4
  enhancement.
- No alerting on outbound activity.
- Proactive stays as-is until the optional Phase 4 consolidation.
