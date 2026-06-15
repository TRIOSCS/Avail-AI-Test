# Search → Part Dossier ("The Bench") — Design Spec

**Date:** 2026-06-15
**Surface:** the "Search" tab → a single one-PN **Part Dossier** at `/v2/search?mpn=<PN>`
**Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL + HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind 3.x. **NOT React.**
**Companion artifacts (same folder):**
- `2026-06-15-search-dossier-frontend.md` — full visual/interaction spec ("The Bench")
- `2026-06-15-search-dossier-mockup.html` — static Tailwind mockup (build reference)
- `2026-06-15-search-dossier-simplify.md` — the YAGNI/reuse critique that shaped scope

---

## 1. Goal

Turn the Search tab from "a results list with a sidebar" into a **one-part dossier**: type a part
number and get *everything AVAIL knows* plus the *live market*, in one clean scrolling page, with
**full sourcing actions inline** — send RFQs to vendors, add offers, add to a requisition — without
leaving the page. A trader pulls up a PN and can learn the part and source it in one place.

Success = a buyer can, from `/v2/search?mpn=<PN>`: (1) read the part's identity, specs, price trend,
and trading history instantly; (2) watch the live multi-source market populate; (3) send an RFQ, log
an offer, or add the part to a requisition — all from this page.

## 2. Locked decisions

These are settled. The spec resolves every downstream detail against them; there are no open options.

1. **Page shape:** a single **scrolling dossier**, not tabs. Top→bottom: persistent search bar →
   identity hero + action bar → **Live Market** → **What We Know** (history) → **Specs & Enrichment**.
2. **Distinct page, reuse data only.** The dossier is its **own** new page with its **own** hero/specs
   templates (the "Bench" design). It **reuses the data/services and the live-market engine and the
   history panel**, but the existing **MaterialCard detail page (`materials/detail.html`) is NOT
   modified** and not shared into.
3. **Frictionless scratch requisition** is the home for one-off actions. Acting (RFQ / add offer)
   creates — only on the action, never on a bare search — a lightweight `Requisition(is_scratch=True)`
   + `Requirement`, then hands off to the **existing unchanged** RFQ/offer flows. "Add to Requisition"
   is the *intentional* path (pick an existing open req or create a named one) and is **not** scratch.
4. **Instant-knowledge, live-market-fills-in.** The dossier paints hero/specs/history synchronously
   from the DB; the live market streams in below.
5. **Market refresh:** on open, reuse a fresh cached market result (<15 min, existing Redis TTL) if
   present, else auto-run the connector sweep once; a **"↻ Refresh market"** button always force-reruns.
6. **Light-footprint writes:** a bare search only bumps `MaterialCard.search_count` / `last_searched_at`
   and records a `MaterialPriceSnapshot`. **No per-vendor Sightings are persisted until an action.**
7. **Persistence source = client-held rows, not Redis.** When an action persists market results as
   Sightings, the rows are taken from the **client-posted payload** (mirroring `add_to_requisition`),
   not re-read from the Redis cache — this removes the 15-min TTL race entirely.
8. **Full scope, one effort:** page-level **and** per-row actions, plus a recent-searches landing.
9. **One justified schema change:** add `requisitions.is_scratch BOOLEAN`. No status overloading (a
   rejected band-aid), no `RequisitionType` enum, no `scratch_expires_at`, no cleanup job.
10. **No `routers/search.py` extraction.** Moving the existing 10 search routes out of `htmx_views.py`
    is refactor scope-creep that risks the build and makes the feature diff unreviewable (CLAUDE.md:
    don't bundle unrelated drift). New routes live in a **net-new** module `app/routers/part_dossier.py`;
    existing search routes stay where they are and are referenced by URL.

## 3. Non-goals (explicit)

- No change to `materials/detail.html`, the materials list, or any non-search route.
- No auto-creation of `VendorCard`s for unknown brokers. RFQ compose shows **known-vendor** sightings
  only — this matches today's `rfq_compose` behavior (`htmx_views.py:2581-2590`).
- No new connector, scoring, dedup, or SSE-broker work. The live-market engine is reused verbatim.
- No client-side routing or JSON APIs. Server-rendered HTML fragments swapped by HTMX.

---

## 4. Route map

### 4.1 New routes — all in `app/routers/part_dossier.py`

| Method · Path | Handler | Returns |
|---|---|---|
| `GET /v2/partials/search` *(see 4.3)* | `search_dossier` | landing (no `mpn`) **or** `dossier_shell.html` (with `mpn`) |
| `GET /v2/partials/search/dossier/hero?mpn=` | `dossier_hero` | `dossier_hero.html` (identity + action bar, DB-only, instant) |
| `GET /v2/partials/search/dossier/market?mpn=` | `dossier_market` | `dossier_market.html` (cache-hit static cards **or** SSE shell) |
| `GET /v2/partials/search/dossier/history?mpn=` | `dossier_history` | `history_panel.html` (delegates to existing `get_part_history`) |
| `GET /v2/partials/search/dossier/specs?mpn=` | `dossier_specs` | `dossier_specs.html` |
| `GET /v2/partials/search/recent` | `search_recent` | `dossier_recent.html` (landing's recent-searches list) |
| `POST /v2/partials/search/quick-source/rfq` | `quick_source_rfq` | `HX-Redirect` → existing `rfq-compose` |
| `POST /v2/partials/search/quick-source/offer` | `quick_source_offer` | `HX-Redirect` → existing `add-offer-form` |
| `POST /v2/partials/search/quick-source/add-to-req` | `quick_source_add_to_req` | requisition picker / confirmation |

All POSTs are HTMX-triggered from within the page; the existing CSRF double-submit cookie covers them —
**no CSRF exemption** is added (consistent with today's `POST /v2/partials/search/run`).

### 4.2 Reused routes — unchanged, in `htmx_views.py`, referenced by URL only (no import coupling)

- `POST /v2/partials/search/run` → launches `stream_search_mpn`, returns `results_shell.html` (SSE).
- `GET /v2/partials/search/stream` → SSE endpoint.
- `GET /v2/partials/search/filter` → re-render cached cards with confidence/source/sort filters.
- `GET /v2/partials/search/lead-detail` → lead-detail drawer body.
- `GET .../requisitions/{req_id}/rfq-compose`, `.../add-offer-form`, the requisition picker,
  `add_offer_htmx` — the action hand-off targets, all unchanged.

### 4.3 Deep-linking — the one targeted edit to `htmx_views.py`

`v2_page` (the `/v2/*` full-page catch-all, `htmx_views.py:279`) currently maps the search tab to
`partial_url = "/v2/partials/search"`. Modify **only** the search branch to pass an `mpn` query param
through, so the page is bookmarkable / linkable:

```python
# in v2_page(), search branch:
mpn_qs = request.query_params.get("mpn", "").strip()
partial_url = f"/v2/partials/search?mpn={quote(mpn_qs)}" if mpn_qs else "/v2/partials/search"
```

`base_page.html` already fires `hx-get="{{ partial_url }}"` on load, so `?mpn=` rides along. The browser
URL is already `/v2/search?mpn=<PN>` from the initial GET — no `hx-push-url` needed on the first paint.
When the user submits a **new** PN from the dossier search bar, that form uses
`hx-get="/v2/partials/search?mpn=<new>"`, `hx-target="#main-content"`, `hx-push-url="/v2/search?mpn=<new>"`.

This makes the global typeahead, FRU-crosswalk links, and history links able to deep-link a dossier by
setting `href="/v2/search?mpn=<PN>"` (wiring those callers is in scope where they already point at search).

---

## 5. Data model change

**Migration:** `alembic/versions/107_is_scratch_requisitions.py` (revision id `107_is_scratch_requisitions`,
27 chars — under the 32-char `alembic_version` limit). `down_revision = "106_brand_canonicalization"`
(the current single head — verify with `alembic heads` at build time). Claim line appended to
`MIGRATION_NUMBERS_IN_FLIGHT.txt` in the same commit:
`107  feat/search-part-dossier  is_scratch flag on requisitions (scratch/quick-source reqs)`.

**Up:**
```sql
ALTER TABLE requisitions ADD COLUMN is_scratch BOOLEAN NOT NULL DEFAULT FALSE;
-- partial index: scratch reqs are looked up per-user during get-or-create
CREATE INDEX ix_requisitions_scratch_user ON requisitions (created_by) WHERE is_scratch = TRUE;
```
**Down:** `DROP INDEX ix_requisitions_scratch_user; ALTER TABLE requisitions DROP COLUMN is_scratch;`

**Model** (`app/models/sourcing.py`, `Requisition`): add
`is_scratch = Column(Boolean, nullable=False, default=False, server_default="false")` and the partial
index in `__table_args__` (`postgresql_where`). The `server_default` keeps every existing row valid.

**List-query guard (same PR as the column):** the requisitions workspace/list query and the requisition
**picker** query must add `.filter(Requisition.is_scratch.is_(False))` so scratch reqs never leak into
the normal requisitions UI or the "Add to Requisition" picker. (A scratch req keeps a real lifecycle
`status` so it behaves normally once promoted; provenance and lifecycle stay orthogonal.)

> **SQLite-masks-Postgres note:** the `WHERE is_scratch = TRUE` partial index is PG-only; SQLite ignores
> `postgresql_where`. Idempotency is enforced in the service layer (section 6), not by a DB unique
> constraint, so behavior is identical on both engines. Tests assert the service behavior, not the index.

**Promotion:** editing a scratch req's customer/name in the normal requisition edit flow sets
`is_scratch = False` (it becomes an ordinary requisition). No separate "promote" endpoint in v1.

---

## 6. Service layer — `app/services/quick_source_service.py` (new)

File header comment required (what/calls/depends, per CLAUDE.md). Two functions; business logic lives
here, the router stays thin.

```python
def get_or_create_scratch_req(db, user, mpn) -> tuple[Requisition, Requirement]:
    """Idempotent per (user, normalized mpn) among the user's OPEN scratch reqs.
    Returns the existing (req, requirement) if one exists; else creates
    Requisition(name=f"Quick-source: {display_mpn}", is_scratch=True, customer_name=None,
    status=RequisitionStatus.OPEN, created_by=user.id) + Requirement(primary_mpn=mpn),
    links Requirement.material_card_id via resolve_material_card, db.flush() so ids exist."""

def persist_rows_as_sightings(db, requirement, rows) -> list[Sighting]:
    """`rows` is the CLIENT-POSTED market payload (list of dicts). Reuses the SAME shared
    Sighting-creation helper as add_to_requisition (extracted in this PR). Requires
    requirement.id (NON-NULL FK Sighting.requirement_id). Calls
    apply_to_fresh_sightings(db, requirement, created) for unavailability re-application.
    Returns the created Sightings. Skips rows with no vendor name."""
```

- **Idempotency** uses a `SELECT ... WHERE created_by = :u AND is_scratch AND status = OPEN`
  + a normalized-MPN match on the linked Requirement, inside the request transaction. Concurrent
  double-clicks are made safe by `db.flush()` + catching the rare integrity race and re-selecting.
  We do **not** rely on a DB unique constraint (SQLite parity).
- **MPN normalization:** use `normalize_mpn_key` from `app.utils.normalization` **consistently** for the
  lookup key and the display name, matching `search_history_panel`'s key. The `Requirement.primary_mpn`
  `@validates` hook uppercases on save.
- **Shared Sighting-creation helper:** extract the row→`Sighting(...)` construction loop currently inline
  in `add_to_requisition` (`htmx_views.py:3539`) into a small helper (e.g.
  `app/services/sighting_ingest.py::sighting_from_row`) and call it from **both** `add_to_requisition`
  and `persist_rows_as_sightings`. This is reuse, not a rewrite — `add_to_requisition`'s behavior is
  unchanged (verified by its existing tests).

**Why client-held rows (decision #7):** `rfq_compose` builds its vendor list from Sightings joined to
`VendorCard` (`htmx_views.py:2581-2590`); Sightings must exist before RFQ. The market rows are already
in the DOM, so the action posts them directly (as `add_to_requisition` already posts shortlist `items`).
No Redis read, no TTL race. If the payload is empty (e.g. zero market results), the scratch req +
requirement are still created (useful ownership) and the action surface shows: *"No market rows captured
— refresh the market, then try again."*

---

## 7. Templates & the "Bench" visual system

Full spec: `2026-06-15-search-dossier-frontend.md`. Binding rules for the build:

**Design language.** Dual-surface "Bench": **paper** (light: `bg-white` on a faint `brand-50` field,
`border-brand-200`) for *our settled knowledge* (hero, history, specs); **terminal** (dark:
`bg-gray-900`/`bg-gray-800`) for the *live market* streaming in. The light↔dark seam is the page's
signature. Refined-utilitarian: dense, `font-mono` numerics, tight rhythm, no decorative gradients.
Reuse the existing `brand-*` palette, mono-for-MPN convention, and `emerald`/`amber`/`rose` status
semantics. **No new colors, fonts, or framework.**

**New partials (in `app/templates/htmx/partials/search/`):**

- `dossier_shell.html` — `max-w-5xl mx-auto` single column. Holds: the **sticky search bar**
  (`sticky top-12`); a `dossier-hero` div (`hx-get .../hero`, `hx-trigger="load"`, `hx-target="this"`,
  `hx-swap="outerHTML"`, skeleton inside); three collapsible sections each lazy-loading their endpoint;
  the reused **lead-detail drawer** markup (moved from `form.html`). **Every** lazy-load div carries an
  explicit `hx-target="this"` (under `#main-content`'s `hx-target="this"`, omitting it would clobber the
  whole page — CLAUDE.md anti-pattern). A **tier-2 condensed sticky rail** (MPN · price · primary
  actions) appears after the hero scrolls off, via Alpine `x-intersect`.
- `dossier_hero.html` — the 3-zone identity hero (identity block / price strip + sparkline / count
  tiles) + the **action bar**. Renders entirely from `MaterialCard` + `PartHistory` summary. Includes
  the graceful **"New to us"** / **"Known via FRU crosswalk"** unknown-part state.
- `dossier_market.html` — the dark "market terminal": source-progress chip row (existing `source-chip`
  CSS verbatim), freshness stamp + "↻ Refresh market", and the **row** feed. Two render branches
  (cache-hit static vs. cache-miss SSE shell) per section 8.
- `dossier_specs.html` — spec grid (`dl`, `grid-cols-2 lg:grid-cols-3`) from `MaterialCard` enrichment
  fields with per-value **evidence-tier** tags (`evidence_tiers.py`); "enriching…" skeleton state.
- `dossier_recent.html` — the landing's recent-searches list (section 9).

**Changed partial:** `vendor_card.html` gains, under a Jinja `{% if in_dossier %}` flag, the per-row
quick actions (section 10.2) and renders as a **terminal row** when in the dossier. Outside the dossier
it is unchanged. The market section passes `in_dossier=True`.

**Collapse interaction.** Each section is Alpine `x-data="{ open: ... }"` with `x-collapse` and a
rotating chevron — copied from the existing `materials/detail.html` collapse pattern. Default open:
Live Market + What We Know; closed: Specs. State persisted with the `@persist` plugin
(`$persist`) so an operator's layout survives navigation.

**Mobile.** Single-column stack. Hero zones B/C drop under zone A. The terminal rows collapse to the
existing mobile-correct `vendor_card` card layout. The action bar becomes a `sticky bottom-[52px]`
action sheet above the fixed bottom nav (Send RFQ filled + "Actions" sheet trigger).

---

## 8. Per-section data flow

`search_dossier(mpn)`:
- No `mpn` → render landing: search box (`form.html` body) + `dossier_recent.html` (recent searches).
- With `mpn` → resolve via `normalize_mpn_key`; **light-footprint write** (bump `search_count` /
  `last_searched_at` — already done by `resolve_material_card`, `search_service.py:~2104`); render
  `dossier_shell.html` with `mpn` + `card` (may be `None`). The shell's lazy-loads fire in parallel.

**Hero / History / Specs** — synchronous DB reads, no connectors, render immediately (no skeleton flash):
- `dossier_hero` → `MaterialCard` by `normalized_mpn` + `get_part_history` summary counts.
- `dossier_history` → existing `part_history_service.get_part_history` → `history_panel.html` (verbatim).
- `dossier_specs` → `MaterialCard` enrichment fields → `dossier_specs.html`.

**Live Market** — `dossier_market(mpn)`:
1. Look up `search:{normalize_mpn_key(mpn)}:latest` in Redis (a new pointer key written at the end of
   `stream_search_mpn`, TTL 900s, alongside the existing `search:{search_id}:results` write at
   `search_service.py:~2663`).
2. **Cache hit** (pointer present + results fresh): render static vendor rows directly from the cached
   results (same render path as `search_filter`), freshness stamp "cached Nm ago", plus "↻ Refresh".
3. **Cache miss / Refresh:** inner div `hx-post`s `/v2/partials/search/run` (`hx-trigger="load"`),
   returning the existing `results_shell.html` SSE experience embedded in the terminal frame. The
   `MaterialPriceSnapshot` write already happens inside the stream path.
4. The market container exposes the active `search_id` as `data-search-id` for per-row Details and for
   the cache-hit refresh.

**Unknown PN** (no `MaterialCard`): hero shows "New to us" (or FRU-aware variant), history shows the
existing `found=False` empty state, specs shows "Unenriched", market runs normally. First action creates
both the card (via the existing on-add path) and the scratch req.

---

## 9. Recent-searches landing (no-PN state)

`search_recent` returns the user's most-recently-searched parts: `MaterialCard` rows ordered by
`last_searched_at DESC` (top ~12), each a deep link `href="/v2/search?mpn=<display_mpn>"` showing
MPN · manufacturer · last-searched-ago · sighting/offer counts. Empty state = today's clean search
prompt. This reuses existing columns only (`search_count`, `last_searched_at`) — no new tracking.

---

## 10. Actions

### 10.1 Page-level (in the hero action bar)

- **Send RFQ** *(primary, filled `bg-brand-600`)* → `POST .../quick-source/rfq` with the client-posted
  market rows (shortlisted if any are selected, else all currently-shown rows). Server:
  `get_or_create_scratch_req` → `persist_rows_as_sightings` → `HX-Redirect` to the existing
  `…/{req_id}/rfq-compose`. A toast confirms *"Quick-source req created · N sightings captured."*
- **Add Offer** *(secondary)* → `POST .../quick-source/offer` → scratch req (+ optional prefill) →
  `HX-Redirect` to the existing `…/{req_id}/add-offer-form`.
- **Add to Requisition** *(secondary, intentional path — NOT scratch)* → opens the existing requisition
  **picker** popover (typeahead over open, non-scratch reqs) with a "+ New requisition…" footer; on pick,
  reuse `add_to_requisition` to add this PN as a Requirement + persist the rows as Sightings.
- **⋯ overflow** → Refresh market · Copy MPN · Open full part page (`/v2/materials/<id>` when the card
  exists) · Add to shortlist.

### 10.2 Per-row (on each market row, `{% if in_dossier %}`)

- **RFQ this** → `POST .../quick-source/rfq` with **only that row** → scratch req → rfq-compose
  pre-scoped to that vendor.
- **+ Offer** → `POST .../quick-source/offer` with that row's vendor/price/qty prefilled.
- **Details →** → existing lead-detail drawer (`hx-get .../lead-detail` → `#lead-drawer-content`,
  `drawerOpen=true`). Reused verbatim.
- The existing **shortlist checkbox** stays at row-left (`$store.shortlist`) so page-level Send RFQ can
  target a multi-vendor selection.

All action endpoints are idempotent on the scratch req (section 6), so RFQ-then-Add-Offer on the same PN
reuse one scratch req rather than spawning duplicates.

---

## 11. Testing plan

Always-with-the-code (CLAUDE.md). `TESTING=1`, SQLite in-memory unless noted.

- `tests/test_quick_source_service.py`: `get_or_create_scratch_req` idempotency (two calls → one row);
  scratch req created with `is_scratch=True`, `status=OPEN`, null customer; `persist_rows_as_sightings`
  from a mock row payload creates N Sightings with `requirement_id` set; empty payload → req created,
  zero Sightings; shared `sighting_from_row` helper parity with `add_to_requisition`.
- `tests/test_part_dossier_router.py`: `GET /v2/partials/search` (no mpn) → landing; with `?mpn=` →
  dossier shell 200; hero/history/specs endpoints 200 for known **and** unknown PN (graceful states);
  recent-searches endpoint 200; `v2_page` `?mpn=` passthrough.
- `tests/test_quick_source_actions.py`: RFQ/offer POSTs create scratch req + Sightings + emit the right
  `HX-Redirect`; per-row single-vendor variant; empty-payload warning path; "Add to Requisition" routes
  through `add_to_requisition` (non-scratch).
- Migration: `107` upgrade→downgrade→upgrade roundtrip; `alembic heads` single-head assertion; the
  in-flight claim-line test passes.
- Requisitions list/picker **exclude** scratch reqs (regression test on the added filter).
- **Live-verify on the deployed app** after ship (real PG): drive `/v2/search?mpn=<known PN>`, confirm
  instant hero + streaming market + an RFQ creating a scratch req — the path 16k SQLite tests can't
  exercise (per `feedback_live_verify_finds_sqlite_masked_500s`).

## 12. Risks & mitigations

1. **SSE inside a collapsible** — closing the `<details>` mid-stream doesn't close the SSE; swap targets
   still receive events and the stream completes. Existing disconnect handling in the stream generator is
   preserved. No action beyond keeping that logic.
2. **`search_id` for per-row Details** — surfaced as `data-search-id` on the market container; buttons
   read it via `dataset`, no Alpine store mutation.
3. **Empty market payload on action** — handled (section 6): req created, zero Sightings, explicit
   user-facing warning. No silent failure.
4. **Scratch reqs leaking into the requisitions UI** — the `is_scratch` filter ships in the **same PR**
   as the column (list + picker). Without it, scratch reqs would appear immediately.
5. **`normalize_mpn_key` consistency** — one normalization function used across hero, history, scratch
   req key, and market pointer key.
6. **Known-vendor-only RFQ** — accepted v1 behavior (matches today). Brokers with no `VendorCard` won't
   appear in rfq-compose even after Sightings persist; this is documented, not a bug.
7. **Migration number drift** — 106 is already taken; we use **107** and re-confirm the head with
   `alembic heads` at build time, re-chaining if a newer head merged first (per the in-flight protocol).

## 13. Build order

One feature branch (`feat/search-part-dossier`) in an isolated worktree. Internal order (each step
green before the next; may land as 2-3 stacked PRs for review sanity, but all ship together):

1. **Schema + service:** `is_scratch` migration 107 + model + list/picker filter; extract
   `sighting_from_row` shared helper; `quick_source_service`; tests.
2. **Dossier shell + sections + deep-link:** `part_dossier.py` GET routes; `v2_page` `?mpn=` edit; the
   Bench `dossier_shell` / `dossier_hero` / `dossier_specs` templates; history reuse; recent-searches
   landing; tests.
3. **Live-market terminal:** `dossier_market` (cache-hit/miss); `search:{mpn}:latest` pointer write in
   `stream_search_mpn`; terminal re-skin of `vendor_card` (`in_dossier`); per-row Details; tests.
4. **Actions:** page-level + per-row RFQ / offer / add-to-req endpoints wired to the service; tests.
5. **Polish + verify:** `frontend-design` pass against the mockup; full suite; `pre-commit run
   --all-files`; deploy; live-verify; update `docs/APP_MAP_ARCHITECTURE.md` (new router/page) and
   `docs/APP_MAP_INTERACTIONS.md` (dossier + scratch-req flow).

## 14. Reuse ledger

| Reused verbatim (data/logic) | New (presentation only) |
|---|---|
| `stream_search_mpn`, SSE broker, scoring, dedup | `dossier_shell` / `dossier_hero` / `dossier_specs` |
| `part_history_service.get_part_history` + `history_panel.html` | `dossier_market` terminal frame |
| `add_to_requisition` Sighting loop → shared `sighting_from_row` | `dossier_recent` landing |
| `rfq_compose` / `add_offer_htmx` / requisition picker (hand-off) | `vendor_card` terminal-row variant (`in_dossier`) |
| lead-detail drawer, `source-chip` CSS, `$store.shortlist` | tier-2 sticky rail, count tiles, sparkline |
| `MaterialPriceSnapshot`, `resolve_material_card` light write | — |
| `evidence_tiers`, `normalize_mpn_key`, `apply_to_fresh_sightings` | — |
