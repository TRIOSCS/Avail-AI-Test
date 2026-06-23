# CLI Build Prompt — Resell / Excess Brokerage Module (v1)

> Paste this into a fresh Claude Code session in `/root/availai` to build v1.
> It is the build handoff for **`docs/superpowers/specs/2026-06-23-resell-module-design.md`** —
> read that spec in full first; it is the source of truth. This prompt is the marching order.

---

## Mission

Build **v1 of the Resell / Excess Brokerage module** ("Trading" workspace) per
`docs/superpowers/specs/2026-06-23-resell-module-design.md`. The module lets the resell team post a
customer's excess stock, collect buy-offers from other brokers (per-line **or** take-all), surface
the **best unit price per part**, and assemble a clean **bid back to the stock holder** — while every
posted line live-mirrors into `Sighting` so the existing matcher sees it for free.

**This is a reshape of the orphaned `excess` module, not greenfield.** Tables exist (migration
`29a41f5a248c`) but there's no data and no nav entry. Reshape in place; do not fork a parallel tree.

## How to work (non-negotiable — from CLAUDE.md)

1. **Isolate**: create a git worktree for this feature (`EnterWorktree`); run all pytest from inside it.
2. **Run the full pipeline**: writing-plans → TDD → execute (subagent-driven, max parallelism) →
   simplify → requesting-code-review → verification-before-completion. Don't skip steps.
3. **Brainstorm is already done** — start at **writing-plans** using the spec. Produce the plan,
   then execute it.
4. **Fat services / thin routers.** Logic in `excess_service.py` (+ small new services); routers HTTP-only.
5. **Tests with every change** (pytest, xdist-safe). Verify on **live Postgres** before claiming done
   (SQLite masks PG-invalid SQL).
6. **Update the APP_MAP docs** (`docs/APP_MAP_DATABASE.md`, `_ARCHITECTURE.md`, `_INTERACTIONS.md`) in
   the same PR.

## Locked decisions for v1 (from the spec — do not re-litigate)

- **Reshape in place**: keep `ExcessList` / `ExcessLineItem`; **retire** `Bid` + `BidSolicitation`;
  add **`ExcessOffer` / `ExcessOfferLine`**; UI relabel → **"Trading"**.
- **No price-based matching** (part-number only) but **price is collected** and rolled up to
  best-unit-price; `unit_price` is **nullable**.
- **Offer scope**: `per_line` | `take_all` (take-all carries optional `take_all_total_price`).
- **Unmatched offers queue** (never drop): `ExcessOfferLine.excess_line_item_id` nullable + `mpn_raw`.
- **Roles → capabilities** (`can_post`, `can_offer`); **block self-offer**.
- **Customer hidden** from offerers (view-discipline whitelist; strict MPN/qty/condition only).
- **Mirrored-sighting `requirement_id`** → a **virtual "unallocated excess" requirement per list**.
- **Lock-on-post**; revise = new version. **Late offers** → accept + flag `late`, queue for review.
- **Out of scope for v1**: external portal/auth, the match engine itself, Acctivate write-back,
  partial-publish, late-offer *revision* workflow (v2), Teams alerts (v2).

## Build sequence

1. **Schema + migration.** Reshape `app/models/excess.py`: retire `Bid`/`BidSolicitation`; add
   `ExcessOffer`/`ExcessOfferLine` (scope, nullable `unit_price`, `lead_time_days`, `mpn_raw`,
   nullable `excess_line_item_id`, `match_status`, `take_all_total_price`, status lifecycle); add to
   `ExcessLineItem`: `material_card_id`, `best_offer_unit_price`, `best_offer_id`, `offer_count`. Add
   status `StrEnum`s to `app/constants.py`. Autogenerate Alembic migration (id **≤32 chars**),
   review, upgrade→downgrade→upgrade, **coordinate the number via `MIGRATION_NUMBERS_IN_FLIGHT.txt`**,
   include rollback. Verify single `alembic heads`.
2. **Services.** In `excess_service.py`: list/line CRUD with `resolve_material_card()` on line create;
   `parse_tabular_file()` import; the **Sighting live-mirror** (single method owns the dual-write —
   see contract below); the **best-price rollup** (mirror `sighting_aggregation.rebuild_vendor_summaries()`
   keyed on `excess_line_item_id` over `ExcessOffer`). New `offer` intake path (AI funnel below),
   self-offer guard, unmatched queue. Drop `_call_claude_bid_parse()` and `send_bid_solicitation()`.
3. **AI data-entry funnel.** Wire `ai_intake_parser.parse_freeform_intake` (paste),
   `attachment_parser.parse_attachment` (upload), `ai_email_parser.parse_email` (email),
   `ai_part_normalizer.normalize_parts` (normalize, keep ≥0.7) → `normalize_mpn_key()` → match to
   posted lines → preview/confirm grid (reuse `import_preview.html`). Gate on `_ai_enabled(user)`.
4. **Router + nav.** Reshape `app/routers/excess.py` (thin); register the **Trading** tab in
   `app/templates/htmx/partials/shared/mobile_nav.html` (same shape as the other 8).
5. **Workspace UI.** Reshape the `excess/` templates per the spec's Reuse table into the two-panel
   `splitPanel()` Trading workspace: lens switch (My Lists / Open to Me), `stat_card` triage strip,
   opportunity-table-v2 left list (`status_dot`, `coverage_meter` as offer-coverage, `time_text`),
   adaptive detail (table / single-card / take-all banner), tabs Lines·Offers·Build Bid·Activity,
   offer comparison table cloned from `quote_builder/modal.html`. **Drop `solicit_modal.html`.**
6. **Bid-back.** Clone `quote_report.html` → `bid_report.html`; assemble from selected offers seeded
   by `best_offer_unit_price`; **enforce** stripping of trader names/sources; suppress the Vendor
   column in any Excel export.
7. **Customer hiding.** Offerer-facing serializer/partial = pure whitelist (MPN/qty/condition);
   filter `customer_excess` sightings from non-owning users.
8. **Tests + APP_MAP docs + live-verify on PG.**

## Sighting live-mirror contract (get this exact)

For each `ExcessLineItem`, write/update a `Sighting` (reuse `sighting_ingest.sighting_from_row()`):
- `normalized_mpn` via **`normalize_mpn_key()`** (NOT `normalize_mpn()`), `mpn_matched`, `material_card_id`.
- `source_type='customer_excess'`; `source_company_id = ExcessList.company_id` (the hiding hook).
- `requirement_id` = the list's **virtual "unallocated excess" requirement**.
- `vendor_name` = a synthesized internal label (do **not** feed the customer name into VendorCard dedup).
- Live mirror: qty drop / award / expire updates or retires the row. Upsert key
  `(source_company_id, material_card_id)` — do **not** let the connector-aware delete wipe siblings.

## Design consistency (must match the app)

Inherit, never re-style. Use shared macros only (`badge`/`status_badge`, `btn_*`, `stat_card`,
`filter_pill`, `coverage_meter`, `time_text`, `opp_*` cells, `empty_state`, global modal, `showToast`).
**No new colors** — pick resell status *values that already exist* in `status_badge`'s map so pills
inherit the app's colors (open→sky, collecting→`sourcing`/amber, bid_out→`quoted`/violet,
awarded→`won`/emerald, lost→rose). `.page-fluid` shell, `splitPanel`, morph swaps.

## Traps (each has bitten this codebase)

- Lazy tab via `hx-trigger="load"`/`intersect` MUST set explicit `hx-target` or it wipes the page.
- `x-data='…|tojson'` must be **single-quoted**.
- New Tailwind classes → canonical CSS layer + safelist; verify in built bundle after deploy.
- Rewire **every** `hx-*` endpoint + Jinja `include`/`from` path when reshaping templates (silent 404s).
- Align status-enum values to template `pill_colors`/`status_badge` keys or buttons/pills vanish.
- `db.get(Model, id)` not `.query().get()`; Loguru not print; `StrEnum` constants not raw strings.
- Run `pre-commit run --all-files` before the PR; deploy only via `./deploy.sh`.

## Definition of done

Migration applies+rolls back on PG; full pytest suite green from inside the worktree; the Trading tab
renders and is reachable from nav; can intake (paste/upload/single-line via AI), post (lines mirror to
`Sighting`), submit per-line + take-all offers (incl. unmatched→queue), see best-unit-price + offer
comparison, build a clean bid-back PDF (no trader identities); customer hidden on offerer view;
code-review findings all fixed; APP_MAP docs updated; live-verified on the deployed app.
