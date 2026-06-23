# Resell / Excess Brokerage Module — Rough Draft Design

> **Status: ROUGH DRAFT for review.** Decisions marked _(draft)_ are my recommendation,
> not locked — flip any of them. Open forks are listed at the bottom.

## What it is (plain English)

The **inverse of the sales page.** On the sales side a customer posts what they *want* and
we find stock. Here a customer hands our **resell team** a list of **excess stock** they want
to offload; we post the part numbers so **other brokers** can see them; those brokers send us
**offers to buy** — sometimes on a single line, sometimes **"take the whole list"**; our trader
collects the offers and builds a **bid back to the customer** (our offer to buy their excess so
we can resell it).

The page we're building is a **workspace for the resell team to manage these buy/resell
opportunities** — post excess, watch offers stack up per part (or per whole-list), and assemble
the bid back. **Matching is on part number only** (no price-based ranking in the matcher), but each
offer carries a **unit price**, and per part we surface the **best unit price across all collected
offers** — that best-per-unit is what the trader uses to plan and price the bid back to the **stock
holder** (the customer who owns the excess).

What we get for free by reusing what already exists:
- **The old excess UI is the starting point.** The orphaned module already ships list / detail /
  line-table / offer-entry / CSV-import templates in the house style (status pills, compact tables,
  HTMX+Alpine). We **revive and reshape them in place**, not rebuild from scratch (see Reuse).
- **AI does the data entry.** A trader pastes / uploads / forwards a messy broker offer list; the
  existing AI parsers turn it into structured offer lines, normalize the MPNs, and auto-match them
  to the posted excess lines (see AI Automation).
- **No new matcher.** Every posted excess line is mirrored into the existing **`Sighting`** table
  (`source_type='customer_excess'`), so the proactive matcher/search already surfaces it against
  open demand — we emit the supply signal, we don't rebuild the matching.
- **No new export.** The bid-back reuses the **Quote** PDF path (which already hides vendor names).

## Context — this is NOT greenfield (the disconnect)

A grounding pass over the real code (`app/models/excess.py`, `app/services/excess_service.py`,
`app/routers/excess.py`, plus Requisition/Offer/Quote/Sighting/CRM/nav) found that **an "excess"
module already exists in the repo but is built on a different, wrong concept and is orphaned from
the UI:**

- **Orphaned but routable.** The router is registered (`app/main.py:547,572`) and tables ship in
  migration `29a41f5a248c`, but there is **no nav entry** (`mobile_nav.html` lists 8 tabs, none is
  excess). It's dead code with a live schema. **No production data behind it** (DB is intentionally
  fresh), so reshaping it is cheap.
- **Money is forced.** `Bid.unit_price` is `NOT NULL` + `>= 0` validator (`excess.py:153`). We make
  it **nullable** — matching is **part-number only** and a broker may bid "take-all, price TBD" —
  but price is still *collected and rolled up* to the **best unit price per part** for planning.
- **Wrong direction.** The wired path is **outbound** email-RFQ (`BidSolicitation` → MS Graph) —
  *we go ask people to bid.* The concept is **inbound** — *brokers see the post and send offers.*
- **Per-line only.** `Bid.excess_line_item_id` is a single non-null FK. **No take-all.**
- **No Sighting mirror.** Import currently writes buyer-side `Offer` rows, never `Sighting` rows.
- **Customer always visible.** No hiding layer.

**Terminology collision (must fix or it stays confusing forever).** The existing schema's `Bid`
means *an incoming offer from a buyer* — i.e. the concept's **"offer."** The concept's **"bid"**
means *the outgoing document we send the customer* — which has **no existing model.** We rename to
kill the collision (see Terminology).

## Core decision _(draft, recommended)_

**Reshape the scaffold; don't scrap, don't layer beside.** Keep `ExcessList` / `ExcessLineItem`
(they already mirror `Requisition` / `Requirement` correctly — header + lines + normalized MPN +
the same `parse_tabular_file()` importer). **Retire** `Bid` and `BidSolicitation` and build a clean
inbound **`ExcessOffer` / `ExcessOfferLine`** pair (your Q2 answer = option 5). Add the Sighting
live-mirror and a Quote-shaped bid-back. Routers stay thin; logic lives in `excess_service.py`
(matches the existing fat-service / thin-router split).

Layering a third parallel hierarchy beside Requisition/Requirement/Offer would triple the
not-DRY problem — rejected. Scrapping everything wastes the 60% that's already right.

**Reshape the UI in place too.** The orphaned excess templates/router/service already exist in the
house style — reshape them where they live (keep `app/templates/htmx/partials/excess/`,
`app/routers/excess.py`, `excess_service.py`; relabel the UI to **"Trading"**). Do **not** fork a
parallel `resell/` template tree — that re-creates the parallel-hierarchy problem (see Reuse).

## Roles & capabilities

Three user roles exist: **sales** (sell only), **buyers** (buy only), **traders** (both).
Traders are the **primary users** of this module. Model the two powers as **capabilities derived
from role**, not `if role == 'trader'` branching scattered in routers:

- **`can_post`** = sales + traders → can intake & post an excess list.
- **`can_offer`** = buyers + traders → can submit offers on a posting.

One guard that will fire weekly (traders are on both sides): **block self-offer** —
`ExcessOffer.submitted_by` must have `can_offer` **and** `≠ ExcessList.owner_id`. Clear message,
not a silent failure.

## Terminology (locked for the spec)

| Term in this spec | Means | Model |
|---|---|---|
| **Excess List** | the customer's parts-to-offload (header + lines) | `ExcessList` / `ExcessLineItem` (kept) |
| **Posting** | the customer-anonymized published state of an Excess List | `ExcessList.status` (no separate table _(draft)_) |
| **Offer** | an inbound offer from another broker to BUY — per-line or take-all | `ExcessOffer` / `ExcessOfferLine` (new) |
| **Customer Bid** | the outbound doc we assemble & send the customer (our offer to buy their excess) | reuse Quote path (new `CustomerBid` header _(draft)_) |

> Floor vocabulary calls inbound offers "bids" too — in this spec **inbound = Offer, outbound = Customer Bid.**

## Data model

### Kept (reshaped) — the posting
- **`ExcessList`** (≈ Requisition): `company_id` (the real customer — **owner-only**),
  `customer_site_id`, `owner_id`, `title`, `notes`, `status`
  (`draft → open → closed → awarded → expired`), `open_at`, `close_at`, `source_filename`.
- **`ExcessLineItem`** (≈ Requirement): `excess_list_id`, `part_number`, `normalized_part_number`,
  `manufacturer`, `description`, `quantity`, `condition`, `date_code`, `notes`, `status`
  (`available → bidding → awarded → withdrawn`).
  - **Add `material_card_id`** resolved on create (Requirements do this; ExcessLineItem currently
    doesn't) — the Sighting mirror needs it.
  - **Add rollup fields** `best_offer_unit_price` (min across collected offers), `best_offer_id`,
    `offer_count` — recomputed whenever an offer lands; this is the "best price per unit" the trader
    plans the bid-back against. `asking_price` stays as an optional seller-ask (no auto-margin).

### New — the inbound offers (the big thing)
- **`ExcessOffer`** (header): `excess_list_id`, `submitted_by` (FK User, `can_offer`),
  `offerer_company_id` / `offerer_vendor_card_id` (who the broker is), **`scope`** (`per_line` |
  `take_all`), `valid_until`, `notes`, `status` (`open → won → lost → expired → withdrawn`),
  `created_at`.
  - `scope='take_all'` → binds the **whole list**, no line rows expanded; carries an optional
    `take_all_total_price` (one lump for everything).
  - `scope='per_line'` → has `ExcessOfferLine` rows.
- **`ExcessOfferLine`** (only for `per_line`): `offer_id`, **`excess_line_item_id` (nullable)**,
  **`mpn_raw`**, `quantity`, **`unit_price` (nullable)**, `lead_time_days` (nullable), `terms_text`
  (free text — packaging / date-code / whatever the broker typed),
  `match_status` (`matched | unmatched | ambiguous`).
  - Nullable `excess_line_item_id` + `mpn_raw` = **unmatched queue:** a broker pasting a part that
    doesn't cleanly match a posted line is **held for manual resolution, never dropped** (a dropped
    offer is a lost deal).

### New — the outbound bid back
- **`CustomerBid`** _(draft)_: `excess_list_id`, `owner_id`, `status`
  (`draft → sent → accepted → rejected`), `revision`. Bid lines reference the selected
  `ExcessOfferLine`(s) (or the take-all offer). Rendered via a **cloned Quote PDF path** (see below).

### Retired
- **`Bid`** (money-required, per-line-only, terminology collision) → replaced by `ExcessOffer`.
- **`BidSolicitation`** (outbound email-RFQ, wrong direction, untyped `contact_id`) → dropped.

Migration: a reshaping Alembic revision (cheap — no data). Coordinate the number via
`MIGRATION_NUMBERS_IN_FLIGHT.txt`; revision id ≤ 32 chars.

## The management page — the Trading workspace (the core ask)

**Design goal: dead-simple by default, flexible across deal types, and an at-a-glance way to
aggregate and triage every excess opportunity.** It must read clean whether a deal is a 100-line
list, a single one-off part, or a take-all bundle — **one adaptive layout, not three screens**.
Built **entirely from live design-system components** (no new UI vocabulary): `splitPanel()`, the
opportunity-table-v2 cell macros, `stat_card`, `badge`/`status_badge`, `filter_pill`, the kebab
action-rail, `empty_state`, toasts — `.page-fluid` width, new `'Trading'` bottom-nav tab.

Three principles:
- **Simple by default, depth on demand** — a row shows only essentials; the rest hides behind hover,
  kebab, and lazy-loaded tabs (the CRM-IA contact-card pattern).
- **One adaptive surface** — the detail panel re-shapes to the deal; it never branches to new pages.
- **Reuse, don't invent** — every element maps to an existing macro/class, so it looks native day one.

```
┌ Trading ───────────────────────────────────────────────────────────────────────┐
│ [ My Lists ]  [ Open to Me ]              ← lens toggle (My Lists default)        │
├──────────────────────────────┬───────────────────────────────────────────────────┤
│ MY LISTS (posted by me)      │  Acme excess · 42 lines · OPEN · closes in 3d      │
│  • Acme excess   OPEN  12▮    │  ── customer: Acme Corp (you only) ───────────────│
│  • Globex CM     BIDDING 5▮   │  ┌ Lines │ Offers │ Build Bid ┐                    │
│  • Initech       CLOSED       │  │ MPN          qty  cond  offers                   │
│                              │  │ XCVU9P-2F    50   New   ▸ 3 offers               │
│ OPEN TO ME (post by others)  │  │ EP4CE10      120  New   ▸ 1 offer  · 1 take-all  │
│  • West-coast CM  ~40 lines  │  │ ...                                              │
│  • [anonymized] ~8 lines     │  └──────────────────────────────────────────────── │
└──────────────────────────────┴───────────────────────────────────────────────────┘
```

**Aggregate & triage (the "manage many opportunities" ask).** A `stat_card` strip above the list is
the team's glance — **Open** · **Offers to review** (new, unactioned) · **Take-all offers** · **Bids
out** · **Awarded $** — and each card is a one-click filter into the list. The left list uses the
opportunity-table-v2 row: `status_dot` + name + a **`coverage_meter`** repurposed as *offer coverage*
(lines with ≥1 offer ÷ total lines) + `time_text` urgency on the close date. Filter via `filter_pill`
(Open · Collecting · Bid out · Awarded) + "offers waiting" + closing-soon; default sort surfaces
**what needs attention** (new offers, closing soon, unmatched-queue items).

**Lens switch** — *My Lists* (posted by me: collect offers, build bids) vs *Open to Me* (others'
postings I can offer on, **customer-anonymized**). Same pill pattern as the Buy-Plans Hub (`hx-get` +
`hx-push-url`, Alpine `:class` for instant feedback). Pure-sales / pure-buyer become filtered subsets
later.

**Flexible detail — one panel, three deal shapes.** Slim header (breadcrumb `Trading › {name}` +
metadata chips: customer [owner-only] · status · #lines · #offers · closes-in · owner), then
lazy-loaded tabs **Lines · Offers · Build Bid · Activity**. The Lines/Offers rendering *adapts to the
deal* by a mechanical rule — **density scales to line count, placement follows offer scope:**
- **Multi-line list** (common) → compact table (`compact-cell`): each line shows qty / condition /
  offer-count (`▸N`) / best-$; the Offers tab stacks offers under each line.
- **Single-line one-off** → no table chrome; collapses to **one `.card`** — the part + its offer
  stack inline. A 1-line deal shouldn't look like a spreadsheet.
- **Take-all bundle** → the whole-list offer(s) render as a **pinned banner card** above the lines
  (the take-all is the headline; lines are its contents).

**Easy entry everywhere** — one **Paste / Upload / + Add line** affordance on both the Lines tab and
offer entry, feeding the AI funnel → preview/confirm grid. Inline-edit qty/condition on a line
(blur-to-save, the rq2 pattern). Honest `empty_state` per stage; every mutation confirms with a toast.

- **HTMX trap:** any `hx-trigger="load"`/`intersect` lazy tab MUST carry an explicit `hx-target` or
  it inherits `#main-content`'s `hx-target="this"` and wipes the page (documented Buy-Plans bug).

## Reuse — revive the old excess UI (in place)

The orphaned excess module already ships the screens in the house style. **Reshape them in place**
(keep the `excess/` template folder, router, and service; relabel UI → "Trading"). Do **not** fork a
parallel `resell/` tree.

| Existing template | Action |
|---|---|
| `list.html` | Keep — list view, search/status filters, stats cards. Relabel → "Trading". |
| `detail.html` | Reshape — keep header + status buttons + file-import; **drop the "Solicit Bids" button**; pivot line columns to best-offer / offer-count. |
| `line_item_row.html` | Reshape cells — keep part/mfr/qty/condition/date-code; replace asking-price with **best-offer + offer-count link**. |
| `bid_form.html` (→ offer entry) | Reshape — company / unit_price / qty / lead / notes form is right; make price optional; add `scope` (per-line / take-all). |
| `bid_list.html` (→ offer stack) | Reshape into the **comparison table** (best highlighted, price-spread bar from quote-builder). |
| `create_modal.html`, `add_line_item_modal.html`, `import_preview.html`, `row.html`, `_macros.html` | Keep ≈as-is (label/field tweaks). |
| `solicit_modal.html` + `send_bid_solicitation()` | **Drop** — outbound email RFQ, wrong direction. |

Reuse the visual conventions verbatim: status-pill palette (draft=gray, open=emerald, bidding=amber,
closed/awarded=blue), compact tables, `tabular-nums` for price/qty, kebab row actions. Reshape traps:
rewire **every** `hx-*` endpoint and Jinja `include`/`from` path (silent 404s otherwise); keep
`x-data='…|tojson'` **single-quoted** (double-quoted + tojson breaks on apostrophes — a known class
in this app); align status-enum values to the template `pill_colors` keys or the buttons vanish.

## Design consistency — fits the rest of the app

The module must look like it was always part of AvailAI, not bolted on. The rule is **inherit, never
re-style:**

- **Tokens, verbatim.** Font stack Aptos → Segoe UI → system; the single border color `#BFC4CE`
  (`border-brand-200`); radius/spacing from `.card`, `.btn`, `.table-cell`. Add **no new colors** —
  every accent comes from the canonical `SEMANTIC` map (`success`=emerald, `warning`=amber,
  `danger`=rose, `info`=sky, `brand`, `violet`, `neutral`, `muted`).
- **Status pills inherit the app's colors for free** by choosing status *values that already exist*
  in the `status_badge` map — no per-module palette, no new color logic:

  | Resell status | Reuse existing key → color |
  |---|---|
  | list `draft` | `draft` → muted/gray |
  | list `open` (posted, awaiting offers) | `open` → sky |
  | list `collecting` | `sourcing` → amber |
  | list `bid_out` | `quoted` → violet |
  | list `awarded` | `won` → emerald |
  | list `closed` / `expired` | `completed` / `expired` → muted |
  | offer `open` | `pending` → amber/neutral |
  | offer `won` / `lost` | `won` / `lost` → emerald / rose |

- **Components, not bespoke markup.** Render through the shared macros only: `badge`/`status_badge`,
  `btn_primary`/`secondary`/`danger`, `stat_card`, `filter_pill`, `coverage_meter`, `time_text`,
  `opp_name_cell`/`opp_status_cell`/`opp_row_action_rail`, `empty_state`, `activity_row`, the global
  modal (`#modal-content` + `.modal-header/body/footer`), and `showToast`. If a needed element has no
  macro, add it to `shared/_macros.html` (so the whole app gets it) — never inline a one-off style.
- **Shell & nav.** `.page-fluid` for the workspace (dense), `.page-readable` for any standalone form;
  `splitPanel()` for the two-panel layout; register the tab in the **same** `mobile_nav.html` list as
  the other 8 — same icon weight, same label treatment. Breadcrumb + slim header match the CRM-IA
  detail header exactly.
- **Interaction grammar.** HTMX swap into `#main-content`, `hx-swap="morph"` to preserve Alpine
  state, lazy tabs via `intersect once` (with explicit `hx-target`), inline blur-to-save, kebab
  `@click.outside`, `data-loading-disable` on submits — the conventions every other workspace uses.

**Consistency guardrails (build-time):** any genuinely new Tailwind class must land in the canonical
CSS layer + Tailwind safelist and be verified in the built bundle after deploy (new classes silently
drop otherwise); keep `x-data='…|tojson'` single-quoted; before merge, render the page beside
Sightings / Buy-Plans / CRM and confirm pills, cards, spacing, and density read identically.

## Intake — three methods, one parser

Posting an excess list reuses the Requisition intake wholesale:
- **Upload** (CSV/XLSX) → `parse_tabular_file()`.
- **Paste** → the AI intake parser `parse_freeform_intake(text, mode="offer")` (bidirectional;
  `mode="offer"` flips rows into excess-line shape).
- **Single-line** → a one-row paste with the line pre-selected.

All three funnel to the same intermediate rows → `normalize_mpn_key()` → resolve/create
`MaterialCard` → `ExcessLineItem`. Build the funnel once.

## Offer collection — per-line, take-all, best-price rollup (the centerpiece)

Brokers send messy lists, so the offer side gets the **same easy-entry funnel** as intake — paste,
upload, single-line, or forwarded email — all AI-parsed (see AI Automation) → `normalize_mpn_key()`
→ **match against the posting's lines on part number** → each row returns
`matched | ambiguous | unmatched`.

- **Per-line offer** → `ExcessOffer(scope='per_line')` + `ExcessOfferLine` rows. Matched lines
  attach to an `excess_line_item_id` (with this broker's `unit_price` / qty / lead time); unmatched
  or ambiguous rows keep `mpn_raw` and **queue for the owner to resolve** — never dropped.
- **Take-all offer** → `ExcessOffer(scope='take_all')`, no line rows — binds the whole list,
  all-or-nothing, optional lump `take_all_total_price`, pinned as a list-level banner.
- **Best-price rollup per line.** As offers land, recompute each line's `best_offer_unit_price` (min
  unit price across its offers), `best_offer_id`, `offer_count` — mirror the existing
  `sighting_aggregation.rebuild_vendor_summaries()` rollup, but keyed on `excess_line_item_id` and
  the **`ExcessOffer`** table (not Sightings — see trap). This is "compile many offers, take the best
  per unit."
- **Offer-stack view** (owner): every per-line offer stacked under its line in a comparison table
  cloned from the **quote-builder modal** (`quote_builder/modal.html` — vendor / unit$ / qty / lead
  columns + a price-spread bar with the best highlighted in emerald). Take-all offers pinned on top.
  **Do NOT auto-select** even when one offer is cheapest — with many broker bids the trader eyeballs
  terms / lead / reputation first (the quote-builder's single-offer auto-select is wrong here).

## AI automation & easy data entry

Data entry is the whole UX battle — traders won't hand-type 40-line broker lists. Every entry path
(intake **and** offers) funnels through the existing, mature AI services; the trader pastes / drops /
forwards, then reviews:

- **Paste** (freeform) → `ai_intake_parser.parse_freeform_intake(text)` → structured `offers[]` /
  `requirements[]` + confidence + summary.
- **Upload** (CSV/XLSX, messy headers) → `attachment_parser.parse_attachment(bytes, filename)` →
  deterministic column-mapping first, Claude fallback for odd layouts, cached per vendor+file.
- **Forwarded email** (broker quote reply) → `ai_email_parser.parse_email(body, subject, vendor)` →
  `quotes[]` with per-line confidence; auto-apply ≥0.8, flag 0.5–0.8 for review.
- **Normalize + match** → `ai_part_normalizer.normalize_parts([mpn,…])` (manufacturer + package
  inference; keep the normalized value only at confidence ≥0.7) → `normalize_mpn_key()` → match to
  posted lines; misses → the unmatched queue.
- **Single-line** quick-add for the one-off, normalize-on-blur.

Everything lands in a **preview/confirm grid** (reuse `import_preview.html`) with inline edit before
commit — high-confidence rows pre-checked, low-confidence flagged. AI calls are gated by the existing
`_ai_enabled(user)` check and routed through `llm_router` (tier-routed Haiku/Sonnet/Opus). Traps:
`parse_email` clips at 5000 chars; `attachment_parser` assumes headers on row 0; the normalizer's
in-memory cache isn't shared across workers; **drop** the excess module's one-off
`_call_claude_bid_parse()` in favor of `parse_email()`.

## Customer Bid (the bid back) — reuse the Quote path

Strongest reuse in the build. The Quote PDF path
(`quote_builder_service.generate_quote_report_pdf()` → `quote_report.html` → WeasyPrint)
**already omits the vendor column** (the template never receives `vendor_name`), so a clean,
trader-name-free customer doc is almost free. Build a **`bid_report.html` cloned from
`quote_report.html`**.

Two traps:
1. The **Excel** export (`build_excel_export()`) **does include a Vendor column** — explicitly
   suppress it for the bid-back.
2. PDF cleanliness is currently *accidental* (field just never passed). For the bid-back, **enforce**
   it at assembly — strip trader names/sources when building bid lines, don't rely on template
   omission.

The bid-back **is priced** — for each line the builder pre-fills a planning reference from
`best_offer_unit_price`, and the trader enters **our offer to the stock holder** (no automatic
margin — the trader sets the number; the best-per-unit just informs it). The customer doc shows
part / qty / condition / our offer price — **never** trader names or what each broker bid.

## Sighting live-mirror (free matcher integration)

On post (and on every line change), each `ExcessLineItem` writes/updates a **`Sighting`** row so
the existing matcher sees it. **Live mirror, not fire-and-forget** — one service method owns the
dual-write so the two never drift (qty drop / award / expire updates or retires the Sighting).
Reuse `sighting_ingest.sighting_from_row()` (the single dict→ORM source of truth).

Exact fields the mirror must set (from `app/models/sourcing.py:188-284`):
- `normalized_mpn` via `normalize_mpn_key()` (**not** `normalize_mpn()` — they differ; must match
  `MaterialCard.normalized_mpn` or aggregation won't link), `mpn_matched`, `material_card_id`.
- `source_type='customer_excess'` (the sentinel).
- `source_company_id = ExcessList.company_id` (FK exists, currently never populated — **this is the
  customer-hiding hook**).
- `vendor_name` — **trap:** for an external sighting this is the supplier; for excess it's the
  *seller*. Don't feed the customer name into `VendorCard` dedup — synthesize an internal label or
  skip vendor-card linkage for `customer_excess` _(draft: synthesize "Customer Excess" label)_.
- `requirement_id` — **`NOT NULL` landmine** (an excess line isn't tied to one requirement). See Open #1.

Dedup trap: `_save_sightings()` deletes by `(requirement_id, source_type)`; a re-publish would wipe
prior `customer_excess` rows. Use an explicit upsert key (`source_company_id` + `material_card_id`).

## Customer identity hiding

Single-tenant today (all internal users), so hiding is **view discipline, not an auth boundary.**
The offerer-facing render of a posting (the "Open to Me" panel + any mirrored sighting a non-owner
sees) must project **only MPN / qty / condition** and **never** `ExcessList.company_id`,
`company.name`, or `CustomerSite`. Implement as a **separate offerer-facing serializer/partial**
(pure whitelist), plus filter `customer_excess` sightings so non-owning users don't see the seller.
The owner always sees the real customer. _(draft: hard hide — keeps it future-portal-safe.)_

## Out of scope (later, separate specs)
- External counterparty **portal + auth** (brokers logging in directly).
- The **match engine** itself — we only emit the supply signal into `Sighting`; consuming it against
  demand/watchlists is the existing/forthcoming matcher's job.
- **Acctivate write-back** on award.
- Partial-publish (posting a subset of a list) — fold into `ExcessList.status` for now.

## Resolved for v1 (recommended defaults — flag before build to change)

These were the open forks; each is locked to the recommended option so the spec has no TBDs.

1. **Mirrored-sighting `requirement_id` home** → **a virtual "unallocated excess" requirement per
   posted list.** The mirror attaches its sightings to one system-owned requirement per list, so the
   existing matcher (which assumes `requirement_id NOT NULL`) is untouched. Making the column nullable
   is deferred (touches too many call sites).
2. **Lock-on-post vs editable** → **lock on post; revising a posted list creates a new version.** Once
   `status=open`, lines are frozen; edits open a revision. Avoids offers attached to shifting lines.
3. **Late offers** (after `closed`/`bid_out`) → **accept but flag `late` and queue for review** (never
   dropped). Auto-folding a late offer into a *revised* bid-back is the v2 late-offer workflow.
4. **Customer hint on the anonymized posting** → **strict MPN / qty / condition only** (no region or
   teaser). Pure-whitelist serializer; future-portal-safe.

## Phased build _(draft)_
- **v1** — reshape schema in place (retire Bid/BidSolicitation; add ExcessOffer/Line + `scope` +
  nullable `unit_price` + best-price rollup fields); revive the excess templates as the **Trading**
  workspace (two panels); **AI-assisted entry on both sides** (paste/upload/email → parse → normalize
  → match → preview/confirm); offer collection per-line + take-all with unmatched queue;
  **best-unit-price rollup + comparison table**; Sighting live-mirror; customer hiding; bid-back
  assembly seeded from best-per-unit + clean PDF.
- **v2** — offer expiry automation, ambiguous-match assist, late-offer handling, Teams alert on new
  postings (ties into the cross-app alert system), bid-back revisions.
- **v3** — external counterparty portal + auth, award workflow, Acctivate handoff.

## Load-bearing files
- `app/models/excess.py` — reshape target (`Bid.unit_price:153`, no `scope`).
- `app/models/sourcing.py:188-284` — `Sighting` (`source_company_id:219` unused, `requirement_id:191` NOT NULL).
- `app/services/excess_service.py` — import + match; needs MaterialCard resolve + Sighting mirror; drop `_call_claude_bid_parse()`.
- `app/services/sighting_ingest.py` — `sighting_from_row()` (reuse for mirror).
- `app/services/sighting_aggregation.py` — `rebuild_vendor_summaries()` best-price rollup pattern to mirror.
- `app/services/ai_intake_parser.py` (`parse_freeform_intake`), `app/services/attachment_parser.py` (`parse_attachment`), `app/services/ai_email_parser.py` (`parse_email`), `app/services/ai_part_normalizer.py` (`normalize_parts`) — AI data-entry.
- `app/services/quote_builder_service.py` + `app/templates/documents/quote_report.html` — bid-back reuse (clean PDF).
- `app/templates/htmx/partials/excess/*.html` — the old UI to revive in place (list / detail / line_item_row / bid_form / bid_list / import_preview / _macros).
- `app/templates/htmx/partials/quote_builder/modal.html` — offer comparison table + price-spread bar to clone.
- `app/templates/htmx/partials/shared/mobile_nav.html:24` — nav registration.
- `app/templates/htmx/partials/sightings/list.html` — split-panel pattern to clone.
