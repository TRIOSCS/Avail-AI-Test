# Prospecting Module — Read-Only Audit & Rework Plan

**Date:** 2026-07-03
**Scope:** End-to-end prospecting workflow — discovery → enrich → qualify → own → convert,
including the ownership auto-sweep/reclaim machinery, background enrichment, scoring, and the
HTMX tab UI.
**Method:** Static read of routers/services/models/templates/jobs. No code changed, no migrations run.
Every finding cites `file:line` against the current tree.

---

## 1. Workflow map (as-built)

**Entry points into the pool (rows in `prospect_accounts`, "the pool only grows"):**

| Source | Writer | `discovery_source` |
|---|---|---|
| Manual domain add | `prospect_claim.add_prospect_manually` (`app/services/prospect_claim.py:599`) | `manual` |
| Monthly Explorium discovery | `prospect_scheduler.job_discover_prospects` → `prospect_discovery_explorium` | `explorium` |
| Monthly email mining | `prospect_discovery_email.run_email_mining_batch` | `email_mining` |
| "Send to prospecting" from CRM | `prospect_claim.send_company_to_prospecting` (`:240`) | `sent_back` |
| SP4 dormancy sweep | `prospect_reclamation.job_account_sweep_with_db` (`:44`) | `auto_sweep` |
| Reactivation surface | `prospect_reclamation.job_auto_surface_with_db` (`:260`) | `reactivation` |

**Status lifecycle** (`ProspectAccountStatus`, `app/constants.py:323`): `SUGGESTED → CLAIMED`
(release returns to `SUGGESTED`), `SUGGESTED → DISMISSED`. `CONVERTED` is defined but **never
written**; `"expired"` is written as a **raw string that is not an enum member** (see H4).

**Ownership is modeled twice and the two never sync:**
- **Company-level** `Company.account_owner_id` — set by prospect claim, SP4 sweep, `run_ownership_sweep`.
- **Site-level** `CustomerSite.owner_id` — used by the SITE_CAP guard, `run_site_ownership_sweep`,
  `claim_site`, and the "my accounts/sites" pages.
Claiming a prospect sets *company-level* ownership only; the site it creates is left unowned.

**Three sweeps touch ownership** (all gated, see H5):
- `ownership_service.run_ownership_sweep` — clears `account_owner_id→NULL` at 30/90 days, warns at day 23,
  **does not** park into the pool. (`email_jobs.py:42`, every 12h, `ownership_sweep_enabled`)
- `ownership_service.run_site_ownership_sweep` — clears `CustomerSite.owner_id` at 30 days. (daily 3AM)
- `prospect_reclamation.job_account_sweep_with_db` — parks the Company into the pool with a 30-day
  reclaim cooldown + notification at 90 days. (daily 1AM, `account_sweep_enabled`)

---

## 2. Findings (ranked by severity)

### CRITICAL

**C1 — Monthly contact-discovery job is dead: imports a function that does not exist.**
`app/services/prospect_scheduler.py:271`
```python
from app.services.prospect_contacts import run_contact_enrichment_batch
```
`run_contact_enrichment_batch` is defined **nowhere** in the repo (`prospect_contacts.py` only holds
classifier/util helpers, most of them themselves uncalled — see M14). Every run of `job_find_contacts`
raises `ImportError`, which the job's blanket `except Exception` (`:282`) swallows into `{"error": ...}`.
So the "find procurement contacts for high-fit prospects" step (the whole reason `contacts_preview`/DM
scoring exists) has **never run in production**. This starves the buyer-ready score, which weights
verified decision-makers heavily (`prospect_priority.py:48-63`).
**Fix:** implement `run_contact_enrichment_batch` in `prospect_contacts.py` (query
`status=SUGGESTED AND fit_score >= prospecting_min_fit_for_contacts`, call the real contact-finder,
persist `contacts_preview` + `readiness_signals['contacts_verified_count']`, commit), or repoint the
import at the function that actually enriches contacts. Add a job test that fails on `ImportError`
instead of swallowing it.

---

### HIGH

**H1 — The only "convert to opportunity" affordance is a broken link; the prospect is never marked converted.**
`app/templates/htmx/partials/prospecting/detail.html:99-105`
```html
<a href="/v2/requisitions?action=create&customer={{ prospect.name|urlencode }}"
   hx-get="/v2/partials/requisitions?action=create&customer={{ prospect.name|urlencode }}" ...>
  Create Requisition</a>
```
The target is the **list** route `requisitions_list_partial` (`app/routers/htmx/requisitions.py:69`),
whose signature accepts neither `action` nor `customer` — both params are silently ignored, so the
click just dumps the full flat requisitions list. The real modal route is
`/v2/partials/requisitions/create-form` (`:317`), and even *it* takes no prefill args (`:318-325`).
Net: the salesperson claims a prospect (which already created/linked a `Company` with
`prospect.company_id`), then the one handoff button drops them on an unrelated list with the customer
lost. The prospect also never transitions to `CONVERTED` (that enum value is dead — see H4/M4), so
there is no "this became a real opportunity" state and the account keeps showing as `claimed` forever.
**Fix:** point the button at `create-form` and teach that route + `unified_modal.html` to accept a
`company_id`/`customer_name` prefill; on requisition creation from a prospect, set
`ProspectAccount.status = CONVERTED`.

**H2 — Background enrichment wedges permanently if its worker dies mid-job.**
`app/routers/htmx/prospecting.py:585-594` and `:172`
The stale-guard `_enrich_is_stale` (`:58`) is applied **only** in the poll route
`enrich_status_partial` (`:621`), which self-heals the *view* but never writes the DB. It is **not**
applied where it matters:
- `enrich_prospect_htmx` restarts only `if ed.get("enrich_status") != "running"` (`:586`) — a status
  left at `"running"` by a crashed worker (container restart, OOM) is never cleared, so a new job is
  never started; the endpoint just returns the spinner (`enrich_state="running"`, `:598`).
- `_prospect_detail_ctx` sets `enrich_state = "running"` from the raw flag with no staleness check
  (`:172`), so the detail page keeps the **Enrich button disabled** (`detail.html:94`) indefinitely.
The `enrich_status.html` "Retry" button POSTs `/enrich`, which hits the same guard and loops back to
the spinner → stale → error toast, forever. No exit.
**Fix:** treat a stale `"running"` as restartable in `enrich_prospect_htmx` (`if state != "running" or
_enrich_is_stale(...)`) and clear/ignore it in `_prospect_detail_ctx`; or have `enrich_status_partial`
persist `enrich_status="error"` when it detects staleness so subsequent triggers see a terminal state.

**H3 — `run_enrichment_job` / batch enrichment can cascade a whole batch into errors (no rollback on the shared session).**
`app/services/prospect_free_enrichment.py:354-359, 489-517`
When invoked from `run_free_enrichment_batch` (`owns_session=False`), the error path returns
`{"error": ...}` **without `db.rollback()`**. A single failed `commit()` mid-loop leaves the shared
session in an aborted state, so every subsequent prospect's `commit()` raises `PendingRollbackError`
and the rest of the batch is lost.
**Fix:** `db.rollback()` in the `except` of `run_free_enrichment` (guarded so the caller still owns
`close()`), and continue the loop.

**H4 — `"expired"` is a black-hole terminal state: not an enum member, no UI, unreachable resurface.**
`app/services/prospect_scheduler.py:408`
```python
p.status = "expired"
```
`ProspectAccountStatus` has only `SUGGESTED/CLAIMED/DISMISSED/CONVERTED` (`app/constants.py:323`), so
this raw string violates the "always use the StrEnum" rule and creates an orphan status. The list
filter pills are hard-coded to `'', suggested, claimed, dismissed` (`list.html:76-88`) — there is **no
"expired" pill**, so expired rows become invisible and cannot be inspected or recovered from the UI.
Resurface is effectively unreachable too: `job_expire_and_resurface` only resurfaces rows with
`last_enriched_at > now-30d` (`:414-419`), but enrichment/re-scoring jobs only touch `SUGGESTED`
(`:303`, `:390`, `:480`), so an expired row is never re-enriched → its `last_enriched_at` never
refreshes → the 30-day window lapses → it is stuck forever.
**Fix:** add `EXPIRED` to `ProspectAccountStatus`, add a filter pill, and either include expired rows in
the enrich/refresh candidate sets or drive resurface off a signal recompute rather than
`last_enriched_at`.

**H5 — Two conflicting company-ownership sweeps; enabling both silently disables the SP4 park/cooldown/notify.**
`app/jobs/email_jobs.py:42-47`, `app/services/ownership_service.py:36-93`,
`app/services/prospect_reclamation.py:44-128`
`run_ownership_sweep` clears `account_owner_id→NULL` at `customer_inactivity_days` (30) with no pooling,
running every 12h. The SP4 `job_account_sweep_with_db` only considers Companies where
`account_owner_id IS NOT NULL` (`prospect_reclamation.py:62`) and parks them at
`account_sweep_inactivity_days` (90) with a cooldown + rep/manager notification. If both flags are on at
go-live, the plain sweep wins the race (12h cadence, 30-day threshold), nulls ownership first, and the
SP4 sweep never sees the account — so parking, the 30-day reclaim cooldown, and the loss-notification
email **never fire**. The two thresholds (30 vs 90) and two "pools" (unowned `Company` "open pool" vs
`ProspectAccount` pool) are also unreconciled: a company cleared by `run_ownership_sweep` lands in the
open pool but **not** the Prospecting tab (unless it happens to have a req/quote and the reactivation job
catches it).
**Fix:** pick one company-ownership sweep. Recommended: retire `run_ownership_sweep`'s *clearing* role
and make SP4 `job_account_sweep` the single owner-dormancy path (it already parks + notifies + cools
down); keep `run_ownership_sweep` only for the day-23 warning email if desired. Unify the threshold
setting.

**H6 — Monthly discovery ignores its rotation slice and runs the full 12-segment scan every time (~6× credits).**
`app/services/prospect_scheduler.py` (`get_next_discovery_slice` at `:108`, call at `:154-176`) vs
`app/services/prospect_discovery_explorium.py:376` (`run_explorium_discovery_batch`).
`get_next_discovery_slice` computes a segment/region slot, but `run_explorium_discovery_batch` takes no
segment/region argument and internally loops **all** `SEGMENT_SEARCH_PARAMS × REGIONS` (4×3) at 50 each
unconditionally. The slice only stamps the batch's label columns; its `intent_keywords` are never used,
and the rotation vocabulary (`"EMS / Electronics Mfg"`, etc.) doesn't even match the
`SEGMENT_SEARCH_PARAMS` keys. Result: ~600 Explorium credits/month instead of the intended ~1/6 slice.
**Fix:** pass the slice into `run_explorium_discovery_batch` and have it scan only that
segment/region set; reconcile the rotation labels with the actual param keys.

**H7 — Signal backfill hits a dead Explorium endpoint and always fails.**
`app/services/prospect_signals.py:128-140`
`enrich_missing_signals` POSTs to `{EXPLORIUM_BASE}/v1/businesses/search` with an
`Authorization: Bearer` header. Per this module's sibling discovery module
(`prospect_discovery_explorium.py:8-10`), `/v1/businesses/search` "was never a real Explorium route" —
the real API is `POST /v1/businesses` with an `api_key` header. Every call returns non-200 → `False`,
so `run_signal_enrichment_batch` Step 1 (`signals_added`) is always 0 for email-mined/manual prospects.
**Fix:** route through `app.connectors.explorium.discover_businesses`/`enrich_company` instead of the
hand-rolled POST.

**H8 — One duplicate domain aborts the entire monthly discovery persist.**
`app/services/prospect_scheduler.py` (`_persist_discovery_results` ~`:45-69`, dedup window at `:175`)
`ProspectAccount.domain` is `unique=True, nullable=False` (`app/models/prospect_account.py:25`). Dedup
relies on `existing_domains = {...}.limit(10000)` plus in-batch `seen_domains`. Once the pool exceeds
10 000 rows (or a domain was created by an earlier step in the same job), a re-discovered domain slips
through, the batch `db.commit()` raises `IntegrityError`, and the `except` rolls back the **whole
batch** → 0 prospects saved that month. There is no per-row `IntegrityError` handling.
**Fix:** insert with `ON CONFLICT (domain) DO NOTHING`, or catch per row and continue; drop the
`.limit(10000)` dedup cap.

**H9 — SITE_CAP claim guard is dead: it counts the wrong ownership axis.**
`app/services/prospect_claim.py:25-34, 99, 154-155`
`claim_prospect` enforces `_active_site_count(db, user_id) >= SITE_CAP` (200), which counts
`CustomerSite.owner_id == user_id`. But claiming sets **company-level** `Company.account_owner_id`
(`:113/:127/:149`) and creates the HQ `CustomerSite` with **no `owner_id`** (`:154`). So a claim never
increments the site count and the cap never trips — a user can claim unlimited prospects. The
anti-hoarding control is silently a no-op.
**Fix:** count what claim actually assigns (owned Companies, or set `CustomerSite.owner_id` on claim so
the site cap is meaningful). Decide which ownership axis is canonical (see M11) and enforce against it.

---

### MEDIUM

**M1 — Claim silently swallows the domain-collision warning.**
`app/routers/htmx/prospecting.py:460`, `app/services/prospect_claim.py:104,123-136,192-194`
On a domain collision, `claim_prospect` links the prospect to a *different* existing Company and returns
`{"warning": "Linked to existing company '<X>' (same domain)"}`. The router discards the return value
entirely and toasts a flat `"Claimed <name>"`. The user is never told their claim merged into another
account (possibly a different company name).
**Fix:** capture the result and surface `warning` in the toast when present.

**M2 — No per-user scoping: every rep sees (and can act on) every rep's claimed accounts.**
`app/routers/htmx/prospecting.py:262-266, 327-332`
The list query filters by `status` only — never `claimed_by == user`. The "Claimed" pill shows all
users' claimed prospects to everyone, and there is no "My prospects" view. This contradicts the
See-All/See-Mine scoping the Approvals module got, and matters for the multi-user go-live.
**Fix:** add a `mine` toggle (filter `claimed_by == user.id`) mirroring the Approvals scope pattern.

**M3 — Reactivation surfaces past customers as ice-cold (fit=0/readiness=0) and never fills historical context.**
`app/services/prospect_reclamation.py:326-334`
`job_auto_surface_with_db` finds unowned Companies that already have a `Requisition`/`Quote` and inserts
`ProspectAccount(fit_score=0, readiness_score=0)` with **no `historical_context`**. In fact **nothing in
`app/` ever writes `historical_context`** (grep for assignment is empty — only Salesforce import, which
is pending). So `build_priority_snapshot`'s "Previous Trio customer"/"quote history" boosts
(`prospect_priority.py:89-101`), `prospect_scoring.apply_historical_bonus`, and
`prospect_screening`'s history checks are **dead in the live flow**, and the reactivation job — which
already queried the reqs/quotes — throws that evidence away, so genuine warm accounts render as
zero-score.
**Fix:** in the reactivation job, populate `historical_context` (`bought_before`, `quoted_before`,
`quote_count`, `last_activity`) from the same reqs/quotes it filtered on, and apply
`apply_historical_bonus` (see M8) so past customers score warm.

**M4 — `CONVERTED` status is defined but never written.**
`app/constants.py` (`ProspectAccountStatus.CONVERTED`) — no writer anywhere in `app/` (the only
`.CONVERTED` writes are `ProactiveOfferStatus`, a different enum). There is no terminal "promoted to
opportunity" state, so claimed accounts never leave the `claimed` bucket even after becoming real deals.
**Fix:** set `CONVERTED` when a requisition/opportunity is created from a prospect (ties into H1).

**M5 — Default `ai_match_desc` sort loads the whole pool into memory every request.**
`app/routers/htmx/prospecting.py:275-296, 352`
The default sort does `rows = base.all()` over all `SUGGESTED+CLAIMED`, screens + sorts + paginates in
Python, and passes **every** screened-out row to the template as `screened_out_prospects` (rendered
unpaginated in the collapsed bucket, `list.html:163-198`). "The pool only grows," so this is O(N) row
hydration + O(N) DOM on each list load. The `buyer_ready_desc` sort already shows the right pattern
(SQL `order_by` on the persisted `buyer_ready_score` cache, `:304-310`).
**Fix:** persist a `trio_match`/`opportunity`/`readiness` composite (or reuse `buyer_ready_score`) and
sort/paginate in SQL; cap/paginate the screened-out bucket.

**M6 — `_prospect_stats_ctx` loads all suggested rows and snapshots each, on every action.**
`app/routers/htmx/prospecting.py:176-202`
`db.query(ProspectAccount).filter(status==SUGGESTED).all()` then `build_priority_snapshot(p)` per row.
This runs on the lazy stats load *and* OOB after every claim/dismiss/release. O(N) per grid action.
**Fix:** compute the KPIs in SQL — `is_buyer_ready`/`call_now` map onto the persisted
`buyer_ready_score`/`readiness_score` columns; `screened_out` onto a JSONB predicate or a cached flag.

**M7 — `enrich` has no row lock: concurrent clicks start duplicate background jobs.**
`app/routers/htmx/prospecting.py:585-594`
Read-check-write on `enrichment_data['enrich_status']` without `with_for_update`; two near-simultaneous
requests both see `!= "running"` and both spawn `run_enrichment_job`.
**Fix:** `SELECT ... FOR UPDATE` the prospect (or a DB-level guard) before flipping to `running`.

**M8 — `apply_historical_bonus` / `calculate_composite_score` are dead; the scheduler re-implements the weighting inline (drift).**
`app/services/prospect_scoring.py:454-489`, `app/services/prospect_scheduler.py:335-336`
`job_refresh_scores` recomputes fit/readiness but never calls `apply_historical_bonus`, and hand-codes
the 60/40 composite instead of `calculate_composite_score`. The shared functions are referenced only by
tests → guaranteed drift.
**Fix:** call the shared scorers from the scheduler and apply the historical bonus in both the persist
and refresh paths.

**M9 — `find_similar_customers` re-scans all owned companies per prospect (N+1 in the monthly job).**
`app/services/prospect_signals.py:293-300`, called at `:659`
Each prospect triggers a fresh `db.query(Company).filter(...).all()` full scan (O(P×C)).
**Fix:** load owned companies once before the loop and pass them in.

**M10 — `send_company_to_prospecting` + SP4 sweep double-commit leaves a cooldown-less parked prospect on crash.**
`app/services/prospect_claim.py:297` then `app/services/prospect_reclamation.py:105-120`
The service commits the ownership-clear + pool row, then the sweep sets `swept_at` /
`reclaim_blocked_until` / `discovery_source="auto_sweep"` and commits **again**. If the process dies
between the two commits, the account is unowned and in the pool but with **no `swept_at`/cooldown** — it
looks like an ordinary `suggested` prospect and the former owner can immediately re-claim it with no
cooldown and no notification.
**Fix:** set the swept provenance inside the same transaction as the park (pass the swept fields into
`send_company_to_prospecting`, or wrap both in one commit).

**M11 — Company-level vs site-level ownership are never reconciled.**
`app/services/prospect_claim.py:113/149/154` vs `app/services/ownership_service.py` (site sweep/claim)
Claim writes `Company.account_owner_id`; the "my accounts/sites" pages, `run_site_ownership_sweep`, and
`claim_site` operate on `CustomerSite.owner_id`. A claimed prospect's company is owned while its site is
unowned, so it can appear in the site "open pool" the moment it's created. Two parallel ownership models
with no sync is a persistent source of the confusion behind H5/H9.
**Fix:** choose one canonical ownership axis for the prospecting flow and drive all sweeps/caps/pages
off it.

**M12 — reclaim/reassign endpoints bypass the module-access gate.**
`app/access_paths.py:47`
`ModuleAccessMiddleware` only guards the `/v2/partials/prospecting` prefix. The reclaim/reassign
endpoints live under `/v2/partials/prospects/...` (plural, no `-ing`), which does not match, so they
carry only `require_user` + service-level ownership checks. The service checks hold (former owner /
admin / manager), so this is not an open door, but a user with the PROSPECTING key revoked can still
invoke them.
**Fix:** add `/v2/partials/prospects` to `_GUARDED_BASES`, or fold these routes under the `prospecting`
prefix.

**M13 — Manual add is a check-then-insert TOCTOU that 500s on a duplicate race.**
`app/services/prospect_claim.py:611-637`, `app/routers/htmx/prospecting.py:401`
`add_prospect_manually` does `first()`-then-`add()`-`commit()` with no `IntegrityError` guard; a
concurrent add of the same domain raises `IntegrityError`, which the router's `except (ValueError,
RuntimeError)` does not catch → 500.
**Fix:** insert inside a SAVEPOINT and adopt the existing row on `IntegrityError` (the pattern
`send_company_to_prospecting` already uses at `:278-295`).

**M14 — `prospect_contacts.py` is an unused module with a wrong header and a latent date bug.**
`app/services/prospect_contacts.py`
`classify_contact_seniority`, `mask_email`, `_is_personal_email`, `_is_new_hire` have zero call sites
(the header's claimed callers don't import them), and the module duplicates seniority/personal-domain
logic already in `prospect_free_enrichment.py`. `_is_new_hire` (`:149`) uses
`now.replace(month=max(1, now.month-6))`, which mis-computes "6 months ago" for months ≤6 and raises
`ValueError` on day-31→shorter-month.
**Fix:** delete or consolidate into the live enrichment module; if kept, use
`now - timedelta(days=182)`. Note this is also where C1's missing `run_contact_enrichment_batch` belongs.

**M15 — Discovery `credits_used` is never written → health report always reports 0.**
`app/services/prospect_scheduler.py` (batch create/complete vs read at `:506-509`)
`run_explorium_discovery_batch` computes `credits_est` but discards it; `DiscoveryBatch.credits_used`
keeps its `0` default, so `job_pool_health_report`'s `credits_used_this_month` is always 0 — no cost
visibility on the biggest credit spender (H6).
**Fix:** return `credits_est` and set `batch.credits_used` before the final commit.

**M16 — Email-discovery exclusion sets are truncated; vendor/prospect exclusions leak.**
`app/services/prospect_discovery_email.py:86-92`
`VendorCard.emails` and `ProspectAccount.domain` exclusion sets are capped at `.limit(5000)` while
`customer_domains` (`:74-83`) is correctly unbounded. Past 5000 rows, known vendor/prospect domains slip
the exclusion and get re-mined as "new" (then hit H8's abort).
**Fix:** drop the `.limit(5000)` or stream distinct values.

**M17 — Dismiss puts business logic in the router and never captures a reason.**
`app/routers/htmx/prospecting.py:501-516`; `_card.html:159-166`, `detail.html:76-81`
`dismiss_prospect_htmx` mutates the model and commits inline (violates "keep routers thin"). The dismiss
buttons post no `reason`, so `dismiss_reason` is always defaulted to `"other"` — the model field exists
but the UI can't populate it, and dismiss (unlike release) has no `hx-confirm`.
**Fix:** move the transition into `prospect_claim` (a `dismiss_prospect` service), add a reason
selector, and add a confirm.

**M18 — `discover_companies_with_signals` drops a full 50-row page on a non-string intent topic.**
`app/services/prospect_signals.py:171`; same pattern `prospect_discovery_explorium.py:186`
`kw in t.lower()` assumes every `intent_topics` element is a `str`; a dict/None raises `AttributeError`,
and the broad `except` (`:142`) discards all 50 records for that segment/region.
**Fix:** `if isinstance(t, str) and kw in t.lower()`.

**M19 — `ProviderQuotaError` is swallowed in discovery and doesn't trip the circuit.**
`app/services/prospect_discovery_explorium.py:141-143`
The broad `except Exception` treats a 402/429 quota error like any failure (returns `[]`), so discovery
keeps issuing all 12 slice calls after the quota is exhausted. The email path
(`prospect_discovery_email.py:194-196`) correctly trips the circuit.
**Fix:** catch `ProviderQuotaError` first and short-circuit the remaining slices.

---

### LOW

- **L1** — `prospect_signals.py:619-653`: Step 1 and Step 2 of `run_signal_enrichment_batch` each run the
  same `suggested AND fit>=min` query, and Step 1 re-fetches each row via `db.get` (`:32`). Fetch ids
  once and reuse.
- **L2** — `prospect_free_enrichment.py:332-336`: within-batch news dedup uses a pre-loop
  `existing_types` set that isn't updated as items are appended, so two fresh items sharing the first
  50 chars both persist.
- **L3** — `prospect_screening.py:307-324`: the `insufficient_data` verdict is written without a
  `grounding_fingerprint`, so it re-runs every enrichment pass (no LLM cost, just churn).
- **L4** — `prospect_scoring.py:124-131`: `SIZE_BRACKETS` is non-monotonic and only correct because of
  the `hi is None` special-case ordering in `score_company_size` (`:215`); a reorder would misbucket
  10001+ companies. Sort and make the unbounded case explicit.
- **L5** — `prospect_signals.py:49-72`: `enrich_with_intent`/`_hiring`/`_events` are dead (no callers).
- **L6** — `prospect_warm_intros.py:227-264`: `enrich_warm_intros_batch` is dead (tests only) and N+1
  if used; the live path is `detect_warm_intros` per prospect. Remove or batch.
- **L7** — `prospect_scheduler.py:166/235`: a hard failure after the batch is committed as `running`
  leaves it `running` forever (rotation filters `status=="complete"`, so harmless but pollutes audit).
  Mark `failed` in the outer `except`.
- **L8** — Discovery reads the Explorium key from `settings.explorium_api_key`
  (`prospect_discovery_explorium.py:91`) while email enrichment reads
  `get_credential_cached("explorium_enrichment", ...)`; if only one is configured the two paths
  disagree on availability. Confirm intentional.

---

## 3. Recommended rework plan (phased, each phase independently shippable)

**Phase 0 — Stop the silent bleeding (correctness, no schema).**
C1 (implement/repoint `run_contact_enrichment_batch` + make the job fail loud), H3 (rollback on batch
error), H7 (real Explorium endpoint for signals), H8 (per-row `ON CONFLICT`/try-continue on discovery
persist), H2 (stale-enrich restart). Each is a localized fix with a regression test; no migration.

**Phase 1 — Fix the ownership model (the launch blocker).**
Resolve H5 by designating one company-dormancy sweep (recommend SP4 `job_account_sweep` as the single
park+cooldown+notify path; demote `run_ownership_sweep` to warnings-only or retire). Reconcile M11
(one canonical ownership axis) and M10 (single-transaction park). Then H9 (make SITE_CAP count the
canonical axis). Unify the inactivity-days setting. Add tests that assert only one sweep clears
ownership and that a parked account always has `swept_at`+cooldown.

**Phase 2 — Close the conversion loop (workflow).**
H1 (Create-Requisition prefill via `create-form` + `company_id`), M4 (set `CONVERTED` on
opportunity creation), M3 (populate `historical_context` + apply historical bonus on reactivation),
M8 (use the shared scorers). This makes claimed→converted a real, scored, terminating path. One small
migration if `CONVERTED`/`EXPIRED` need to become first-class (they're already enum values except
`EXPIRED`).

**Phase 3 — Fix the `expired` black hole + scoring hygiene.**
H4 (`EXPIRED` enum member + filter pill + reachable resurface), M6/M5 (SQL-side stats + list ranking on
the persisted caches), L4 (`SIZE_BRACKETS`). Migration only for the enum value; the rest is code.

**Phase 4 — Efficiency + credit control.**
H6 (honor the discovery rotation slice) + M15 (write `credits_used`), M9/L1 (batch the similar-customer
and signal queries), M16 (unbounded exclusion sets), M18/M19 (discovery robustness).

**Phase 5 — UX + cleanup.**
M1 (surface collision warning), M2 (My-prospects scope), M7 (enrich row lock), M13 (manual-add SAVEPOINT),
M12 (guard `prospects/*`), M17 (dismiss service + reason + confirm), M14 + L5/L6 (delete/consolidate dead
modules), L2/L3/L7/L8.

---

## 4. What's solid — leave alone

- **Buyer-ready snapshot + persisted cache.** `prospect_priority.build_priority_snapshot` is a clean,
  pure, explainable scorer, and the `before_insert/before_update` write-through into
  `buyer_ready_score` (`app/models/prospect_account.py:124-141`) is the right pattern — it lets
  `buyer_ready_desc` rank/paginate in SQL. This is the model the other sorts/stats should copy (M5/M6),
  not something to change.
- **Claim atomicity.** `claim_prospect` correctly uses `with_for_update()` on both the prospect and the
  target company, handles the SF-migrated / new-discovery / domain-collision paths distinctly, and
  guards the reclaim cooldown (`prospect_claim.py:75-167`). The collision *handling* is right — only the
  *surfacing* of the warning is missing (M1).
- **`send_company_to_prospecting` SAVEPOINT dedup.** The nested-transaction "insert-or-adopt on unique
  domain" (`:278-295`) is exactly the right race-safe pattern — it's the template for fixing M13/H8.
- **Sweep notification fan-out.** `_sweep_notification_recipients` + per-recipient try/except
  (`prospect_reclamation.py:131-239`) is careful: deduped, rep-first, one bad address can't suppress the
  rest.
- **HTMX plumbing.** The explicit `hx-target` on the lazy stats container (`list.html:47-49`), the
  HTTP-286 stop-polling pattern for enrich status, the OOB stats refresh (`_action_oob.html`), and the
  honest `_prospect_error_toast` (200 + `HX-Reswap: none` + toast) all follow the project's documented
  conventions correctly.
- **LLM screening robustness.** `prospect_screening._call_screen_llm` uses `claude_structured` with a
  schema and defensive `int(... or 0)` coercion + fingerprint caching — no fragile hand-parsing.
- **JSONB usage is PG/SQLite-safe.** Enrichment/signals writes reassign the full dict + `flag_modified`;
  no raw JSONB operators or `func`-level JSON queries, so no SQLite-masked PG behavior in this module
  (the one DB-portability footgun is H8's unique-constraint abort, not a dialect issue).
