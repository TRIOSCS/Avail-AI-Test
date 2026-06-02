# Search Page — Part History Column ("What we know")

**Date:** 2026-06-01
**Status:** Approved design — ready for implementation plan
**Branch:** `feat/search-part-history`

## Problem

The search page (`/v2/search`) is a pure **live market lookup**. A buyer enters an
MPN, the page fans out to every enabled supplier API (BrokerBin, Nexar, DigiKey,
Mouser, OEMSecrets, Element14, Sourcengine, eBay, AI web search, email mining) in
parallel and streams live vendor offer cards back over SSE. It shows **nothing about
what AvailAI already knows about that part internally** — past offers, who worked it,
confirmed business, sightings, open requisitions. A buyer has to leave the page and
open the material card to get that context.

## Goal

When a buyer searches an MPN, surface the **internal history for that part** in a
clean, organized column beside the live supplier results, answering "what do we
already know about this part?" at a glance — without leaving the search page.

## Scope (in)

For the searched MPN, the history column surfaces:

- **Offers** — past `Offer` records (vendor, qty, price, status, date).
- **Buyers** — the distinct users who logged/worked offers on this part.
- **Confirmed / Won** — defined explicitly as **won/sold `Offer`s + won `Requisition`s
  + `CustomerPartHistory` purchase rows**.
- **Sightings** — vendor sightings from prior searches & email mining.
- **Requisitions / Requirements** — `Requirement` rows referencing the part (open vs
  closed), with status.
- **Price trend** (bonus) — a light min / max / last summary from
  `MaterialPriceSnapshot`.

## Scope (out)

- No change to the live supplier search itself (left column is untouched).
- No new database tables or columns; **no Alembic migration** — read-only over
  existing tables.
- No cross-MPN / alternate-part family rollup. History is scoped to the searched
  part's own material card only (see Decisions).
- No fuzzy MPN matching beyond the existing `normalize_mpn` normalization.

## Layout

The search results shell becomes a two-column grid:

```
MPN: STM32F407VGT6   [Search]
─────────────────────────────┬────────────────────────
Live market (streaming…)      │ What we know
 [Mouser card]                │  STM32F407VGT6 · ST · Active
 [DigiKey card]               │  Open full part page →
 [BrokerBin card]             │  Offers 12 · Won 3 · Sightings 40
 … filter bar, shortlist      │  Reqs 5 · Buyers 2
                              │  ▾ Offers   ▸ Confirmed
                              │  ▸ Sightings ▸ Requisitions
```

- Grid: `lg:grid-cols-[1fr_360px]`; stacks (history below live results) on narrow
  viewports.
- **Left column** — existing live search, unchanged: source progress chips, SSE
  result cards, filter bar, shortlist bar.
- **Right column** — the "What we know" panel. Loaded by its **own** HTMX request
  (`hx-get` to the history endpoint) firing **in parallel** with the SSE stream, so
  neither blocks the other. A skeleton shows while it loads.

### Right-panel structure (Alpine accordion)

- **Header** — display MPN · manufacturer · lifecycle-status chip ·
  **"Open full part page →"** deep link to `/v2/materials/{card_id}`.
- **Summary stat row** — `Offers N · Won N · Sightings N · Reqs N · Buyers N`.
- **Collapsible sections** — top 5 rows each, with a "view all on part page" link:
  - **Offers** — vendor, qty, unit price, status, date.
  - **Confirmed / Won** — won/sold offers + won requisitions + customer purchases.
  - **Sightings** — vendor, qty, price, source type, date.
  - **Requisitions** — req #, customer, status.
- **Buyers** — chip row of the users who worked this part.
- **Empty state** — no card or no records → a quiet
  "No prior history — this part looks new to us."

## Architecture (Approach A — shared service)

### New: `app/services/part_history_service.py`

A single entry point returning a structured summary:

```python
def get_part_history(db: Session, normalized_mpn: str) -> PartHistory: ...
```

- `PartHistory` is a small dataclass: `found: bool`, `card_id: int | None`,
  `display_mpn`, `manufacturer`, `lifecycle_status`, per-section count + top-N row
  lists (offers, confirmed, sightings, requirements), `buyers` list, and the price
  trend summary.
- Resolves the active `MaterialCard` by `normalized_mpn` with `deleted_at IS NULL`.
  No card → `PartHistory(found=False)`.
- All aggregation is **scoped by `material_card_id`** (parity with the materials
  detail page — same source of truth).
- The discrete query helpers that currently live inline in the materials router
  (`material_detail_partial`, `material_tab_partial`) move **into this service**;
  both the materials router and the new history panel consume them. This removes the
  current duplication and prevents the two views from drifting.

### New endpoint: `GET /v2/partials/search/history?mpn=…`

In `app/routers/htmx_views.py`:

- `require_user` (consistent with the other `/v2/partials/search/*` endpoints).
- Normalizes `mpn` via `app/utils/normalization.normalize_mpn_key` (the same key
  `MaterialCard.normalized_mpn` is stored under — strip-all-nonalphanumeric,
  lowercase), looks up the card **read-only** (no create), calls `get_part_history`,
  renders `htmx/partials/search/history_panel.html`.
- Wrapped in `try/except` → logs via Loguru and returns a small inline error notice.
  **No silent failure.** A single failed sub-section degrades to an error sliver
  rather than blanking the whole panel.

### Modified: `app/templates/htmx/partials/search/results_shell.html`

Wraps the existing content in the left column of the two-column grid and adds the
right column with the `hx-get` trigger + skeleton.

### New: `app/templates/htmx/partials/search/history_panel.html`

Renders the `PartHistory` summary (header, stat row, accordion sections, buyers row,
empty state).

### Modified: `app/routers/materials.py` / materials partials in `htmx_views.py`

`material_detail_partial` and `material_tab_partial` refactored to call the shared
service helpers instead of their own inline queries. Rendered output stays identical
(guarded by regression test).

## Data flow

1. Buyer submits MPN → existing `POST /v2/partials/search/run` returns the
   `results_shell.html` two-column shell.
2. Left column opens the SSE stream and streams live supplier cards (unchanged).
3. Right column independently fires `GET /v2/partials/search/history?mpn=…`.
4. Endpoint normalizes the MPN, resolves the `MaterialCard` (guaranteed to exist —
   `search_run` already upserts/resolves one via `_upsert_material_card` /
   `resolve_material_card`), calls `get_part_history`, renders the panel.
5. Panel swaps into the right column.

## Error handling

- Endpoint-level `try/except` logs via Loguru and renders an inline error notice.
- Sub-section query failures inside the service are caught per-section and surfaced
  as an error sliver in that section; the rest of the panel still renders.
- Card not found / no history → explicit empty state (not an error).

## Testing

- **Service unit tests** (`tests/`): card found with mixed records → correct counts +
  top-N ordering; no card → `found=False`; soft-deleted card excluded; buyers
  derivation from `Offer.entered_by`; confirmed/won composition (won offers + won reqs
  + customer purchases).
- **Endpoint tests**: `200` with all sections present for a part with history;
  unknown MPN → empty-state HTML; unauthenticated request blocked.
- **Materials-detail regression test**: detail + tabs still render the same data after
  the refactor (parity).
- **Template smoke**: history panel passes html-validate per existing patterns.

## Docs

- Update `docs/APP_MAP_INTERACTIONS.md` — new search → part-history data flow.
- Update `docs/APP_MAP_ARCHITECTURE.md` — new `part_history_service`.

## Decisions

- **History scoping = by `material_card_id`** (not a broader `normalized_mpn` sweep of
  unlinked rows), for exact parity with the materials detail page. `search_run`
  already guarantees a card exists for the searched MPN, so the column is reliably
  populated.
- **Confirmed / Won = won/sold `Offer`s + won `Requisition`s + `CustomerPartHistory`
  purchase rows.**
- **No migration** — feature is read-only over existing tables.
