# Build-Quote In-Workspace Tab — Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Source of truth:
> `docs/superpowers/specs/2026-06-23-quote-builder-tab-design.md`. DRY against it. Reshape, not rebuild.

**Goal:** Reshape the sales Quote Builder from a full-screen modal into an in-workspace **Build Quote
tab** on the requisition/opportunity (mirroring the resell Build Bid tab) — best-cost pre-fill,
single-stage inline assembly, clean PDF. Keep the whole save pipeline + PDF + margin math + revision lifecycle.

## Global Constraints
- Reuse `quote_builder_service` (get_builder_data/save_quote_from_builder/_preload_last_quoted_prices/
  generate_quote_report_pdf), the `quoteBuilder` Alpine component, `Quote`/`QuoteLine`, `quote_report.html`.
- Mirror resell `_build_bid.html` + shared macros; **no new Tailwind classes**; lazy tab needs explicit
  `hx-target`; single-quoted `x-data='…|tojson'`; `.page-fluid`.
- Keep margin math + revision lifecycle (Q-XXXX-Rn) + customer-site link on save. ADD margin guardrails.
- Seed sell price: last-quoted (via `_preload_last_quoted_prices`) → else best-cost × default markup
  (configurable %, surfaced). Best-cost reference column visible.
- `db.get`, StrEnum, Loguru, fat-service/thin-router, tests-with-code. Verify anchors against current files.
- Likely **no migration** (rollup can be compute-on-read like the resell pattern; only add a column if needed
  — if so, coordinate MIGRATION_NUMBERS_IN_FLIGHT.txt). Additive; app imports + suite green between chunks.

## Chunks (SDD; review between)

### A — Service
- Best-cost rollup for sales `Offer`s: `best_cost_for(db, requirement_id)` (+ which offer) = min unit cost
  across that requirement's ACTIVE offers — mirror resell's `best_offer_unit_price` shape (prefer
  compute-on-read; no schema change unless clearly needed).
- `quote_export_context(quote) -> dict`: explicit whitelist (line: part/mfr/qty/condition/cost/sell/margin/
  extended + customer header) — **no vendor identity leaks**; mirror `bid_back_service.bid_back_export_context`.
  Wire `generate_quote_report_pdf` to render from it.
- `margin_guardrail(cost, sell, threshold) -> warning|None` helper.
- Tests: rollup math (min active offer), export-context whitelist (asserts no vendor keys), guardrail.

### B — Build Quote tab UI
- A **Build Quote** tab on the requisition/opportunity detail (requisitions2), sibling to the Quotes list
  tab; lazy `hx-get` body (explicit `hx-target`), owner/buyer-gated. Mirror `_build_bid.html`.
- Single-stage inline: lines with best-cost reference + offers; check → sell-price pre-fills (seed rule);
  live margin chip + blended total + guardrail warning; "Assemble" → `save_quote_from_builder` (revision
  lifecycle preserved) → inline summary card → Download PDF (via `quote_export_context`) / Send (reuse).
- Reuse/simplify the `quoteBuilder` Alpine component to the inline form. **Re-point** the old multi-select
  "Build Quote" toolbar launch to open this tab (retire the modal entry).
- Thin router endpoint(s) under the requisitions2/quote routes. Tests: route/render + gating + seeded
  price + guardrail render; no new Tailwind classes; console-safe.

### C — Verify + ship
- APP_MAP docs; full xdist suite; pre-commit --all-files; live-verify on PG (build a quote end-to-end on a
  seeded requisition with offers → clean PDF, no vendor identity). PR → CI → merge → deploy → live console/render verify.

## Self-review
Spec coverage: rollup+whitelist+guardrail (A), tab UI + seeding + assembly + re-point (B), docs/verify/ship (C).
Reuse preserves save/PDF/margin/lifecycle. Net-new = best-cost rollup + quote_export_context whitelist.
