# Proactive auto-feed: customer purchase history from buy-plan completion

**Date:** 2026-06-17
**Status:** Design — approved decisions locked, pending spec review
**Author:** session with mkhoury

## 1. Problem

The Proactive tab is a proactive-selling cockpit: it watches incoming vendor stock
(Offers) and surfaces *"a customer who bought this exact part before could buy it again —
here's the margin."* Its entire matching backbone is `customer_part_history` (CPH).

Today CPH only grows from two premature hooks — `_record_offer_won_history`
(`source='avail_offer'`) and `_record_quote_won_history` (`source='avail_quote_won'`) —
which fire when an offer/quote is *marked won*, before there is a confirmed customer PO.
There is **no bulk or authoritative ingestion**, so on a fresh database CPH is empty, the
tab shows nothing, and a rep who opens it once never returns. This is the single biggest
blocker to adoption and to the "minimal updating" goal.

**Key insight (from the business):** every fulfilled customer order closes through a
**buy plan** that carries the customer PO number and the sales-order (SO) number, entered
by buyers/salespeople as part of the normal workflow. Salesforce is being retired, so the
buy plan is the authoritative system of record for "which customer bought which part."

Therefore: record CPH automatically at **buy-plan completion**. The proactive backbone
fills itself as a byproduct of work reps already do — zero extra effort.

## 2. Locked decisions

| Decision | Choice |
|---|---|
| System of record | **Buy plan** — every fulfilled order has one (+ SO#). |
| Authoritative CPH source | New `source='buy_plan'`. **Retire** the `avail_offer` / `avail_quote_won` write hooks. |
| Trigger | Buy plan → **COMPLETED** (`check_completion`); record only **VERIFIED** lines. |
| Idempotency | New column `buy_plans_v3.purchase_history_recorded_at`. |
| History | One-time idempotent **backfill** of existing COMPLETED plans. |
| Engine | **Aggregate CPH per (company, card)** across sources so legacy + buy_plan rows are coherent and the newest history dominates. |
| Responsiveness | On completion, immediately re-match live offers for the purchased parts (don't wait for the 4h scan). |

## 3. Current-state facts (verified in code + live DB)

- `check_completion` (`app/services/buyplan_workflow.py:314-339`) is the single
  ACTIVE→COMPLETED transition. It already sets `plan.status=COMPLETED` and
  `plan.completed_at`, and runs only once (guarded on `status==ACTIVE`). **No CPH call
  exists here today** (grep-confirmed).
- A buy-plan line resolves to a part via `line.requirement.material_card_id`, fallback
  `line.offer.material_card_id` (`app/models/buy_plan.py:186-187,233-234`). The customer
  resolves via `plan.requisition.customer_site.company_id`.
- `line.unit_sell` is the price the customer paid (drives future margin math); `line.quantity`
  is the qty; `line.status` VERIFIED vs CANCELLED distinguishes a real purchase.
- `upsert_purchase(db, *, company_id, material_card_id, source, unit_price, quantity,
  purchased_at, source_ref)` (`app/services/purchase_history_service.py`) is keyed on
  `(company_id, material_card_id, source)`; on conflict it increments `purchase_count`,
  updates rolling `avg_unit_price`, accumulates `total_quantity`.
- `_find_matches` (`app/services/proactive_matching.py:142-310`) queries all CPH rows for
  a card and **dedups by `company_id`, first row wins (unordered)** — so with multiple
  sources per company the scored row is arbitrary. This is why aggregation is needed.
- Live DB today: 6 buy plans (2 COMPLETED), all lines part-resolvable with `unit_sell` set
  — backfill will produce real CPH immediately.

## 4. Components

### 4.1 `record_buyplan_purchase_history(plan, db)` — new
Location: `app/services/purchase_history_service.py` (next to `upsert_purchase`).

Behavior:
1. If `plan.purchase_history_recorded_at` is set → return (idempotent no-op).
2. Resolve `company_id` from `plan.requisition.customer_site.company_id`. If missing → log
   warning, set the flag (so we don't retry forever), return.
3. For each line with `status == VERIFIED`:
   - Resolve `material_card_id` = `line.requirement.material_card_id` or
     `line.offer.material_card_id`. If neither → log, skip line.
   - `upsert_purchase(company_id=…, material_card_id=…, source="buy_plan",
     unit_price=line.unit_sell, quantity=line.quantity,
     purchased_at=plan.completed_at, source_ref=plan.sales_order_number)`.
   - Collect the set of affected `material_card_id`s.
4. Set `plan.purchase_history_recorded_at = now`, `db.flush()`.
5. Call `refresh_matches_for_cards(affected_cards, db)` (4.4) — best-effort.

Wrapped so any failure logs and never raises (completion must not break on a CPH error).

### 4.2 Hook into completion
In `check_completion`, immediately after `plan.status = COMPLETED` / `plan.completed_at`
/ `case_report` are set, call `record_buyplan_purchase_history(plan, db)`.

### 4.3 Migration — `buy_plans_v3.purchase_history_recorded_at`
New nullable `UTCDateTime` column. Standard Alembic up/down (revision id ≤ 32 chars).
Model: add the column to `BuyPlan` (`app/models/buy_plan.py`).

### 4.4 Immediate match refresh — `refresh_matches_for_cards(card_ids, db)`
For each affected card, find live offers (status active / not stale) and call
`find_matches_for_offer(offer.id, db)` (bounded to the most recent N offers per card, N=5,
to cap cost). The engine's existing dedup prevents duplicate matches. Best-effort; logged.
Rationale: a purchased part we already hold stock for should surface a match *now*, not in
up to 4 hours.

### 4.5 Retire legacy CPH write hooks
Remove the `upsert_purchase` calls (and the now-dead `_record_offer_won_history` /
`_record_quote_won_history` helpers) from `app/routers/crm/offers.py` and
`app/routers/crm/quotes.py`. Existing legacy CPH rows are left in place (harmless; handled
by aggregation). Update/remove the corresponding tests.

### 4.6 Engine aggregation — `_find_matches`
Replace "dedup by company_id, first wins" with: group the card's CPH rows by `company_id`;
per company aggregate `last_purchased_at = max`, `purchase_count = sum`,
`avg_unit_price = count-weighted average`, `last_unit_price = the most-recent row's`. Score
once per company from the aggregate. This makes multi-source history coherent and ensures
the strongest/newest purchase signal drives the score.

### 4.7 Backfill — `app/management/backfill_buyplan_cph.py`
Walks `BuyPlan` where `status==COMPLETED` and `purchase_history_recorded_at IS NULL`,
calls `record_buyplan_purchase_history` for each, prints a summary. Idempotent via the
flag. Run once post-deploy: `docker compose exec app python -m app.management.backfill_buyplan_cph`.

## 5. Data flow

```
buy plan lines all VERIFIED + SO approved
        │  check_completion → status=COMPLETED, completed_at set
        ▼
record_buyplan_purchase_history(plan)
        │  per VERIFIED line → upsert_purchase(source="buy_plan", unit_price=unit_sell, …)
        │  set purchase_history_recorded_at
        ▼
refresh_matches_for_cards(affected)  → find_matches_for_offer for live stock
        ▼
ProactiveMatch rows appear on the account owner's Proactive tab
(4h scan continues to handle newly-arriving offers independently)
```

## 6. Error handling & edge cases

- CPH side effects never block completion (best-effort, logged) — mirrors today's hooks.
- No customer_site/company on the requisition → skip, set flag, log.
- Line with no resolvable card (requirement + offer both null/cardless) → skip line, log.
- `unit_sell` is None → record purchase with `unit_price=None` (counts the purchase;
  margin stays neutral until a priced purchase exists).
- A completed plan later cancelled: **not** decremented (out of scope; completion is
  terminal and purchases are effectively immutable). Documented, not handled.
- Re-entrancy: `check_completion` transitions once; the flag guards backfill/any re-run.
- Immediate refresh (4.4) matches the purchased part against **all** customers with CPH
  for it — including the one who just bought, if we still hold stock. That is acceptable
  (a legitimate "more available" reorder nudge) and the 21-day post-*send* throttle still
  applies once an offer goes out. A "suppress recently-purchased customer" rule is **out
  of scope** here; revisit in SP2 if it proves noisy.

## 7. Testing (TDD — write first)

1. Completing a plan records one CPH row per VERIFIED line with the right
   company/card/`unit_price=unit_sell`/qty/`source="buy_plan"`/`source_ref=SO#`.
2. CANCELLED lines are not recorded.
3. Idempotent: calling the recorder twice does not double-count; flag is set.
4. Part resolves via requirement; falls back to offer; unresolvable line skipped while
   siblings still record.
5. Missing company → no rows, flag set, warning logged.
6. `refresh_matches_for_cards` creates a ProactiveMatch when a live offer + CPH owner exist.
7. Offer-won / quote-won no longer write CPH (retired-hook regression).
8. Engine aggregation: a company with two CPH rows (legacy + buy_plan) scores from the
   aggregate (summed count, max date, weighted avg).
9. Backfill records COMPLETED plans and is idempotent on re-run.
10. Migration upgrade→downgrade→upgrade.

## 8. Rollout

1. Land migration + code behind tests; full suite green.
2. Deploy (`./deploy.sh`).
3. Run the backfill command once; verify CPH rows + that the Proactive tab reflects real
   completed-deal history.
4. Update `docs/APP_MAP_INTERACTIONS.md` (CPH now fed by buy-plan completion; legacy hooks
   retired) and `docs/APP_MAP_DATABASE.md` (new column).

## 9. Program roadmap (this is sub-project 1 of 4 — others out of scope here)

The proactive tab drives revenue only when matches are **present** (this spec),
**trustworthy**, reps are **prompted**, and ROI is **visible**:

- **SP1 — Automated data feed (this spec).** Tab fills itself from buy-plan completion.
- **SP2 — Rep workflow completeness.** Editable sell price on Prepare (service already
  accepts `sell_prices`); a "do not offer" button (backend exists, no UI); fix the 7-day
  visibility window vs 30-day status divergence so matches don't silently vanish.
- **SP3 — Adoption & nudges.** Live nav badge (`hx-trigger="every Ns"`), a daily "new
  proactive matches" digest/notification, and an onboarding empty-state.
- **SP4 — Revenue visibility.** Surface the already-computed `gross_profit` /
  `anticipated_revenue` / pipeline on the Scorecard; attribute won revenue back to
  proactive offers.

Each is its own spec → plan → implementation cycle.

## 10. Out of scope

SP2–SP4 above; any Salesforce/Acctivate importer (SFDC retiring; the buy-plan feed makes
external purchase-history import unnecessary). The scorecard display bugs were already
fixed and deployed separately (commit `0ab421a5`).
