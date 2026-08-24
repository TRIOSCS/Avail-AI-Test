# AI-queue "Do all" — phased plan (2026-08-23)

Owner said "do all" of the remaining survey backlog. Seven features, three phases,
built in the same proven loop each: worktree → TDD → multi-agent adversarial review
→ every finding fixed with a regression test → full suite alone → real-Postgres +
real-Claude dry run → PR → CI green → squash-merge → deploy → live check → cleanup.
Owner merge/deploy word is NOT required (standing authorization this session), but
each ships as its own PR so anything can be paused.

Already shipped this session (context): #2 QP serial paste (#905), #6 PO-confirm
prefill (#907), #21 equivalence search (#908), #3 resell reply prefill (#909),
#18 Ask AVAIL (#910) — all live.

Migration numbers in flight reach 212. New migrations below claim 213+ in
MIGRATION_NUMBERS_IN_FLIGHT.txt before writing.

---

## Phase 1 — Finish the skipped approved item  (1 feature, S)

### 1. Vendor-name dup nudge  [idea #10 · S · NO migration · NO AI on hot path]
The offer form's `vendor_name` is a bare `<input>` (the single biggest source of
vendor-row fragmentation). Add an `hx-trigger="blur"` check → new GET
`/v2/partials/offers/check-vendor?name=…` → reuse `vendor_duplicates.check_vendor_duplicate`
(exact normalized + pg_trgm ≥0.3; already deterministic) → render "Did you mean
Arrow Electronics (existing vendor)?" into a sibling `#vendor-dup-nudge` div with a
one-tap adopt that swaps ONLY the input's name string (never re-links rows, never
blocks save). Verifier build-note honored: keep AI out of the blur path — pure
deterministic. Touches `_offer_form_fields.html`, `routers/htmx/offers/crud.py`.
Review risk: XSS on vendor names in the nudge; adopt must not mutate DB.

---

## Phase 2 — Data hygiene  (3 features)  "clean the data we now capture faster"

### 2. Offer free-text structurizer  [idea #12 · M · MIGRATION 213]
`Offer.lead_time / date_code / packaging` are `String(100)` prose ("2-3wk"). Add
sibling normalized columns (`lead_time_days` int, date-code bounds) via migration.
At offer save AND a nightly backfill sweep: deterministic normalizers first
(`utils/normalization`), only the leftovers go to one Haiku structured call
hard-guarded to return null rather than guess; AI-produced values get the amber
"AI — verify" chip on the offer row. Payoff: a buyer can filter/sort offers by
"ships inside 2 weeks" / "stock newer than 2023". Review risk: money/qty never
touched; AI must not overwrite a deterministic parse.

### 3. Manufacturer alias harvester  [idea #11 · M · maybe small MIGRATION 214]
`Manufacturer` model already has `canonical_name` + `aliases` JSON. Nightly batch
collects distinct manufacturer strings from sightings/offers/requirements that miss
`normalize_brand_name`, clusters them, and asks Claude (Batch API, overnight/cheap)
to map each variant → an existing canonical or "new/unknown". Proposals land in a
pending-approval admin list (spec_codes pattern — human approves before promotion);
approval appends the alias. Verifier: DROP the "backfill raw columns" step — aliases
only, never rewrite vendor-reported raw strings. Confirm-queue may need a small
pending table (→ migration) or reuse an existing pending pattern.

### 4. Contact / vendor dedupe sweep  [idea #15 · M · NO migration]
Extend the nightly `auto_dedup_service` with a contact pass: cluster `site_contacts`
across companies on normalized email (exact) + fuzzy name+domain, Claude confirms
only ambiguous name-only pairs. Contacts NEVER auto-merge — every pair lands in a
suggestion list (one-tap merge via existing `contact_merge_service`, or dismiss).
Closes the documented "cross-company contact dupes have zero detection" gap.
Review risk: never auto-merge; respect policy-allowed same-name-different-company.

---

## Phase 3 — Deal-context AI  (3 features)  "answer deal questions from what we hold"

### 5. QP draft-from-deal  [ideas #8 + #19 merged · M · NO migration]
ONE drafting service (verifier said 8 and 19 collide → merge). Primary source =
THIS deal's records (requirement condition/qty/FW-HW-rev, chosen offer terms) via
deterministic copy + extraction from an optional pasted customer TSO/PO; secondary
= carry-forward from the 2-3 most recent approved QPs for the same customer+commodity
(plain SQL similarity, no vectors). Drafts the ~28 free-text sales+purchasing QP
fields; every AI-sourced field renders amber "AI — verify"; nothing PATCHes until
the human accepts per-field (the QP grid already auto-PATCHes per field). Review
risk: accept-per-field write model; no silent writes; deterministic-copy vs AI-extract
clearly separated.

### 6. Buy-plan handoff brief  [idea #17 · M · NO migration]
Extend the existing `activity_digest_service` (get_or_build_digest: claude_structured
+ anti-stampede Redis lock + cooldown) to a BUY_PLAN entity: a one-tap AI summary
over SQL-computed deal facts (line/offer/quote status counts, QP gate states,
ApprovalAction→**ApprovalEvent** timeline, recent ActivityLog) → "what the customer
asked for, where each line stands, blockers, next actions" for handing a deal to a
backup. Cached HTMX panel. Review risk: reuse the digest anti-stampede pattern; the
requisition digest already exists — this is the buy-plan delta only.

### 7. Customer-360 sibling pooling  [idea #22 · M · NO migration · read-only]
Extend `account_summary_service`: before the Claude summary runs, gather sibling
`Company` rows sharing `normalized_name` (the policy-allowed different-owner
duplicates auto-dedup deliberately skips) and pool their part history/open reqs/
quotes/last-activity into the summary context. Panel gains an "includes N sibling
accounts (owned by X, Y)" banner; rows stay unmerged. Review risk: read-only, never
merges; the customer-identity-hiding rules still hold.

---

## Cross-cutting guardrails (every phase)
- AI drafts, human confirms — no silent DB writes; amber "AI — verify" chip on every
  AI-sourced value; one-tap human override outranks AI permanently.
- Ownership/read-gating (`_owned_req_ids` / RESTRICTED_ROLES) on anything req-scoped.
- Interactive AI calls: fast tier, `max_attempts=1`, per-user throttle, fall through
  on failure — never 500 a page.
- Nightly/batch AI: cheap tier, best-effort, own session, safe_background_task.
- Verify every model field/enum at build time; claim migration numbers before writing;
  run pytest INSIDE the worktree; throwaway-docker-PG (not sqlite) for dry runs.
- No new financial/margin logic; nothing integrates with Acctivate.

## Suggested order & rationale
Phase 1 first (closes an approved-but-open item, smallest, recon done). Phase 2 next
(data hygiene compounds the value of the entry-speed features just shipped). Phase 3
last (higher-touch, biggest reach, each self-contained). Ships as 7 independent PRs;
owner can redirect between any of them.
