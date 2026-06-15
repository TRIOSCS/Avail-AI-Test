# Search → Part Dossier: Ruthless Simplification / YAGNI Critique

Date: 2026-06-15. Reviewer mandate: pressure-test the dossier design for over-engineering.
Value function: *the leanest design that still delivers full AVAIL functionality on a PN in a
clean, organized, useful page.*

**Headline finding:** the current "Search" page is **already a two-column dossier** — left
column streams the live market (SSE vendor cards + shortlist action bar), right column is the
"What we know" history panel. And there is a **second, richer dossier already shipped**: the
MaterialCard detail page (`materials/detail.html`, 256 lines) with a hero header, collapsible
Specifications, and tabs for sourcing / vendors / price-history / customers / FRU. The proposed
design re-derives ~80% of UI that exists. The real, genuinely-new work is small: make the page
**deep-linkable (GET ?mpn=)**, **re-stack the existing panels vertically**, and give one-off
search actions a **home requisition**. Everything else is re-skin or already-done.

Evidence (cite file:line):
- `app/templates/htmx/partials/search/results_shell.html:5,48-62` — already a
  `grid-cols-[1fr_360px]`: left = SSE live-market cards + `shortlist_bar.html`; right = lazy-loaded
  `search/history?mpn=` "What we know" panel. The "two-column dossier" exists today.
- `app/templates/htmx/partials/materials/detail.html:12-78` — hero header (MPN, manufacturer,
  lifecycle/RoHS pills, description, search_count, last_searched, **Enrich** button) +
  `:80-90` collapsible Specifications + tabs `sourcing/vendors/price_history/customers` +
  `fru_section`/`crosses_section`/`insights`. This is the IDENTITY HERO + SPECS & ENRICHMENT +
  WHAT WE KNOW sections of the proposed dossier, **already built and styled**.
- `app/routers/htmx_views.py:3483-3581` `add_to_requisition` — **already** finds-or-creates a
  Requirement for the MPN and persists shortlisted live-market results as `Sighting` rows, then
  runs `apply_to_fresh_sightings`. The spec's "persist cached results as Sightings on action" is
  this function, minus the scratch-req wrapper.
- `app/routers/htmx_views.py:2579-2617` `rfq_compose` — derives its vendor list from `Sighting`
  rows joined to `VendorCard`. **This is the load-bearing constraint**: RFQ shows vendors only if
  Sightings exist AND a matching VendorCard exists. So "persist as Sightings before RFQ" is
  *required*, not gold-plating — but it's already coded in `add_to_requisition`.
- `app/search_service.py:2010,2104-2105` `resolve_material_card` already does
  `search_count += 1; last_searched_at = now`. **Design decision #6 (light-footprint write) is
  already implemented.** No new code needed for the bare-search write path beyond a snapshot.
- `app/models/sourcing.py:43,47` — `Requisition.name` is `nullable=False`, `status` is a free
  `String(50)` with a **non-strict** validator (`:84-93` only logs a warning on unknown values).
- `app/routers/htmx_views.py:460-549` `requisitions_list_partial` — **no filter excludes any
  req**; a scratch req with no marker would appear in the requisitions list, the split-panel
  workspace, and the RFQ/add-offer pickers.

---

## 1. Is the `is_scratch` COLUMN justified? — **KEEP the column, but it's the only schema change you need.**

A scratch req MUST be hideable, because `requisitions_list_partial` (htmx_views.py:460) has no
exclusion filter — without a marker, every quick-source action litters the main requisitions list,
the split-panel workspace, and the typeahead picker. So you need *a* distinguishing field. The
options:

- **(A) Reuse `status="scratch"` (no migration).** `Requisition.status` is a free string with a
  lenient validator, so this "works" without DDL. **Reject it.** It conflates two orthogonal axes:
  lifecycle (draft→won→lost) vs. provenance (was-this-a-quick-search). `RequisitionStatus.TERMINAL`
  and every status filter/sort in the app assume status == lifecycle stage. A scratch req still
  needs a real lifecycle ("active") once an offer lands on it. Overloading status is a classic
  band-aid that the no-band-aids rule rejects, and it would force a `status != 'scratch'` guard
  into *every* requisition query forever.
- **(B) Add `is_scratch BOOLEAN DEFAULT false` (one migration).** Orthogonal to status, one
  `.filter(Requisition.is_scratch.is_(False))` in the list query, server_default makes it
  back-compatible. **Recommend this.** It's one column, one tiny migration, and it cleanly
  separates provenance from lifecycle. The cost is trivially low and the alternative pollutes
  every status query.

**Recommendation: KEEP `is_scratch`** (option B). It is the single justified schema change. Add the
`.filter(is_scratch == False)` to `requisitions_list_partial` in the same PR or scratch reqs leak
immediately. Do **not** also add a `RequisitionType` enum, a `scratch_expires_at`, or a cleanup job
— v1 creates a scratch req only on action (decision #4), so abandoned ones never exist.

## 2. Extracting `routers/search.py` — **CUT IT from this project. Do it as a separate, later, no-behavior-change PR (or not at all).**

`htmx_views.py` is 10,724 lines and the search routes are interleaved with materials, requisitions,
and shared helpers (`_base_ctx`, `templates`, `_get_enabled_sources`, `_safe_bg`, `score_unified`,
`classify_lead`, the `templates.get_template(...).render()` calls in `search_filter`). Extracting
cleanly means untangling those shared imports and re-registering a router — a non-trivial diff that
touches import graphs and risks the build, for **zero user-visible value**. It is textbook
scope-creep riding on a feature PR, and it makes the feature diff unreviewable (real changes hide in
move-noise). The no-band-aids rule cuts *both* ways: don't bundle unrelated refactors either
(`feedback_drift_bundling`: changed-files gate, don't bundle drift).

**Recommendation: OUT.** Build the dossier *in place* in `htmx_views.py`, next to the existing
search routes. If the file's size genuinely bothers you, file a separate pure-move PR afterward with
**no behavior change**, reviewed on its own. Minimal alternative if you want *some* hygiene now: put
only the *new* service (`get_or_create_scratch_req`) in `app/services/scratch_req_service.py` — a new
file, not a move — and leave the routes where they are.

## 3. The 4-phase plan — **too granular. Collapse to 2 PRs.** True MVP = one PR.

The proposed dossier is mostly re-stacking existing partials, so a 4-phase plan over-decomposes
work that is one coherent change.

- **PR 1 (the whole dossier, shippable, already "feels new"):** make `/v2/search` accept
  `GET ?mpn=<PN>` and deep-link (decision #1); render a single vertical scroll that **reuses the
  existing partials in a new order** — search bar; a hero that reuses the MaterialCard hero markup
  (or links to it); the existing `results_shell` live-market block; the existing `history_panel`
  "What we know"; the existing specs/enrichment. Wire the action bar to the **already-existing**
  `add_to_requisition` and the requisition picker. This is the minimum that already looks and feels
  like the dossier, and it requires *no* schema change because "Add to Requisition" works today.
- **PR 2 (the scratch-req "frictionless action home"):** add `is_scratch` + migration +
  `get_or_create_scratch_req`, wire the inline RFQ / add-offer / "send-without-picking-a-req"
  buttons to it, record a `MaterialPriceSnapshot` on market completion, add the
  `is_scratch` exclusion filter. This is the only part that touches the DB and the only part with
  real new service logic, so it earns its own PR and its own review.

**True minimum first shippable increment:** PR 1 alone. Deep-link + re-stack + reuse the existing
"Add to Requisition" path. It delivers the new dossier feel on day one with no migration and no new
service. Ship it, get feedback, then do the scratch-req convenience layer.

## 4. Per-feature: essential vs. gold-plating

| Feature | Verdict | Reasoning |
|---|---|---|
| **Deep-linking GET `?mpn=`** | **ESSENTIAL (v1)** | It's the spine of the whole "type a PN, get everything" vision and the one thing genuinely missing today (route is POST-only). Cheap: a GET handler that pre-fills + auto-runs. |
| **Vertical re-stack of existing panels** | **ESSENTIAL (v1)** | This *is* the redesign. It's a template re-order, not new logic. |
| **Reuse existing identity hero / specs / history** | **ESSENTIAL (v1)** | Already built; not reusing them is the only real waste risk. |
| **"Add to Requisition" (pick existing or named)** | **ESSENTIAL (v1)** | Works today via `add_to_requisition` + `requisition_picker`. Zero new code. |
| **Refresh-market button** | **ESSENTIAL (v1)** but trivial | It's just re-POSTing `search/run` for the MPN. One button. Keep. |
| **Collapsible sections** | **KEEP, but free** — copy the `x-data="{ open: true }"` pattern from `materials/detail.html:81`. No new mechanism. Don't over-engineer with persisted per-section state. |
| **Per-row quick actions (RFQ-this-vendor / log-offer)** | **DEFER to v2** | The shortlist bar (`shortlist_bar.html`) already gives batch "Add to Requisition" / "Create RFQ" on selected rows. Per-row single-vendor actions are a UX nicety that multiplies the action surface and forces the scratch-req-on-every-row path. Ship batch-from-shortlist first. |
| **Recent-searches landing (no-PN state)** | **DEFER to v2** | Nice, but it's a whole new query + empty-state design. v1 no-PN state = the existing search box + empty state (`form.html`), which is clean enough. MaterialCard has `search_count`/`last_searched_at` already if you want this later — cheap follow-up. |
| **MaterialPriceSnapshot on market completion (trend)** | **KEEP but DEFER to PR 2** | Useful, but it's a write inside the SSE-completion path. Bundle it with the scratch-req PR, not the re-skin PR. |
| **Scratch requisition** | **KEEP, PR 2** | It's the "frictionless home for one-off actions" — real value, but only needed the moment a user takes an action *without* picking a req. Not needed for the dossier to look right. |

## 5. "Persist cached results as Sightings on action" — **no simpler path exists, and it's already written.**

`rfq_compose` (htmx_views.py:2579) builds its vendor list *from Sighting rows joined to VendorCard*.
There is no shortcut: for RFQ to show vendors, Sightings must exist. The good news is the persistence
is **already implemented** in `add_to_requisition` (htmx_views.py:3536-3567), including
`apply_to_fresh_sightings` for unavailability re-application. So the scratch path is:
`get_or_create_scratch_req → reuse the existing Sighting-creation loop → hand to unchanged
rfq_compose`. Don't write a new persistence routine; **extract the existing Sighting-creation loop
into a small shared helper** and call it from both places.

**Hidden complexity to flag (resolve in PR 2 spec, no TBDs):**
1. `Sighting.requirement_id` is `nullable=False` (models/sourcing.py:184) — the scratch req MUST
   have its Requirement flushed (`.id` populated) before any Sighting is added. The existing code
   already does `db.flush()` after creating the Requirement; mirror that.
2. `rfq_compose` vendors require a **matching `VendorCard`** (`normalized_name.in_(...)`). Live-market
   results for brand-new brokers may have no VendorCard, so they'll silently not appear in the RFQ
   compose list even after you persist Sightings. Decide explicitly: either (a) accept that only
   known-vendor sightings are RFQ-able from a scratch search (matches today's behavior — recommend
   this), or (b) auto-create lightweight VendorCards. **Recommend (a)** for v1; (b) is a separate
   enrichment concern, not dossier work.
3. The Redis search cache is keyed by `search_id` (ephemeral UUID per run), not by MPN. The action
   must carry the live `search_id` so `_get_cached_search_results(search_id)` resolves; if the cache
   TTL (<15 min) lapsed, fall back to the shortlist items the client already holds in the Alpine
   `$store.shortlist` (which is how `add_to_requisition` works today — it posts `items` from the
   store, not from Redis). **Prefer the client-held items path; it has no TTL race.**

## 6. What the design duplicates that should be reused, not rebuilt

- **The MaterialCard detail page is already a dossier.** `materials/detail.html` + its tabs
  (`sourcing/vendors/price_history/customers`) + `fru_section`/`crosses_section`/`insights` cover
  IDENTITY HERO, SPECS & ENRICHMENT, and most of WHAT WE KNOW. **Do not rebuild these.** Either
  `{% include %}` the hero/specs partials into the dossier scroll, or render the dossier *as* the
  MaterialCard detail page with the live-market block injected. The cleanest lean move:
  **the dossier = MaterialCard detail page + a live-market section + an action bar**, deep-linked by
  MPN instead of card_id. That collapses the whole "build a new identity hero / specs panel" effort
  to zero.
- **The "What we know" panel already exists** (`search/history_panel.html`, driven by
  `part_history_service.get_part_history`, with FRU context). Reuse verbatim — it was last touched
  2026-06-10 and already carries offers/sightings/price-trend/FRU.
- **Sighting persistence + unavailability re-application** already exists in `add_to_requisition`
  (htmx_views.py:3536-3572). Extract-and-reuse; don't reimplement.
- **The bare-search light write** (`search_count`/`last_searched_at`) already exists in
  `resolve_material_card` (search_service.py:2104). Decision #6 is mostly done; only the
  price-snapshot is new.
- **The live-market engine** (SSE `search/run` → `stream_search_mpn` → `results_shell` →
  `vendor_card`/`lead_detail`/`search_filter`) is already wired end-to-end. Reuse verbatim
  (decision #5 already says this — good).

---

## CUT LIST (defer / drop)

1. **DROP** the `routers/search.py` extraction from this feature. Separate no-behavior-change PR
   later, or never. (scope-creep, build risk, unreviewable feature diff)
2. **DROP** the 4-phase plan → collapse to 2 PRs (re-skin+deep-link, then scratch-req+snapshot).
3. **DEFER** per-row quick actions (RFQ-this-vendor, log-offer-this-row) to v2 — batch-from-shortlist
   already exists and covers the need.
4. **DEFER** the recent-searches landing to v2 — v1 no-PN state is the existing clean empty state.
5. **DEFER** `MaterialPriceSnapshot`-on-completion into PR 2 (the scratch/write PR), not the re-skin.
6. **DROP** any net-new identity-hero / specs / history templates — reuse `materials/detail.html`
   partials and `search/history_panel.html`.
7. **DROP** any new Sighting-persistence routine — extract the existing loop from
   `add_to_requisition`.
8. **DROP** auto-creating VendorCards for unknown brokers in v1 (accept known-vendor-only RFQ).

## KEEP LIST (essential v1)

1. **`is_scratch BOOLEAN DEFAULT false`** + its **one** Alembic migration — the single justified
   schema change (status-overload is a rejected band-aid).
2. **`.filter(Requisition.is_scratch.is_(False))`** in `requisitions_list_partial` (ship with #1 or
   scratch reqs leak).
3. **Deep-linkable `GET /v2/search?mpn=`** with auto-run — the spine of the vision, the one truly
   missing capability.
4. **Vertical re-stack reusing existing partials** (MaterialCard hero/specs + `history_panel` +
   `results_shell` live market + collapsibles via the existing `x-data` pattern).
5. **Action bar wired to the existing `add_to_requisition` + requisition picker** (works today).
6. **`get_or_create_scratch_req`** as a small new service file (`services/scratch_req_service.py`),
   idempotent per (user, normalized mpn, open), called only on action.
7. **Reuse `rfq_compose`/`rfq_send`/`add_offer_htmx` unchanged**; feed them via the scratch req.
8. **Refresh-market button** (re-POST `search/run`).
9. **Client-held shortlist items** as the persistence source (no Redis-TTL race), mirroring today's
   `add_to_requisition`.

## Leanest correct v1 — one paragraph

Make `/v2/search` deep-linkable via `GET ?mpn=<PN>` and render the dossier as a single vertical
scroll that **reuses what already exists**: the MaterialCard detail page's hero + collapsible
Specifications + "What we know" history panel (`search/history_panel.html`) and the live-market SSE
block (`results_shell.html` → `stream_search_mpn` → `vendor_card`/`lead_detail`), with the existing
shortlist action bar wired to the **already-built** `add_to_requisition` flow — shipping this as PR 1
with **no schema change and no new service**, because everything it needs is coded today and only
needs re-ordering and a GET entry point. Then, and only then, ship PR 2: add the one justified column
`requisitions.is_scratch` (+ migration + a `.filter(is_scratch == False)` in the requisitions list),
a small idempotent `get_or_create_scratch_req` service, a `MaterialPriceSnapshot` on market
completion, and wire the inline "RFQ / add-offer without first picking a requisition" buttons to that
scratch req by extracting (not rewriting) the existing Sighting-creation loop and handing off to the
unchanged `rfq_compose`/`rfq_send`/`add_offer` — deferring per-row quick actions, a recent-searches
landing, and the `routers/search.py` extraction entirely.
