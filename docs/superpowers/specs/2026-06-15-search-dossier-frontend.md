# Part Dossier — Frontend Visual & Interaction Design

**Date:** 2026-06-15
**Surface:** Search tab → single one-PN Part Dossier at `/v2/search?mpn=<PN>`
**Stack:** HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind 3.x (brand-* palette). NO new framework.
**Mockup:** `docs/superpowers/specs/2026-06-15-search-dossier-mockup.html`

---

## 0. Design thesis — "The Bench"

This is not a SaaS dashboard. It is a **trader's bench**: one part on the table, every fact
about it pulled into reach, and the market lit up live next to it. The page reads top-to-bottom
like a sourcing **dossier** — identity first (what is this thing, what do we already know), then
the **live market** (where can I get it right now), then **what-we-know** (our trading memory),
then **specs**. Actions are never more than one tap from where the eye already is.

The defining aesthetic move is **the dual-surface system**, which already exists in the codebase
and we lean into hard:

- **Paper (light):** the page chrome, identity hero, what-we-know, specs — `bg-white` on a faint
  `brand-50` field, `border-brand-200`, slate-blue ink. This is *our knowledge* — settled, owned,
  authoritative.
- **Terminal (dark):** the LIVE MARKET module is a `bg-gray-900` slab with `bg-gray-800` rows.
  This is *the outside world streaming in* — volatile, real-time, lit by per-source neon badges.
  The existing `vendor_card.html` is already dark; we promote that surface into a framed
  "market terminal" so the live feed reads as a distinct instrument embedded in the dossier.

The light↔dark seam is the signature of the page. Nothing else in the app does this; it makes the
dossier instantly recognizable and reinforces the mental model (settled knowledge vs. live feed).

This is **refined-utilitarian**, not maximalist: dense, monospaced numerics, tight vertical rhythm,
no decorative gradients, no rounded-everything. Restraint + precision. The drama comes entirely
from the paper/terminal contrast and the staggered live-stream reveal — not from ornament.

---

## 1. Layout grid, max-width, vertical rhythm, sticky behavior

### 1.1 Page container
- Single **scrolling** column. Outer page: `min-h-screen bg-brand-50` (faint cool field, NOT pure
  white) so every white card lifts off it. `main` keeps its existing `p-4 pb-[52px]`.
- Content max-width: **`max-w-5xl mx-auto`** (1024px). The dossier is a *document*, not a wall-to-wall
  grid — a contained measure makes the dense numerics readable. Two narrow internal asides (price
  spark, counts) live inside sections, not as a global second column. The market terminal alone may
  bleed to `max-w-6xl` on `xl:` to give the row table breathing room.
- Vertical rhythm: sections separated by **`space-y-5`** (20px). Inside a section, `space-y-3`.
  Section headers sit on a `28px` band. This is the same rhythm as `form.html` (`space-y-3`) and
  `history_panel`, kept consistent.

### 1.2 Sticky stack (two-tier)
A two-tier sticky region keeps identity + actions on screen while the user scrolls the long feed —
critical for a trading tool (you act on a row 600px down, the part identity and action bar must
still be there).

- **Tier 0 — global topbar** (`topbar.html`, `sticky top-0 h-12 z-20`). Unchanged.
- **Tier 1 — dossier search bar** (`sticky top-12 z-20`): a thin `bg-white/95 backdrop-blur
  border-b border-brand-200` strip holding the persistent MPN input + "Search". Always present so a
  new PN is one keystroke away from anywhere on the page.
- **Tier 2 — condensed hero rail** (`sticky top-[6.5rem] z-10`): appears ONLY after the user scrolls
  past the full hero (Alpine `x-intersect`). A single-line condensed identity (`MPN · mfr · price
  range`) on the left and the **primary action cluster** (Send RFQ / Add Offer / Add to Req) on the
  right, on a `bg-white border-b` strip. This is the "it follows you down" affordance. On mobile it
  collapses to MPN + a single "Actions" button that opens the action sheet.

The full hero + full action bar scroll away normally; tier-2 is their sticky shadow. This avoids a
permanently fat header while never losing the act-on-a-row ability.

### 1.3 Section frame primitive
Every collapsible section uses one repeated frame:
```
<section class="rounded-xl bg-white border border-brand-200 shadow-sm">
  <header  class="flex items-center justify-between px-4 py-3 cursor-pointer select-none">
     <h2 ... > <icon> Title <span class="count-chip">N</span> </h2>
     <chevron rotates>
  </header>
  <div x-show=open x-collapse> ...body, px-4 pb-4... </div>
</section>
```
The market terminal is the one exception — its frame is `bg-gray-900 border-gray-800` (terminal).

---

## 2. IDENTITY HERO

The hero **renders instantly from the DB** (MaterialCard) before any connector runs. It is the
anchor of the dossier.

### 2.1 Composition (desktop)
A `bg-white border border-brand-200 rounded-xl shadow-sm p-5` card, laid out as a 3-zone flex:

**Zone A — Identity block (left, flex-1):**
- Line 1: the **MPN** in `font-mono text-2xl font-bold tracking-tight text-brand-900`, with a tiny
  click-to-copy ghost button (mono is the codebase signature for part numbers). This is the single
  largest element on the page — the part *is* the page.
- Line 2: a chip row, left→right priority: **manufacturer** (plain text, `font-medium`), then
  semantic chips in this order: **OEM-brand chip** (e.g. `Dell` — `bg-brand-100 text-brand-700`,
  the FRU/OEM crosswalk signal), **category** (`bg-blue-50 text-blue-700`), **lifecycle pill**
  (Active = `emerald`, NRND = `amber`, EOL/Obsolete = `rose`, Unknown = `gray` — semantic, matches
  existing lifecycle treatment), **condition** (`bg-gray-100 text-gray-600`, e.g. "New", "Refurb").
  Chips wrap; manufacturer + lifecycle never wrap below the fold.

**Zone B — Market price strip (center-right, on `lg:` a fixed `220px` column):**
- A compact **price-range readout**: big `font-mono` last/median price, a `min–max` range under it,
  and a 7-point **inline SVG sparkline** of `MaterialPriceSnapshot` history (`stroke-brand-400`,
  no axes — a glanceable trend, not a chart). Caption: "across N snapshots". This is the only chart
  on the page and it is deliberately tiny and dense.

**Zone C — Knowledge counts (right rail, `lg:` `160px`):**
- A 2×2 micro-grid of **count tiles**: Offers / Won / Sightings / Reqs (the `history_panel` summary
  stats), each `font-mono text-lg` over an `uppercase text-[10px] tracking-wide` label. Buyers shown
  as up-to-3 overlapping avatar initials with `+N`. These are *trust signals* — "we've traded this
  N times" — placed where the eye lands last before the action bar.

### 2.2 Unknown / new-part state
When MaterialCard is thin or absent (FTS/normalize miss), the hero degrades gracefully — it must
never look broken:
- Zone A: MPN renders as typed (still mono, still huge). Manufacturer line shows a single muted
  pill **"New to us"** (`bg-amber-50 text-amber-700 border border-amber-100`) — borrowing the
  `history_panel` "This part looks new to us" copy. If the FRU crosswalk *does* know it, swap to
  **"Known via FRU crosswalk"** (`brand` chip) — same logic the history panel already uses.
- Zone B: price strip shows a dashed-outline placeholder "No price history yet — run the market".
- Zone C: count tiles all show `0` in `text-gray-300` (present but visibly empty, not hidden — the
  zeros are information).
- A one-line nudge under the hero: *"We don't have history on this part — the live market below will
  tell us who has it."* This turns the empty state into a forward motion, not a dead end.

---

## 3. ACTION BAR

Sits directly under the hero (and is the cluster that re-docks in the tier-2 sticky rail).

### 3.1 Hierarchy & treatment
- **Primary — "Send RFQ":** solid `bg-brand-600 text-white hover:bg-brand-700`, paper-plane icon.
  This is the money action; it is the only filled button.
- **Secondary — "Add Offer", "Add to Requisition":** `bg-white border border-brand-300 text-brand-700
  hover:bg-brand-50`. Equal weight to each other, clearly subordinate to Send RFQ.
- **Overflow ("⋯" / "More"):** ghost icon button → Alpine dropdown with: *Refresh market*,
  *Copy MPN*, *Open full part page* (deep link to `/v2/materials/<id>`), *Add to shortlist view*.
- All actions sit on one `flex gap-2` row; on `lg:` right-aligned, with a left-side micro-caption
  showing where actions land: *"Actions create a quick-source requisition."* (sets the scratch-req
  expectation honestly, per locked decision #3).

### 3.2 The scratch-req moment (interaction)
Per locked decision #3/#4, no scratch req exists on a bare search. The first action:
1. Button is `hx-post`'d to its existing endpoint (`rfq_compose`, `add_offer_htmx`,
   `add_to_requisition`). Server-side `get_or_create_scratch_req` persists the current cached
   live-market rows as Sightings, then hands to the unchanged flow.
2. While in flight: button shows the existing `htmx-indicator` spinner; `data-loading-disable`
   dims it. No optimistic UI — we wait for the real fragment.
3. On success the existing modal/drawer for that flow opens (rfq composer, offer form). A toast
   (`$store.toast`) confirms: *"Quick-source req created · 6 sightings captured."* — making the
   invisible scaffold legible the first time.
- **"Add to Requisition" is the intentional path** and is visually distinguished: clicking it opens
  a small **typeahead popover** (`hx-get` open-req search) with a sticky footer **"+ New requisition…"**
  — NOT the scratch path. This matches locked decision #3.

### 3.3 Mobile
The action bar collapses to a **bottom action sheet**: a `sticky bottom-[52px]` (just above the
fixed nav) full-width `bg-white border-t border-brand-200` bar with **Send RFQ** filled + an
**"Actions"** trigger that slides up a sheet listing the rest. Thumb-reachable, never hidden by the
nav. The tier-2 condensed rail's "Actions" button drives the same sheet.

---

## 4. LIVE MARKET — the terminal

A `bg-gray-900 border border-gray-800 rounded-xl` slab — the embedded instrument. This is the
**reused engine** (`stream_search_mpn`, SSE, scoring, dedup) re-skinned into a row/table, not a
separate page. The `vendor_card.html` dark surface is the seed; we promote it into a tabular feed.

### 4.1 Terminal header
A `bg-gray-900` bar: title "**Live market**" in `text-gray-100` + a live **source-progress chip
row** (the existing `source-chip` system, verbatim — bright per-source neon: `source-chip--nexar`
violet, `--brokerbin` sky, etc., pulsing while searching, going solid on `ok`, dim on `empty`,
`rose` on `error`). Right side: a freshness stamp (*"cached 4m ago"* / *"live"*) and a
**"↻ Refresh market"** ghost button (`text-gray-400 hover:text-white border border-gray-700`) that
force-reruns the sweep.

### 4.2 Row design (the feed)
Each result is a **row**, not a fat card — denser than today's `vendor_card`, but built from the
same fields. Grid columns on `lg:` (`grid-cols-[auto_1fr_auto_auto_auto_auto]`):

| Col | Content | Treatment |
|---|---|---|
| Confidence | a 3px-wide vertical **confidence bar** + `NN%` | `emerald/amber/rose` keyed off `confidence_color` — the single fastest "is this real" read |
| Vendor | vendor name + `AUTH` chip + source badges (dark `source-badge--*`) | `text-white font-semibold`; badges per existing dark variants |
| Price | `$0.0000` | `font-mono text-white text-base`, the dominant numeric |
| Qty / MOQ | `1,250 avail · MOQ 100` | `font-mono text-gray-300` |
| Region/Lead | flag-ish `US · 8wks` | `text-gray-400 text-xs` |
| Actions | per-row quick actions (4.3) | right-aligned, reveal on `group-hover` |

- Rows stream in with the existing **`vendor-card` slideUp stagger** (`--i` custom prop, 50ms cascade)
  — reused verbatim, so the feed visibly *populates* like a ticker.
- Hover: `hover:bg-gray-800 border-l-2 border-l-transparent hover:border-l-brand-400` — a subtle
  left accent picks out the focused row.
- The **confidence bar** is the design's quiet star: a full-height left rule colored by confidence,
  turning a column of rows into a scannable quality gradient.

### 4.3 Per-row quick actions
On `group-hover` (always visible on mobile/touch), a right cluster of icon buttons:
- **RFQ this** (paper-plane) — single-vendor RFQ.
- **+ Offer** — opens offer form prefilled from this row (reuses `offer_form_modal` prefill path).
- **Details →** — opens the existing **lead-detail drawer** (`hx-get .../lead-detail` →
  `#lead-drawer-content`, `drawerOpen=true`). Drawer is reused verbatim.
- A **shortlist checkbox** stays at the row's far left (the existing `$store.shortlist` toggle),
  so multi-select bulk RFQ still works.

### 4.4 ALL live-market states
1. **Initial auto-run / streaming-in:** terminal header chips all pulsing; body shows **3 skeleton
   rows** (`bg-gray-800 animate-pulse` bars) immediately, replaced as real rows stream via SSE
   `sse-swap="results" beforeend`. A thin top progress shimmer on the terminal frame.
2. **Partial results:** rows already in; some chips still pulsing; a muted footer line *"3 of 8
   sources reporting…"* updates from `source-status` events.
3. **Done with results:** all chips resolved; footer becomes a stats line *"14 offers · 9 vendors ·
   best $0.84 · 4m"* (mirrors existing `#search-stats`).
4. **Empty ("no live stock"):** the existing empty card, re-skinned dark and reframed as a
   *pivot to knowledge*: headline **"No live stock right now."** + sub *"Here's what we already
   know about this part →"** with a button that scrolls to / expands the WHAT WE KNOW section. Never
   a dead end — the dossier still has value.
5. **Per-source error:** that source's chip goes `source-chip--error` (rose) with the operator
   message on `title` hover (existing behavior). A small **"2 sources errored"** rollup chip in the
   footer expands an inline list (source · message) — errors are visible but never block the rows
   that *did* come back.
6. **Refreshing:** the whole terminal body dims to `opacity-60` with a top shimmer; chips reset to
   pulsing; rows are replaced, not appended (force re-run). Freshness stamp flips to "live".

---

## 5. WHAT WE KNOW (history)

White section, the trading-memory record. Reuses `part_history_service.PartHistory`. Laid out for
**scannability**, not an accordion-of-everything:

- **Top strip:** the same summary stat row as `history_panel` (Offers / Won / Sightings / Reqs /
  Buyers) but rendered as the count tiles from the hero, so the vocabulary is consistent. Buyer
  chips below.
- **Price trend band:** a wider version of the hero sparkline + `min – max · last` readout
  (`MaterialPriceSnapshot`), full-width, `bg-brand-50` inset.
- **Sub-accordions** (Alpine `x-data="{open:'offers'}"`, one-open-at-a-time, `x-collapse` — exactly
  the existing `history_panel` pattern): **Offers**, **Confirmed / Won**, **Sightings**,
  **Requisitions**. Each shows top-5 rows (vendor · qty · price · status) with a "view all on part
  page →" deep link. Won rows use `text-emerald-600`; sightings carry their `source_type`.
- **FRU crosswalk card:** rendered ONLY on a crosswalk hit (forward or reverse), the compact card
  from `history_panel` — mono FRU chips, "Used in N FRUs", deep link to the full matrix. Kept as a
  visually distinct `bg-brand-50` inset so the crosswalk reads as *cross-part* knowledge, separate
  from this part's own trades.
- Empty: the existing "No prior history / This part looks new to us" card (or the FRU-aware variant).

The section is scannable because the **counts and price live at the top** (decide in 1 second whether
to dig), and the detail is one click down in named accordions.

---

## 6. SPECS & ENRICHMENT

White section, collapsed by default (it's reference, not decision-driving). Reuses MaterialCard
spec/enrichment fields + the F1 tier ladder's stored facets.

- **Spec grid:** a `dl` of `grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-2` — label (`text-gray-500
  text-xs uppercase`) over value (`text-gray-900`, mono for codes). Datasheet link as a pill button.
- **Provenance:** each enriched value carries a tiny **evidence-tier tag** (`evidence_tiers.py` —
  T1/T2 "Direct", T3/T4 "Marketplace", else "Indirect"), reusing the lead-detail tier treatment.
  This is a trading tool — operators must see *where a spec came from*.
- **"Enriching…" state:** when enrichment is queued/running, the section header shows an inline
  `animate-pulse` "enriching…" chip and the spec grid shows skeleton rows. On completion (HTMX
  poll / SSE), real values fade in. Honest: never show a guessed spec as settled.

---

## 7. Interactions: collapse, drawer, mobile

- **Collapse:** each section is `x-data="{open:true}"` (market + what-we-know open by default; specs
  closed). Header click toggles; chevron rotates `transition-transform`; body `x-collapse`. State is
  **persisted** via `$persist` so an operator's preferred open/closed layout survives navigation
  (matches the codebase's Alpine-persist convention). Deep-link `?mpn=` always opens market + history.
- **Lead-detail drawer:** the existing right-slide drawer (`form.html` `#lead-drawer`), reused
  verbatim — `translate-x-full → translate-x-0`, backdrop, body-scroll-lock, `@htmx:after-swap`
  opens it. Per-row "Details →" feeds `#lead-drawer-content`.
- **Mobile single-column:** everything stacks. Hero zones B/C drop below zone A. The market terminal
  becomes single-column **cards** (the row grid collapses) — falling back to the today's
  `vendor_card` layout, which is already mobile-correct. Sub-accordions stay. Action bar → bottom
  sheet (§3.3). Sticky tier-2 → MPN + Actions button.

---

## 8. Loading / skeleton model (instant-DB-then-live)

The whole point: **the dossier paints instantly from the DB, then the market lights up.**

1. **t=0 (server render):** hero, counts, price spark, what-we-know, specs all render server-side
   from MaterialCard + PartHistory in the *first* HTML response. No spinner on knowledge — it's
   already ours. The page is useful before any connector runs.
2. **t=0 (market):** terminal frame renders with all source chips pulsing + 3 skeleton rows. If a
   fresh Redis cache (<15m) exists, rows render immediately (no skeleton); else the SSE stream opens
   and rows cascade in.
3. **Section-level skeletons** (not full-page): only the *market* and the *enriching specs* ever show
   skeletons. Knowledge sections never flash a skeleton because they come from the synchronous DB
   read. This is the core "perceived-instant" trick — the user reads identity while the market loads.
4. **No layout shift:** skeleton rows reserve the market's height; the price spark reserves its box;
   count tiles render zeros not blanks. Content swaps in place, nothing jumps.

---

## Reuse ledger (what's verbatim vs. re-skinned)

| Reused verbatim | Re-skinned |
|---|---|
| `stream_search_mpn`, SSE wiring, scoring, dedup | `vendor_card.html` → market **row** (same fields) |
| `source-chip--*` progress system (CSS) | `results_shell` → terminal frame |
| `vendor-card` slideUp stagger (`--i`) | `form.html` empty state → forward-pivot empty |
| lead-detail drawer (`#lead-drawer`) | `history_panel` summary → hero count tiles |
| `history_panel` accordion + FRU card | — |
| `$store.shortlist`, `offer_form_modal` prefill | — |
| brand-* palette, mono-for-MPN, status semantics | — |

No new colors, no new fonts, no new framework. The dossier is the existing pieces, reframed into one
scrolling document with the paper/terminal seam as its identity.
