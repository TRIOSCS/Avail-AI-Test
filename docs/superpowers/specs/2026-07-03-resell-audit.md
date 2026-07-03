# Resell Module — End-to-End Audit & Prioritized Rework Plan

**Date:** 2026-07-03  **Scope:** the full Resell workflow (`/v2/resell`) — router, services, models, templates.
**Mode:** READ-ONLY audit. No code changed, no migrations run.

## What the module is

The reverse of sourcing. A customer (the **seller / stock holder**) has excess inventory; Trio
finds **buyers**. The end-to-end flow:

1. **Intake** — an owner (`can_post`: sales/trader/manager/admin) creates an `ExcessList` for a
   `Company`, then adds `ExcessLineItem`s by hand (`add_line`) or bulk import (`import-preview` →
   `import-confirm`). Each line resolves a `MaterialCard`.
2. **Publish** (`excess_mirror.publish_list`) — flips `draft → open`, stamps `open_at`, and
   **live-mirrors** every active line into the `Sighting` table (via a system-owned "Customer
   Excess" virtual requisition) so the parts surface to the existing matcher.
3. **Collect inbound offers** — buyers (`can_offer`) submit `ExcessOffer`s to *buy* the parts,
   either `per_line` (with `ExcessOfferLine` rows, part-number-matched, unmatched rows queued) or
   `take_all`. First offer flips the list `open → collecting`. Each matched line gets a best-price
   rollup (`recompute_line_rollup`).
4. **Offer out** (the trader→buyer inverse) — the owner opens the buyer panel
   (`buyer_affinity_service.rank_buyers_for`), sends email (reusing `send_batch_rfq`) or logs a
   manual channel, tracked as `ExcessOutreach`. Buyer replies land via the inbox poll
   (`email_service` Tier-2.5 → `resell_outreach_service.record_response`) and advance the outreach;
   offer extraction is a manual "Convert to offer".
5. **Award** (`award_offer`) — owner picks a winning `ExcessOffer` → `won`, its lines →
   `awarded`, mirror retired, `BuyerScore` recomputed. `unaward_offer` reverses it.
6. **Bid back** (`bid_back_service.build_bid_back`) — owner assembles selected lines into a clean,
   identity-stripped `CustomerBid` PDF (Trio's offer to *buy* the excess from the customer).
7. **Close** (`close_list`) — owner flips to `bid_out`, stamps `close_at`.

The architecture is genuinely good: fat-service / thin-router split, additive Sighting mirror with a
well-reasoned dedup key, clean whitelist export, and a real who-to-offer intelligence layer. The
findings below are correctness/workflow gaps layered on a solid skeleton, **not** a rewrite.

---

## Findings (ranked)

### HIGH

#### H1 — "Best offer" is computed as the **lowest** buyer bid (min), inverting the money decision
- **Where:** `app/services/excess_service.py:557-564` (`recompute_line_rollup`: `best = min(priced, key=... unit_price)`); surfaced by `app/templates/htmx/partials/resell/offer_compare.html:75-83,105-106` ("Best" badge + emerald spread dot), `_offers.html:136-141`, `_build_bid.html:65,79-80` (seed price); enshrined by `tests/test_resell_rollup.py:81-90`.
- **What's wrong:** `ExcessOffer` is *"an inbound offer from another broker to **BUY**"* (`models/excess.py:138-146`). For a buy-side auction the best bid is the **highest** `unit_price` — that is the most money for the parts and the correct award target. The rollup takes `min()` (copied verbatim from sourcing, where min = cheapest *supply* — see the docstring "Mirrors ... best = min price"). Consequences: the **"Best" badge the trader uses to pick a winner points at the *cheapest* bidder**, and the **`CustomerBid` seed price (`best_offer_unit_price`) is the weakest bid**, understating both what Trio can resell for and what it should offer the customer. This is a direct money/award defect that becomes real the moment two buyers bid different prices.
- **Fix:** Change `min` → `max` in `recompute_line_rollup` (keep the None-filter and distinct-offer count). Re-label the spread bar so the "best" dot sits at `max_price`. Update `test_resell_rollup.py` to assert max (the current test encodes the bug). **First** confirm the direction with the product owner in one line — but every downstream consumer (award highlight, bid-back seed) argues for max.

#### H2 — Customer identity leaks to non-owners through the free-text list **title**
- **Where:** `app/templates/htmx/partials/resell/_lists.html:53` and `detail.html:29` render `el.title` unconditionally; the "open" lens serves lists owned by others (`resell.py:305-311`).
- **What's wrong:** The module's core guarantee is customer-identity hiding — the "open" lens nulls `customer_name` and shows "Anonymized posting" (`_lists.html:54-58`, `_list_card` at `resell.py:125`). But `title` is owner-entered free text shown to every offerer, and traders will naturally name lists after the customer (e.g. "Acme Corp — surplus FPGAs"). The anonymization is defeated by the one field nobody sanitizes. The publish confirm even says "become visible to brokers" without flagging that the *title* is among them.
- **Fix:** Either (a) don't show the raw title in the "open" lens / non-owner detail — show a neutral label ("Excess offer #{{ el.id }}" + line count), keeping the title owner-only; or (b) add a separate owner-only `internal_title` and a required public `public_title` with copy guidance. Option (a) is smaller and ships first. Add a note on the create form that the title is broker-visible.

---

### MEDIUM

#### M1 — Awarding an offer never closes the **competing** offers (`lost` status is dead)
- **Where:** `award_offer` (`excess_service.py:635-694`) flips only the winner to `won`; `ExcessOfferStatus.LOST` is defined (`constants.py:177`) but **never assigned anywhere** (grep: zero writers).
- **What's wrong:** After awarding, the losing offers on the same line stay `open`. They keep counting in "offers to review" (`_stat_strip`, `resell.py:166-174`), keep rendering as live in the Offers tab, and — because the rollup counts `open` + `won` (`_ROLLUP_OFFER_STATUSES`, `excess_service.py:418`) — a losing offer can still *own* `best_offer_id`. The trader has no signal that these are decided.
- **Fix:** In `award_offer`, after the winner flips, mark the other `open` offers touching the awarded lines `lost` (per-line: only offers whose matched lines are now fully awarded; take_all: all other open offers on the list). Exclude `lost` from the rollup and the review counts. Add the inverse in `unaward_offer` (re-open the ones it closed).

#### M2 — No way to **withdraw** an inbound offer from the UI (service exists, no route)
- **Where:** `withdraw_offer` (`excess_service.py:575-596`) is fully implemented and unit-tested (`test_resell_rollup.py:130-158`) but has **no router endpoint and no button** (grep: only the def).
- **What's wrong:** A buyer who wants to retract a bid, or an owner clearing a stale/erroneous offer, has no path. Offers accumulate permanently; the only state change available is award/unaward.
- **Fix:** Add `POST /api/resell/{list_id}/offers/{offer_id}/withdraw` (owner-gated, delegating to `withdraw_offer`) and a "Withdraw" action in the Offers tab / offer-compare, re-rendering the Offers + OOB lines/chips like award does.

#### M3 — `late` offers are never flagged; offers are accepted after close as plain `open`
- **Where:** `submit_offer` always sets `status=OPEN` (`excess_service.py:465`); the router accepts offers on any posted status incl. `bid_out`/`awarded` (`resell.py:66-71, 870-872`); `ExcessOfferStatus.LATE` is in `_UNACTIONED_OFFER_STATUSES` (`resell.py:73`) but **never assigned**.
- **What's wrong:** Spec (`constants.py:168-180`) says an offer landing after the list closed is *accepted but flagged `late` and queued for review*. Instead it lands indistinguishable from an on-time `open` offer. The `late` state is dead UI plumbing.
- **Fix:** In `submit_offer` (and `_link_inbound_offer`), set `status=LATE` when `excess_list.status in {bid_out, awarded}` at submit time. Surface a "late" chip in the Offers tab. (Keeps the "never drop a deal" rule while making lateness visible.)

#### M4 — The `CustomerBid` lifecycle is stuck at `draft`; no send / accept / reject, no revisioning
- **Where:** `build_bid_back` always creates `status=draft` (`bid_back_service.py:65-68`); `CustomerBidStatus.SENT/ACCEPTED/REJECTED` (`constants.py:220-223`) are **never assigned** (grep empty); `revision` defaults to 1 and is never bumped (`models/excess.py:254`); each assemble inserts a **new** `CustomerBid` and `_latest_bid` just returns the newest (`resell.py:526-528`).
- **What's wrong:** The documented bid-back lifecycle (draft → sent → accepted/rejected) is entirely aspirational. Re-assembling silently orphans the prior bid instead of revising it, so `customer_bids` grows a pile of draft rows with no audit chain. There's a "Download PDF" but no "Mark sent", so the deal outcome is never recorded and `BuyerScore`/reporting can't see accepted bids.
- **Fix:** Add a "Send bid" action (`draft → sent`, optionally emailing the PDF) and accept/reject controls (owner records the seller's answer). On re-assemble of a list that already has a non-terminal bid, either bump `revision` on the same row or supersede with an explicit `superseded` status rather than leaving duplicate drafts.

#### M5 — `closed` / `expired` list states and `close_at` have no lifecycle; `close_list` is under-guarded and leaves the mirror live
- **Where:** `ExcessListStatus.CLOSED`/`EXPIRED` (`constants.py:161-162`) are **never assigned** (grep empty); no scheduler job expires lists past `close_at` (grep of `app/jobs`/`scheduler.py` empty). `close_list` (`excess_service.py:744-762`) has no status precondition and does **not** call `sync_list_mirror`.
- **What's wrong:** (a) `close_list` flips *any* status to `bid_out` — you can "close" a `draft` (never published, never mirrored) or re-close an `awarded` list; the button is hidden for those but the endpoint isn't guarded. (b) After close, the lines stay `available` and **still advertise as live supply** in the Sighting matcher — the posting window "ended" only in name. (c) The `closed`/`expired` terminal states and the "expired" pill (`_lists.html:15,67`) are unreachable dead UI; nothing ever consumes `close_at` to auto-expire.
- **Fix:** Guard `close_list` to `open`/`collecting` only (409 otherwise). Decide whether close should retire the mirror (if "closed = no longer sourcing supply from this list", call `sync_list_mirror` — but see note: retirement currently keys off line status, so you'd need a list-level "posting closed" gate in `_line_is_active`). Add a small nightly job to flip past-`close_at` unresolved lists to `expired`, or remove the dead states from the enum + pills if out of scope.

#### M6 — No notification to the owner on an inbound offer or a buyer reply
- **Where:** No `Notification`/`NotificationType` usage anywhere in the resell flow (grep of `resell.py`, `excess_service.py`, `resell_outreach_service.py`, `bid_back_service.py` empty), despite `NEW_OFFER` / `OFFER_PENDING_REVIEW` / `BID_RECEIVED` existing (`constants.py:720-723`).
- **What's wrong:** When a broker submits an offer, or an emailed reply creates one via `record_response`, the owner learns of it only by reloading the workspace and reading the stat strip. For a time-boxed posting window this is a real workflow gap — the whole point of collecting offers is to act on them promptly.
- **Fix:** Emit a notification to `excess_list.owner_id` in `submit_offer` and in `record_response`/`_link_inbound_offer` (use `NEW_OFFER`/`BID_RECEIVED`). Deduplicate per (list, buyer) like the outreach follow-up task already does.

#### M7 — Resell sub-partials are gated by `require_user`, not `require_access(RESELL)`
- **Where:** Only the workspace shell uses `Depends(require_access(AccessKey.RESELL))` (`resell.py:265`). `resell_lists` (`:293`), `resell_detail` (`:363`), and the tab bodies all use `Depends(require_user)`.
- **What's wrong:** Any authenticated user (even without Resell access) can hit `/v2/partials/resell/lists?lens=open` and `/v2/partials/resell/{id}` directly and enumerate every posted list (anonymized, but see H2 re: title leak). Owner-only tabs (offers/build/outreach/compare) are separately gated, so the exposure is bounded — but the access model is inconsistent with the page gate.
- **Fix:** Apply `require_access(AccessKey.RESELL)` to the list/detail/tab read endpoints too (mutations already carry ownership guards). Keep the owner checks on top.

#### M8 — N+1 queries in the left list and the buyer panel
- **Where:** `_list_card` (`resell.py:106-130`) runs a line-items `.all()` **plus** a filtered offer-count query **per list**, called in a loop over every list (`resell.py:322`) → ~2N queries for N lists. `_suggestion_rows` (`resell.py:987-1001`) calls `overlap_warning` (its own query) per ranked buyer → up to 20 extra queries per panel open. `_stat_strip` (`resell.py:133-191`) issues 6 separate count queries.
- **What's wrong:** Scales linearly with list/buyer count; the left list re-renders on every filter keystroke (`hx-trigger="input changed delay:300ms"`), multiplying the cost. Masked on SQLite tests; bites on live PG.
- **Fix:** Batch the coverage/offer-count rollups with a single grouped query keyed by `excess_list_id`; compute overlap for all ranked buyers in one query; fold the 6 stat counts into one `GROUP BY status` aggregate plus a couple of offer aggregates.

#### M9 — Award has no row-level locking (lost-update race on concurrent awards)
- **Where:** `award_offer` reads line statuses, checks "already awarded", then writes, with no `with_for_update` (`excess_service.py:660-673`).
- **What's wrong:** Two concurrent awards touching overlapping lines can both pass the `already_awarded` check before either commits, double-awarding a line (and double-firing the buyer-score/mirror hooks). Low probability at today's single-user stage, but the module is explicitly on the multi-user go-live path (see project memory).
- **Fix:** `SELECT ... FOR UPDATE` the affected `ExcessLineItem`s (and/or the list row) at the top of `award_offer`/`unaward_offer`, or add a partial unique constraint enforcing one `awarded` winner per line.

---

### LOW

#### L1 — Inbound offer entry is single-line only
- **Where:** `offer_form.html` (per-line fields are one MPN/qty) + `resell_submit_offer` (`resell.py:847-902`) parses exactly one row. The docstring references a "paste/upload funnel" reusing `import_preview`, but no offer-side UI reaches it.
- **Fix:** Allow multi-row paste for `per_line` offers (reuse the import preview grid), so a buyer bidding on 10 parts doesn't submit 10 times. Same friction on "Convert to offer" (`resell.py:1335-1380`).

#### L2 — Non-positive quantity returns 500, not 400
- **Where:** `ExcessLineItem`/`ExcessOfferLine` `@validates("quantity")` raise `ValueError` (`models/excess.py:124-128, 217-221`); `resell_add_line` passes `quantity: int` straight through (`resell.py:713-748`), so `0`/negative reaches the validator as an unhandled 500.
- **Fix:** Validate `quantity > 0` in the router (400) before the model, or map the `ValueError` to `HTTPException(400)`.

#### L3 — `confirm_import` trusts client-submitted `rows_json` without re-validation
- **Where:** `resell_import_confirm` (`resell.py:790-813`) → `confirm_import` (`excess_service.py:377-403`) inserts the posted rows without re-running `_parse_import_row`.
- **What's wrong:** The previewed rows are echoed back through a hidden field and imported verbatim; a hand-crafted POST can inject part numbers/prices/conditions bypassing preview validation. Owner-scoped and low-impact (they can add lines anyway), but it trusts the client.
- **Fix:** Re-parse/re-validate each row server-side in `confirm_import` (reuse `_parse_import_row`) instead of trusting the round-tripped JSON.

#### L4 — Dead / inconsistent UI states
- `closed`/`expired` status pills (`_lists.html:15,66-67`) are never reachable (see M5). The stage filter offers `bid_out`/`awarded` but not `closed`. `offer_count` means different things in the list card (open+late only, `resell.py:114-121`) vs the detail tab badge (all statuses, `resell.py:227`). Reconcile once the lifecycle in M1/M3/M5 is settled.

#### L5 — `rank_buyers_for` candidate query can load most of `vendor_cards` into Python
- **Where:** `buyer_affinity_service.py:259-262` — the candidate filter is `engagement_score IS NOT NULL OR commodity_tags IS NOT NULL OR id IN history`. On a populated CRM nearly every card has an engagement score, so the "bound the working set" comment doesn't actually bound much; commodity-tag matching then happens in Python.
- **Fix:** Push a stronger pre-filter (e.g. only history buyers + cards whose `commodity_tags` overlap the target commodities via a JSON containment/`EXISTS` on PG) and cap before materializing.

---

## Recommended rework plan (phased, each independently shippable)

**Phase 1 — Correctness & privacy (ship first).**
- H1 flip best-price to `max` (+ fix test, spread-bar label). *One-line logic change, high value.*
- H2 stop leaking the title in the open lens / non-owner detail.
- M7 gate the read partials with `require_access(RESELL)`.
- L2 quantity 400. L3 re-validate import-confirm.
These are small, mostly independent, and close the money + privacy holes.

**Phase 2 — Offer lifecycle completeness.**
- M1 close competing offers as `lost` on award (+ inverse on unaward).
- M2 add the withdraw endpoint + button.
- M3 flag `late` offers after close.
- M6 owner notifications on inbound offer / reply.
Coherent set — all about the inbound-offer state machine and its signals.

**Phase 3 — Bid-back & list lifecycle.**
- M4 `CustomerBid` send/accept/reject + revisioning.
- M5 guard `close_list`, decide mirror-on-close, add the `expired` job (or delete the dead states).
- L4 reconcile the pills/filters/counts once the lifecycle is final.

**Phase 4 — Performance & polish.**
- M8 batch the N+1s (list cards, stat strip, overlap). M9 award locking. L5 rank pre-filter.
- L1 multi-line offer entry.

Each phase is independently deployable; Phase 1 items can even go as separate small PRs.

---

## What's solid — leave alone

- **Sighting live-mirror** (`excess_mirror.py`) — the virtual-requisition design, the
  `(source_company_id, material_card_id, requirement_id)` upsert key that dodges the
  delete-by-`(requirement_id, source_type)` dedup trap, and the `EXCESS_VENDOR_LABEL` "never the
  customer name" discipline are careful and correct. Don't touch the keying.
- **Clean bid-back export** (`bid_back_service.bid_back_export_context`) — the explicit whitelist
  enforced at assembly (not template omission) is exactly right for the identity-hiding guarantee.
- **Reply adapter wiring** (`email_service.py:800-905`) — Tier-2.5 exact conv/msg match before the
  fuzzy fallbacks, inside the per-message savepoint (`commit=False`), with the auto-reply gate so an
  OOO doesn't falsely advance a buyer. Well-reasoned; keep the tier ordering.
- **Part-number-only matching + never-drop queue** (`submit_offer` / `_link_inbound_offer`) — the
  matched/unmatched/ambiguous classification and the "queued, never dropped" rule are consistent
  across both entry points and match the spec.
- **Empty states** (`_lists.html:87-108`) — first-run ("You haven't posted any…") + a "Post a list"
  CTA, plus lens- and filter-specific messages. Genuinely good; no change needed.
- **Capability model** (`can_post`/`can_offer`) and the owner/non-owner detail projection — the
  role-derived powers and the owner-only tab gating (offers/build/outreach/compare all 403 for
  non-owners) are clean; only the *read* partial gate (M7) and the *title* field (H2) need tightening.
- **Take-all award scope fix** (`_award_scope_items`, `excess_service.py:603-615`) — the comment
  documents a prior bug already fixed; the whole-list scope derivation is correct.
