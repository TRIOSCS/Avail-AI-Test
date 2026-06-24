# Build-Quote In-Workspace Tab — Design (reshape of the Quote Builder)

> Bring the resell **Build Bid** UX to the sales **Quote Builder**: an in-workspace "Build Quote" tab
> on the requisition/opportunity, so a salesperson builds a customer quote in context — best-cost
> pre-fill, single-stage inline assembly, clean PDF. Build Bid was *cloned from* the quote builder, so
> this is a **reshape, not a rebuild**. North star (from the resell work): simple / clean / flexible.

## Context

The sales Quote Builder today is a **full-screen modal** launched from a multi-select toolbar action
on the requisitions list. It loads offers + smart-defaults one, but the **sell-price field is empty**
(manual), the modal **blocks CRM/prior-quote context**, and the entry is buried. Build Bid (resell)
took the same `quote_builder_service` spine and refined it into a cleaner in-workspace tab. The user
reacted to that slickness and wants it for quotes.

**Why Build Bid feels slicker (the recoverable delta):**
| | Build Bid (loved) | Quote Builder (today) |
|---|---|---|
| Container | in-workspace **tab**, stays in context | full-screen **modal**, blocks context |
| Entry | tab on the record | buried multi-select → toolbar |
| Pricing | best price **pre-fills** the editable field | sell-price **empty** (manual) |
| Assembly | single-stage inline (check → price → submit) | per-line decision tree (select → price → confirm-lock) |
| Export | guaranteed-clean **whitelist** | relies on the template omitting fields |

## Design — "Build Quote" tab

**Where:** a **Build Quote tab** on the requisition/opportunity detail (requisitions2), a sibling to
the existing **Quotes** *list* tab (which stays — it lists saved quotes). Lazy `hx-get` body with an
explicit `hx-target`, gated to the requisition owner/buyer, `.page-fluid`. Mirrors Build Bid's
position on the resell list detail. The old multi-select toolbar "Build Quote" launch **re-points to
this tab** (no two ways to build).

**Flow (single-stage inline — the Build-Bid shape):**
1. The tab lists the requirement's lines. Each line shows a **best-cost reference** (the new rollup =
   min unit cost across that requirement's active `Offer`s) + the loaded offers (best highlighted).
2. **Check** a line to include it. On check, the **sell-price field pre-fills**: the **last-quoted
   price** for that customer/part (via the already-shared `_preload_last_quoted_prices()`), else
   **best-cost × default markup** (markup % configurable + surfaced). Fully editable.
3. A live **margin** chip per line + a **blended-margin** total. A **guardrail warning** shows when
   sell < cost or margin < a threshold (new — Build Bid had no margin; sales does).
4. **Assemble** → `save_quote_from_builder` (the existing pipeline: `Quote` + `QuoteLine` + state
   transition + knowledge capture + the **revision lifecycle** Q-XXXX-Rn — all preserved) → a clean
   **summary card renders inline below** with totals.
5. **Download PDF** (`generate_quote_report_pdf` → `quote_report.html`, now via the new
   `quote_export_context()` whitelist) / Send (reuse the existing send path unchanged).

## Reuse (it's a reshape)

Keep the whole spine — the change is the **container + seeding**, not the save/PDF/math:
- **Service:** `quote_builder_service.get_builder_data` / `save_quote_from_builder` /
  `_preload_last_quoted_prices` / `generate_quote_report_pdf` — unchanged.
- **Alpine:** the `quoteBuilder` component (`htmx_app.js`) — simplified to the single-stage inline form
  (the decision box appears inline per checked line); reuse its `margin`/`extCost`/`extSell`/
  `blendedMargin`/`pricePosition` getters + `applyBulkMarkup`.
- **Models/PDF:** `Quote`/`QuoteLine`, `quote_report.html`, the WeasyPrint path. `QuoteBuilderSaveRequest`/`QuoteBuilderLine` schemas.
- **UI patterns:** clone the resell `_build_bid.html` tab + the offer comparison table; shared macros
  (`status_badge`, `stat_card`, compact-table, kebab, `empty_state`, toasts); **no new Tailwind classes**.

## Two genuinely new pieces
1. **Best-cost rollup for sales `Offer`s** — mirror of resell's `best_offer_unit_price`: min unit cost
   across a requirement's active offers (+ which offer), exposed as the reference column + the seeding
   source. (A small service/query; consider a cached column or compute-on-read — match how the resell
   rollup is done.)
2. **`quote_export_context()` whitelist** — an explicit pure-dict export context (Cost/Sell/Margin per
   line + customer header; **vendor identities never leak to the customer doc**), matching
   `bid_back_export_context()`. Makes the quote PDF testable to the same bar (today it relies on
   `quote_report.html` simply not including sensitive fields).

## Kept / strengthened
- **Margin math** (Cost/Sell/Margin/blended) — kept, **+ new guardrails** (warn sell<cost / margin<threshold).
- **Revision lifecycle** (Q-XXXX-Rn) — preserved; quotes are negotiated, versioned artifacts.
- **Customer-site link** on save — preserved.

## Design consistency
Inherit the app look + mirror Build Bid exactly: in-workspace lazy tab (explicit `hx-target`),
single-quoted `x-data='…|tojson'`, shared macros, existing `status_badge`/compact-table, **no new
Tailwind classes**. The tab should read identically to the resell Build Bid tab beside the existing
Quotes list.

## Out of scope (v1)
- The separate **cross-requisition bulk-quote composer** (its own flow; reshape later if wanted).
- Any **Send/email** behavior change (reuse the existing quote send path as-is).
- Margin-threshold *policy* config UI (use a sensible default threshold + the configurable markup %).

## Locked decisions (recommended defaults; flag to change)
1. **Container → in-workspace Build Quote tab** (user-approved). ✓
2. **Single-stage inline** assembly (check → seeded price → margin/guardrail → assemble → inline summary). ✓
3. **Seed:** last-quoted price → else cost × default markup (configurable, surfaced). ✓
4. **Keep + strengthen margin math** (guardrails); keep **revision lifecycle**. ✓
5. **Ship `quote_export_context()` whitelist** in this reshape. ✓
6. **Re-point** the old modal launch to the tab (retire the modal entry; the per-req path is the tab). ✓
7. Tab scope = the requisition/opportunity's lines (single-req quote; the bulk cross-req path stays separate). ✓

## Phasing (SDD chunks)
- **A — Service:** best-cost `Offer` rollup + `quote_export_context()` whitelist + margin-guardrail
  helper; tests.
- **B — Build Quote tab UI:** the in-workspace tab (reshape the modal markup → tab, clone `_build_bid.html`
  patterns), seeded single-stage assembly, inline summary, owner-gated route; re-point the old launch; tests.
- **C — Verify + ship:** APP_MAP docs, full suite, live-verify on PG (build a quote end-to-end, clean PDF),
  PR → CI → merge → deploy.

## Load-bearing files
- `app/services/quote_builder_service.py`, `app/routers/quote_builder.py` (+ requisitions2 detail router/tabs),
  `app/templates/htmx/partials/quote_builder/modal.html` (reshape source), `app/templates/htmx/partials/resell/_build_bid.html` (UX template to mirror),
  `app/static/htmx_app.js` (`quoteBuilder`), `app/models/quotes.py`, `app/models/offers.py` (Offer rollup),
  `app/templates/documents/quote_report.html`, `app/services/bid_back_service.py::bid_back_export_context` (whitelist pattern).
