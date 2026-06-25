# Resell Outreach & Buyer-Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Source of truth:
> `docs/superpowers/specs/2026-06-23-resell-outreach-design.md` (+ the outreach audit). DRY against them.

**Goal:** Add the outbound half of the Resell module — help a trader pick *who to offer excess to*,
track *who they offered to* + responses, build a *buyer scorecard* (good bidders + what they bid),
and warn on team overlap / nudge on forgotten buyers.

**Architecture:** Reuse ~70% — `email_service.send_batch_rfq`/`poll_inbox` via a thin adapter, the
coverage-rank suggestion shape, the vendor-intelligence layer (inverted to buyers). New: `ExcessOutreach`
record, `BuyerScore` rollup, `ActivityLog.excess_list_id` scope, `ExcessOffer` counterparty→vendor_card.
Fat services / thin router; the `excess_*` + `resell` (router/templates) modules are home.

## Global Constraints (verbatim)
- Tracker-centric, **email is the most common channel** (first-class automated; phone/Teams/marketplace = one-tap log into the same record).
- Match/suggest: **MPN bought-before → commodity → engagement**. Buyer-side summary (NOT the supply Sighting mirror).
- **Advisory overlap** (warn, never block; log override). **Don't-forget active** via the EXISTING cadence/nudge system + a "usually-offered, not yet" strip. **DEFER** My-Day-Task auto-creation behind one hook until CRM Phase 2.
- Per-list compose; per-(buyer×line) tracking rows. Counterparty canonical = `vendor_card_id`.
- Customer-hiding + clean-export discipline preserved. No new Tailwind classes (inherit). `db.get`, StrEnum, Loguru, fat-service/thin-router, tests-with-code.
- Migration: additive, coordinate `MIGRATION_NUMBERS_IN_FLIGHT.txt`, id ≤32 chars, single head, up/down/up, PG-valid.
- Build additive; app imports + full suite green between chunks. Verify anchors against current files (main moves).

## Execution chunks (SDD; review between each)

### Chunk A — Schema
- `app/models/excess.py`: add **`ExcessOutreach`** (excess_list_id FK, excess_line_item_id nullable FK,
  target_vendor_card_id FK, submitted_by FK User, channel StrEnum[email/phone/teams/marketplace/other],
  status StrEnum[sent/opened/responded/bid/declined/no_response], graph_message_id/graph_conversation_id
  nullable, parts_included JSON, sent_at, created_at) + **`BuyerScore`** (vendor_card_id FK unique,
  offers_received, wins, avg_bid_pct_of_ask, response_rate, median_response_hours, last_offered_at,
  commodity_affinity JSON, updated_at).
- `app/models/intelligence.py`: add nullable `excess_list_id` FK to `ActivityLog` (+ relationship).
- `app/constants.py`: add `OutreachChannel`, `OutreachStatus` StrEnums.
- Additive Alembic migration (chain onto current head; coordinate number). Tests: `tests/test_resell_outreach_models.py`.

### Chunk B — Send/log + reply adapter (service core)
- `app/services/resell_outreach_service.py`: `submit_outreach(db, *, list_id, owner, buyers, scope,
  channel, send_email)` → per-(buyer×line) `ExcessOutreach` rows; if email → adapt to
  `send_batch_rfq` (buyer contacts as the vendor_groups payload) + stamp graph ids; if log → just the
  rows. Guards: `can_post`/owner; resolve counterparty to `vendor_card_id` (backfill card for
  company-only). `record_response(...)` adapter: `poll_inbox` reply → advance `ExcessOutreach.status`
  (+ link the inbound `ExcessOffer`). Write `ActivityLog` (excess_list_id scope). Tests: send creates
  rows + graph ids; log path; reply advances status; counterparty canonicalization.

### Chunk C — Suggestions + buyer scorecard + overlap/nudge
- `app/services/buyer_affinity_service.py`: `rank_buyers_for(db, list_or_lines)` → tiered MPN→commodity→
  engagement (mirror `_coverage_ranked_vendor_rows` shape; buyer-keyed). `recompute_buyer_score(db,
  vendor_card_id)` (on offer-win + nightly hook). `overlap_warning(db, list_id, buyer)` (teammate
  offered this buyer these lines recently — advisory). `not_yet_offered_strip(db, list_id)`
  ("usually-offered this commodity, not on this list this round"). Tests for each.

### Chunk D — UI (resell workspace integration)
- "Offer to buyers" action on the resell list/lines → buyer panel (ranked suggestions + chips:
  last-bid/win-rate/last-contacted + advisory overlap flag; manual add; scope). Send/Log. The
  **Outreach tracker** tab/view on the list (who·when·by-whom·channel·status). Surface the "not-yet-
  offered" strip. Wire the don't-forget nudge into the existing nudge surface (NOT a new My-Day). One
  TODO-seam comment where the My-Day-Task hook goes (CRM Phase 2). Reuse shared macros; no new Tailwind.
  Thin router endpoints under `/v2/resell` + `/api/resell`. Tests: route/render + owner-gating + console-clean.

### Chunk E — Docs + verify + ship
- Update APP_MAP docs. Full xdist suite green; pre-commit --all-files; live-verify on PG (compose →
  send/log → tracker → scorecard). PR → CI → merge → deploy.

## Self-review
Spec coverage: ExcessOutreach (A), send/log+reply (B), suggestions+scorecard+overlap+nudge (C), UI tracker
(D), docs/verify (E). Deferred (clean seam): My-Day-Task auto-create. Counterparty canonicalization in A+B.
