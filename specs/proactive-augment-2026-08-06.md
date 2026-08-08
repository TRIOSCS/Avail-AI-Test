# Proactive Feature Augmentation — Spec (2026-08-06)

Source: user's 9-step brief of 2026-08-06 (manual Salesforce run of the same
match), reconciled against the audited current state. This is an augmentation
of the existing feature — same tables, same UI shell, same send/convert
pipeline. No parallel feature.

## Concept (user's words, binding)

Look at parts customers asked for in the past — requirements that moved to won
or lost — and parts uploaded as hotlists (parts they use but don't need right
now). When those parts become available, offer them — without noise: no
repeating the same part number, one actionable line per part per customer.

## Current state (audited 2026-08-06, verbatim evidence in session log)

- Engine `app/services/proactive_matching.py` seeds from CustomerPartHistory
  (purchase history) + HOTLIST requisitions, joined on `material_card_id`.
  Requirements carry **no time window and no status filter**; there is no
  requirement-driven seeding at all. CPH is only written on buy-plan
  completion → prod `proactive_matches` has never had a row.
- One match row = one (part, company) anchored to ONE `offer_id`. The
  36-offers-per-part noise problem is structural.
- `/api/proactive/scorecard` and `/api/dashboard/proactive-picks` do not exist
  (legacy router deleted, PR #751). Real surface: HTMX
  `/v2/partials/proactive/*`. No LLM ranking, no cache; deterministic score
  recency 40% + frequency 30% + margin 30%. Only LLM use is Haiku drafting
  the outreach email.
- Suppression that already works and stays: 21-day throttle per (mpn, site),
  per-company do-not-offer list, one active NEW/SENT match per (part, company),
  30-day NEW-match expiry, 10% min-margin gate (skipped when margin unknown).
- Price history sources exist: `quote_lines` (per-MPN sell/cost, indexed) ⋈
  `quotes` (`sent_at`, `customer_site_id`, `created_by_id`, `result` won/lost,
  `result_at`, `won_revenue`). W/L came from migration 158 (June requisition
  pipeline), NOT the AS9120B QP fields.
- Prod DB has zero requirement/offer data for the validation MPNs — the SFDC
  import never happened. User decision 2026-08-06: **one-time seed now** from
  the OneDrive export; recurring import explicitly out of scope.

## Design decisions (all resolved)

**D1 — Seeding rework (Steps 3).** Demand side = every `Requirement` whose
`created_at >= now − PROACTIVE_REQUIREMENT_WINDOW_MONTHS` (24), **any**
requisition status (open, quoted, won, lost, cancelled — an ask is an ask),
PLUS hotlist requisitions with no age limit (a hotlist is a standing monitor).
Supply side = offers with `created_at >= now − PROACTIVE_OFFER_WINDOW_DAYS`
(7) and status in {active, approved}. Both windows are new `Settings` fields
in `app/config.py` beside the existing `proactive_*` knobs, mirrored as named
constants where the queries live. The CPH seeding path is removed; hotlist
path stays. `run_proactive_scan`'s incremental watermark stays (it bounds
which offers trigger a rescan; the 7-day window bounds what aggregates).

**D2 — Join key.** `material_card_id` equality when both sides have a card;
otherwise `normalized_mpn` equality (`normalize_mpn`, already applied on both
tables). Price never affects matching; the margin gate only suppresses
already-matched lines as today.

**D3 — Part-level rollup (Step 2).** Match granularity stays one row per
(part, company). Aggregates are computed at read time in the service layer —
`available_qty = SUM(qty_available)`, `low_cost = MIN(unit_price)` over live
offers for the part inside the offer window — and rendered on the match line;
individual offers listed on drill-down (match row expand). Nothing stored
denormalized on the match; digest lines freeze a snapshot at generation time
(D7). A new offer on a part with an active match updates the rollup, never
creates a second line. Zero-price offers ($0.00 = price not provided, e.g.
two placeholder rows on GSOT36C-E3-08 in the 08-06 export) count toward
`available_qty` and appear on drill-down but are excluded from `low_cost` —
the user's hand-computed $0.05 is the minimum *positive* price; decision
surfaced to the user 2026-08-06.

**D4 — Repeat demand (Step 4).** `requirement_count` = COUNT of requirement
rows for (company, part) inside the window; `last_asked_at` / `last_asked_qty`
from the newest one. Stored on the match at scan time (cheap, rescanned every
cycle), shown as "(N requests on file)" only when N > 1, feeds ranking (D9).

**D5 — Price history (Step 5).** Two lookups, both bounded by a single
`PROACTIVE_PRICE_LOOKBACK_MONTHS = 24`:
- *Last quote*: newest `quote_lines` row for the MPN joined to `quotes` with
  status in {sent, won, lost}, ordered `sent_at desc nullslast, created_at
  desc`; carries price, date, customer (site→company name), rep
  (`created_by_id`→user name).
- *Last win*: same join filtered `result = 'won'`, ordered `result_at desc`;
  shown even when same customer/rep — best available anchor.
Renders exactly per the brief (same-customer vs other-customer phrasing; win
line always "won ${price} on {date}, {rep} at {customer}"). Line omitted
entirely when no record — no N/A, no placeholder. Implemented as a new
function in `app/services/pricing_history.py` querying `quote_lines` (the
existing JSON-based helper stays for its current consumers). Digest carries
"Internal reference — not for forwarding." because these lines can contain
another customer's pricing.

**D6 — Salesperson routing.** Primary: `company.account_owner_id` (as today).
If the company has no owner: the `created_by` of the newest in-window
requirement's requisition. If the requirement has no customer account at all:
the line goes to that requisition's `created_by` under a "Trio Back Order"
group in *their* digest. Ownerless companies are no longer silently skipped.

**D7 — Digest (Step 6).** New models:
- `ProactiveDigest`: salesperson_id, status draft|sent|discarded, subject,
  body_html, generated_at, sent_at, sent_by_id.
- `ProactiveOutreachLine` (also serves Step 8): digest_id, match_id (SET
  NULL), mpn, company_id, salesperson_id, frozen snapshot (last_asked_at,
  last_asked_qty, requirement_count, available_qty, low_cost, quote/win
  anchor fields), contacted bool default false, outcome
  (still_looking | looking_again | needs_more | no_longer_needed |
  no_response, nullable until known), produced_requisition_id,
  produced_quote_id, `sales_order_number` (ERP-neutral reference string).
Generation: weekly APScheduler cron (Monday 07:00 container-local, beside the
existing email_jobs crons) + on-demand "Generate digests" button in the
workspace. Generation only creates drafts. Review pane: new tab on the
proactive page listing drafts per salesperson with rendered preview;
manager (or admin) clicks Send → Graph `/me/sendMail` with the **actor's**
delegated token to the salesperson's email. Nothing sends automatically.
Copy, grouping, and formatting exactly per the brief: subject "Availability
match on your requirements, please check with customers"; greeting = first
whitespace-token of `User.name`; customer name as plain line over a bulleted
list; thousands separators; M/D/YYYY; prices bare when whole, two decimals
otherwise. Regenerating while an unsent draft exists replaces it (draft is
disposable; sent digests are permanent).

**D8 — Duplicate material names (Step 7).** Collision scan over MPNs seen in
the window (requirements ∪ offers): group by `lower(strip(spaces, hyphens))`;
groups with >1 distinct raw spelling are listed in a "Suspected duplicate
materials" section at the bottom of each affected digest AND a workspace
panel. Detection only — no silent merge, ever; fixing is a human action in
the system of record.

**D9 — Ranking rework (Step 9).** `compute_match_score` becomes (0–100):
- ask recency 25%: last ask ≤30d→100, ≤90d→80, ≤365d→60, older→**40 floor**
  (an old ask with fresh supply is interesting, not stale — floor, not decay).
- repeat demand 20%: 1 ask→40, 2→60, 3–4→80, ≥5→100.
- price spread 25%: (last-quote − low_cost)/last-quote: ≥30%→100, ≥20%→80,
  ≥10%→60, >0→40, ≤0→10, no quote on record→50 (neutral).
- recent win 15%: win ≤24mo same customer→100, any customer→80, none→40.
- quantity fit 15%: available ≥ last asked→100, ≥50%→70, <50%→40.
Hotlist matches keep a floor of 60. "Top picks" = highest-scored strip atop
the proactive list, Redis-cached 24h per user, busted by manual refresh.
Deterministic — no LLM (none existed; none added).

**D10 — Scorecard/tracking (Step 8).** Existing scorecard stays. New weekly
outreach summary (service + partial next to the scorecard): lines sent, lines
contacted, contact rate by salesperson, requirements reopened
(produced_requisition_id), quotes issued, orders won (sales_order_number
present). Contacted/outcome are set inline on the digest's sent lines by
sales or manager; every change audit-logged like the rest of the workspace.

**D11 — Seed importer (one-time, user-approved).** Management command
`app/management/import_proactive_export.py` reading the export xlsx from a
local path: upserts companies + customer sites (customer identity from the
requirements sheet), maps rep names → `users.name` case-insensitively
(unmatched reps → importing admin as owner, every fallback logged), creates
one requisition per (customer, SF requirement group) with requirements dated
by their original ask dates, and offers with vendor name, qty, unit price,
and export-file date as `created_at`. Idempotent via natural keys
(customer+mpn+ask-date for requirements; vendor+mpn+qty+price+file-date for
offers). Runs against prod only after the diff is approved and deployed.
Column mapping: appendix A.

**Known seed limitation (parsed 2026-08-06):** the export is a Salesforce
joined report that materializes requirement rows ONLY for materials that also
had an availability in the last 7 days — 42 requirement rows in the grid,
while the report footer counts **6,340 requirement items / 129.85M pcs** in
the full 2-year set. Availabilities are complete (170 rows, footer-exact).
This file fully supports validation and the current week's digest, but AVAIL
cannot catch "old ask, suddenly supplied" in future cycles until the full
2-year requirements-only export is also seeded — that export must come from
Salesforce (user/mbilal action, requested 2026-08-06). The importer accepts
both shapes: the joined report and a requirements-only export.

**D12 — Boundaries.** No Acctivate touchpoint; `sales_order_number` stays a
reference string. No recurring Salesforce sync. No auto-send. No silent MPN
normalization in stored data. Stale docstrings found by the audit
(ProactiveMatch model, proactive_service header) corrected in the engine
commit. APP_MAP docs updated with the new flow.

## Schema & migration

Migration **205** (claim line added to MIGRATION_NUMBERS_IN_FLIGHT.txt in the
same commit, chains onto 204, round-tripped on throwaway PG):
- `proactive_digests`, `proactive_outreach_lines` (columns per D7).
- `proactive_matches` += `requirement_count` int default 0, `last_asked_at`,
  `last_asked_qty` Numeric(12,2), `match_source` varchar(20) default
  'requirement' (requirement | hotlist).
All additive; downgrade drops in reverse. Indexes declared in
`__table_args__` so the fresh-DB drift gate stays green.

## Validation (binding, from the brief)

Fixture-seeded test DB must reproduce on the main part line, exactly:
- GSOT36C-E3-08 | last asked 7/30/2026 for 500,000 | available 1,805,521,
  low $0.05 (Beckhoff Automation)
- LTSR15-NP | last asked 8/3/2026 for 3,000 (2 requests) | available 4,850,
  low $6.44 (Beckhoff Automation)
- BSM300GA120DN2HOSA1 | last asked 7/28/2026 for 950 | available 96,
  low $274 (RES Renewable Energy Systems, Spain)
Disagreement = stop and report (data difference vs matching bug); the rule is
never adjusted to fit. After Step 5 lands: 5 sample lines carrying a quote, a
win, or both go to the user for checking against Salesforce before anything
reaches a salesperson.

## Build order

1. Seed importer + fixture (Task 2) — everything downstream needs data.
2. Engine rework: windows, requirement seeding, rollup, repeat demand
   (Task 3) → validation test green (Task 8).
3. Price history service + row/drill-down UI (Task 4) → 5 samples to user.
4. Digest generate/review/send + duplicate-names section (Task 5).
5. Tracking + weekly summary (Task 6).
6. Ranking + top picks (Task 7).
Each wave: tests in the same commit, `pre-commit run --all-files` before
push, diff shown to the user before merge; deploy and prod seed only on the
user's ship word.

## Appendix A — export column mapping (from the parsed 2026-08-06 file)

Workbook: one grid (SF joined report), two column blocks keyed by
`Material Name`, grouped per material. Extract validated against the report
footer: availability qty 3,190,729 exact; price sum within 2¢ (report
rounding). 60 distinct materials, 170 availability rows, 42 requirement rows.

Requirements block → AVAIL:
- `Material Name` → `Requirement.primary_mpn` (stored verbatim;
  `normalized_mpn` computed as everywhere else — no silent merging)
- `Req Item Number` (ReqItem#-…) → importer natural key (idempotency)
- `Requisition: Customer: Account Name` → `Company.name` + a default
  `CustomerSite` (created if absent)
- `Requisition: Owner: Full Name` → case-insensitive match on `users.name` →
  requisition `created_by`; also fills `company.account_owner_id` when unset.
  Unmatched rep names → importing admin, every fallback logged.
- `QTY` → `Requirement.target_qty`
- `Target Price per Unit` → requirement target-price field if the model has
  one (checked at build time; no new column otherwise)
- `Sourcing Status` → `sourcing_status`: Requirement→open, Quoted→quoted,
  Won→won, Archived→archived, blank→open
- `Created Date` → `created_at` (original ask date preserved)

Availabilities block → AVAIL (`Offer` rows, status active):
- `Material Name` → `Offer.mpn`; `Sourcing Item Number` (SRC#-…) → natural key
- `Vendor: Account Name` → vendor name field; `Qty` → `qty_available`;
  `Outright Price` → `unit_price`; `Created Date` → `created_at`
- `Owner: Full Name` (avail side) is the BUYING owner, not the sales rep —
  not used for salesperson routing.

Not present in the export: condition, MOQ, date code, SO/PO references, and
any quote/win price history — Step 5 anchors come from AVAIL's own
`quotes`/`quote_lines` only, and will be sparse until quotes are worked in
AVAIL (user informed 2026-08-06).

Data-quality findings the digest's duplicate-materials section must catch
(all confirmed in this file): `LTSR15-NP` vs `LTSR 15-NP` split (the variant
adds 1,000 pcs + one 3/30/2026 ask, incl. an apparent double-entered offer —
same vendor/qty/price/date, different SRC#), and near-twin MPNs
`GSOT36C-E3-08` / `PSOT36C` (the latter with a 500K-pc offer at $0.01 —
possible typo'd part). Detection lists these for a human; nothing merges.

## Addendum 2026-08-08 — AI part-equivalence (user-requested)

User: catch false negatives from formatting differences and benign ordering
suffixes (packaging codes) without false positives; AI guesses color-coded so
the user double-checks. Design shipped:

- **Formatting tier (deterministic):** matching joins on normalize_mpn_key,
  so spacing/hyphen/case variants pool automatically, labeled "variants
  pooled" (gray). This supersedes the 08-06 verbatim-identity rule; the
  hand-checked LTSR15-NP line becomes 5,850 pooled (4,850 + 1,000 under
  'LTSR 15-NP') with the split still flagged for cleanup at source.
  normalized_mpn is now derived at the model layer (Offer.mpn /
  Requirement.primary_mpn validators) so no creation path can miss it.
- **AI tier (part_equivalences, migration 206):** candidate key pairs
  (suffix shape ≤6 extra chars, or same-length one-char near-miss) are
  classified ONCE by Claude (Haiku, conservative prompt: packaging/reel/RoHS
  = same; voltage/grade/family = different; else uncertain) and stored.
  Only verdict=same pools — amber "AI match — verify" chip on the row, amber
  rows + reasoning in the offers drill-down, textual "AI variant match,
  VERIFY" note on digest lines. different/uncertain/absent never pool
  (PSOT36C can never contaminate GSOT36C without a human saying so).
- **Human override:** one tap ("Not the same part") writes a source=human
  verdict that permanently outranks the AI; suppression (throttle,
  do-not-offer, active-match dedup) applies across the whole class so a
  variant spelling can't sidestep it.
- Classification runs before each scan (and lightly on manual refresh),
  capped per pass; matching itself never calls the model — deterministic,
  auditable, and free at read time.
