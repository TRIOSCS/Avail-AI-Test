# Offer Qualification Capture — Design Spec

**Date:** 2026-06-17
**Status:** Design (pending user review)
**Topic:** Standardized buyer qualification capture at sighting → offer conversion
**Author:** brainstormed with mhk

---

## 1. Problem & North Star

When a buyer converts a sighting to an offer, we want them capturing condition,
packaging, and provenance **in a standardized way** — derived from the "Working an RFQ —
Buyer Checklist" — **without** adding field-noise or turning the convert step into a
workflow speedbump.

The resolving principle is **"confirm, don't compose."** The buyer's job at conversion is
to *confirm or correct* values that are pre-filled, inferred, or chip-selected — never to
type prose or re-enter what the system already knows. Standardized capture becomes a
**side-effect of confirmation**, not extra work.

### Jobs the captured data must do (all four, per user)
1. **Trustworthy note for the next human** — a consistent, complete note the next buyer / a
   customer-facing quote can rely on.
2. **Filter / report** — query offers by condition, qualification status, country, etc.
3. **Force buyer rigor at the gate** — the buyer must actually qualify; but *never a blockade*.
4. **Feed RFQ / vendor follow-up** — captured gaps drive the next message to the vendor.

### Explicitly OUT of scope (declined / deferred)
- **Checklist Section 1 — Market Intelligence** (FPQ, franchise pricing, franchise stock):
  these are *part-level* facts, not per-offer typing. Not captured as fields here.
- **Checklist Section 4 — Price sanity / "below market" auto-flag** and the read-only
  **market-baseline strip** (was idea #6): declined. No market-engine dependency in this
  feature. Price judgment stays in the buyer's head / free notes.
- **Full image-upload redesign**: not needed — the existing `OfferAttachment` (OneDrive via
  Graph) pipeline already stores images. "Images received" = the offer has ≥1 attachment.
- **Promoting JSON facets to typed columns**: deferred until a facet proves hot.

---

## 2. Current State (grounding)

- **Convert-to-offer modal:** `app/templates/htmx/partials/sightings/offer_form_modal.html`
  includes the shared field grid `app/templates/htmx/partials/offers/_offer_form_fields.html`
  (also used by `requisitions/add_offer_form.html`). Today the grid renders **17 flat fields
  always visible**: vendor, mpn, qty, price, manufacturer, lead_time, date_code, condition,
  moq, spq, packaging, firmware, hardware_code, warranty, country_of_origin, valid_until, notes.
- **`condition` taxonomy today:** `new` / `refurb` / `used` — does NOT match how the buyer
  thinks (New / New-no-mfr-pkg / Pulls / Refurbs).
- **Offer model** (`app/models/offers.py`): already has `condition`, `packaging`,
  `country_of_origin`, `notes`, `attachments` (→ `OfferAttachment`, OneDrive + `thumbnail_url`).
- **Vendor follow-up infra:** `Contact` model (graph message/conversation id, status) +
  existing solicit/RFQ flow — reused for idea #7.
- **POST/PUT offer handlers** live under the sightings router (`/v2/partials/sightings/{req}/offers`)
  and the requisitions offer router; both read form fields by `name=`.

---

## 3. Data Model — Approach A (reuse + 3 columns + JSON)

Reuse existing columns; add **exactly three** new columns on `offers`. All condition-specific
detail goes into one JSON blob (matches the existing `Contact.parse_result_json` pattern), so
the schema does not churn as the checklist evolves.

| New column | Type | Purpose |
|---|---|---|
| `qualification_status` | `String(20)`, indexed | `unset` / `incomplete` / `essentials` / `complete` — drives badge + filter/report (job #2). |
| `qualification_note` | `Text` | System-composed **standardized note** (Section 5 wording). Distinct from free `notes`. Never buyer-edited. |
| `qualification` | `JSON` | Condition-specific detail + pending vendor requests (shape below). |

Reused as-is: `condition` (taxonomy expanded), `packaging`, `country_of_origin`, `notes`,
`attachments`.

### 3.1 `qualification` JSON shape (versioned)
```json
{
  "schema": 1,
  "usage": "boards | systems | null",            // Pulls
  "refurbished_by": "supplier | third_party | null",  // Refurbs
  "refurb_process": "string | null",             // Refurbs
  "cert_doc": "yes | no | null",                 // Refurbs, only when refurbished_by = third_party
  "part_condition": "string | null",             // Pulls (optional new_no_pkg)
  "provenance_story": "string | null",           // optional, all conditions
  "terms": "string | null",                      // optional, all conditions (e.g. "delivers to test house, pay after inspection")
  "lead_time_reason": "string | null",           // optional; field shown whenever lead_time is non-empty
  "requests": [                                   // idea #7 — pending vendor requests
    {"kind": "images|fpq|cert|pkg_qty", "status": "pending|sent|received",
     "requested_at": "<iso8601>", "contact_id": null}
  ]
}
```
Filterability (job #2): PostgreSQL JSONB supports `qualification->>'usage'` etc. Typed
columns are reserved for the two things actually faceted today — `qualification_status` and
`condition`. Promote a JSON key to a column only when a facet proves hot.

### 3.2 Condition taxonomy + migration
New `condition` values: **`new`** (mfr packaging) · **`new_no_pkg`** · **`pulls`** · **`refurb`**.

- Migration maps legacy `used → pulls`; legacy `refurb` unchanged; legacy `new` unchanged.
- Alembic migration adds the 3 columns + the `used → pulls` data update.
  - Revision id ≤ 32 chars (PG `alembic_version` is `VARCHAR(32)`).
  - Reserve the migration number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` (cross-session coord).
  - Include a working `downgrade` (drop columns; `pulls → used` is **not** reversed — note in docstring).

---

## 4. Condition is the Spine — Capture Matrix

Only the chosen condition's panel renders (Alpine `x-show`). Chips, not free text. **Bold =
hard-block essential** (server-validated; usually one tap, often pre-filled from the sighting).
Everything else feeds the **meter only** (soft, never blocks).

| Condition | Hard-required (blocks save) | Recommended (meter only) | Standardized auto-note |
|---|---|---|---|
| **New (mfr pkg)** `new` | `manufacturer` present | package type, date code | `New — parts are in the original manufacturer's packaging.` |
| **New (no mfr pkg)** `new_no_pkg` | **`packaging`** (chip, not "bulk") | images, date code | `New, no original manufacturer packaging. Packaged in {packaging}.` |
| **Pulls** `pulls` | **`packaging`** (chip) + **`usage`** (boards/systems) | images, part-condition | `Pulls — packaged in {packaging}, pulled from {usage}.[ Condition: {part_condition}.]` |
| **Refurbs** `refurb` | **`refurbished_by`** (supplier/3rd-party) + **`refurb_process`** | cert-doc (only if 3rd-party), images | `Refurbished by {who}.[ Process: {refurb_process}.][ {cert clause}]` |

**Cross-condition (all optional / meter only):**
- `country_of_origin` — existing column; auto-filled by idea #8.
- `terms` — free note (e.g. "delivers to test house, pay after inspection"); auto-filled by idea #8.
- `lead_time_reason` — field shown whenever `lead_time` is non-empty (deterministic; no
  free-text "> 1 week" parsing). The label hint reminds the buyer to explain long lead times.
- `provenance_story` — free note.

### 4.1 Chip vocabularies (exact allowed values)
- **packaging** (`new_no_pkg`, `pulls`): `Tape & Reel`, `Reels`, `Trays`, `Tubes`,
  `Antistatic bags`, `Boxes`. (`new` mfr-pkg: free, optional.) — "bulk"/"loose" are **not**
  selectable and are server-rejected for `new_no_pkg`/`pulls`.
- **usage** (`pulls`): `boards` (Pulled from boards) · `systems` (Pulled from systems).
- **refurbished_by** (`refurb`): `supplier` · `third_party`.
- **cert_doc** (`refurb`, only when `third_party`): `yes` · `no`.

### 4.2 Standardized note composition (deterministic, server-authoritative)
Composed by `compose_note()` (Section 6) and stored to `qualification_note`. Client renders an
identical **read-only live preview** — buyer never types it. Section 5 of the checklist is the
contract for wording. Substitutions:
- `{usage}`: `boards → "boards"`, `systems → "systems"`.
- `{who}`: `supplier → "the supplier"`, `third_party → "a third party"`.
- cert clause (3rd-party only): `cert_doc=yes → "Certifying document on file."`,
  `cert_doc=no → "No certifying document."`, `null → ""` (omit).
- Bracketed `[…]` clauses are emitted only when their source value is present.

The standardized note stays **condition-only** (matches Section 5). `provenance_story`,
`lead_time_reason`, and pending requests render as separate detail lines on the offer, not
inside the standardized note. Free `notes` (existing textarea) is untouched and additive.

---

## 5. Rigor Without Blockade (the gate)

- **Server is the source of truth.** On POST/PUT offer, `validate_essentials(condition, form)`
  rejects only when a **bold essential** for the chosen condition is missing, or `packaging`
  is empty/"bulk"/non-chip for `new_no_pkg`/`pulls`. Rejection re-renders the form with a
  precise inline error (HTMX swap), never a dead-end.
- **Why this is not a speedbump:** essentials arrive pre-filled from the sighting and are
  one-tap chips, so a clean offer saves with zero stops. The block only fires when the buyer
  genuinely hasn't stated the bare minimum.
- **Soft layer (everything below the essentials):** a **"Qualified X / Y"** meter in the form
  and an **"incomplete" badge** on the offer row + detail, read by filters/reports (job #2).
- **No condition chosen** → `qualification_status = unset` (allowed to save; badge "unqualified").
- `incomplete` only arises for pre-existing/legacy offers or API writes that bypass the UI —
  the UI cannot produce a new `incomplete` offer because essentials are hard-blocked.

### 5.1 Status + meter computation (`compute_status`, `meter`)
Per condition, `essentials` and `recommended` item sets (an item is "filled" per rules below):
- **new:** essentials `{manufacturer}`; recommended `{package_type, date_code}` → Y=3.
- **new_no_pkg:** essentials `{packaging}`; recommended `{images, date_code}` → Y=3.
- **pulls:** essentials `{packaging, usage}`; recommended `{images, part_condition}` → Y=4.
- **refurb:** essentials `{refurbished_by, refurb_process}`; recommended `{images}` + `{cert_doc}`
  *only when* `refurbished_by=third_party` → Y=3 (supplier) or 4 (third_party).

"images" filled ⇔ offer has ≥1 `OfferAttachment` (a *pending* request does not satisfy it).
`qualification_status`: `complete` (all essentials + all recommended) / `essentials` (all
essentials, ≥1 recommended missing) / `incomplete` (an essential missing) / `unset` (no condition).

---

## 6. Service Layer — `app/services/offer_qualification.py`

Pure, independently testable functions (routers stay thin per CLAUDE.md). Used by **both** the
sightings offer handler and the requisitions offer handler so behavior is identical everywhere:

- `validate_essentials(condition, form) -> list[error]`
- `compose_note(condition, data) -> str`
- `compute_status(condition, data, has_images: bool) -> str`
- `meter(condition, data, has_images: bool) -> tuple[int, int]`  # (filled, total)
- `prefill_from_vendor(db, vendor_name_normalized) -> dict`       # idea #8
- `request_template(kind, mpn) -> str`                            # idea #7 RFQ-back draft text

---

## 7. Idea #7 — One-tap Vendor Requests

For things the buyer doesn't yet know (images, FPQ confirmation, 3rd-party cert, package-qty),
a chip turns the unknown into an **action**, not a blank field.

- **Endpoint:** `POST /v2/partials/sightings/{req}/offers/{offer}/request` body `kind=images|fpq|cert|pkg_qty`.
- Appends `{kind, status:"pending", requested_at}` to `qualification.requests`, re-renders the
  offer's request chips (HTMX swap), and returns a **draft RFQ-back line** from
  `request_template(kind, mpn)`, e.g.:
  - `images` → "Please provide images of all angles, markings, contact points, and packaging for {mpn}."
  - `fpq` → "Please confirm the factory package quantity (FPQ) for {mpn}."
  - `cert` → "Please provide the third-party refurbishment certification document for {mpn}."
  - `pkg_qty` → "Please confirm the package quantity and how the parts are packaged for {mpn}."
- **Sending** reuses the existing solicit/RFQ flow (creates a `Contact`); on send the request's
  `status → sent` and `contact_id` is linked. v1 logs `pending` + drafts; the actual send is the
  existing flow (no new mail path).

---

## 8. Idea #8 — Remember Vendor-Stable Answers

On opening the convert-to-offer modal, `prefill_from_vendor(db, vendor_name_normalized)` reads
this vendor's **most-recent offer** and pre-fills *only empty* fields: `country_of_origin`,
`qualification.refurbished_by`, and a `terms` line if present. The buyer can override any of it.
Repeat vendors ≈ zero typing. No new table — a scoped query on `offers` by
`vendor_name_normalized` ordered by `created_at desc`.

---

## 9. UI Changes (needs explicit sign-off — rearranges existing form)

`_offer_form_fields.html` reorganized so it feels **lighter than today**, not heavier:
1. **Always-shown core line:** vendor, mpn, qty, unit price.
2. **Condition select** (the spine).
3. **Condition-gated panel** (`x-show`) with that condition's chips + the read-only
   standardized-note preview + the "Qualified X/Y" meter.
4. **Collapsible "More details"** holding the remaining existing optional fields
   (manufacturer, lead_time, date_code, country_of_origin, valid_until, moq, spq, firmware,
   hardware_code, warranty) — so the current 17-field wall is tamed, not extended.

- Alpine logic in an `Alpine.data('offerQualification', …)` **factory** with **single-quoted**
  attributes — avoids the `tojson`/double-quote-attr init bug documented in CLAUDE.md.
- `_offer_row.html`: qualification badge (`Qualified` / `Essentials` / `Incomplete` /
  `Unqualified`) + mini meter.
- Offer detail: standardized note + provenance/story + lead-time reason + pending requests.
- All three offer entry points (sightings modal, requisitions add, requisitions edit) share the
  reorganized `_offer_form_fields.html`, so the feature is uniform.

---

## 10. Routes Touched
- `POST /v2/partials/sightings/{req}/offers` and `PUT …/{offer}` — read new fields, call
  `validate_essentials`, `compose_note`, `compute_status`; persist.
- Requisitions add/edit offer handlers — same shared validation/compose path.
- **New:** `POST …/offers/{offer}/request` (idea #7).
- Modal-open handler — call `prefill_from_vendor` (idea #8).

---

## 11. Testing

- **Unit (`tests/test_offer_qualification.py`):** `compose_note` for all 4 conditions + every
  bracketed-clause variant; `validate_essentials` block cases incl. packaging="bulk" rejection
  for `new_no_pkg`/`pulls`; `compute_status` + `meter` per condition incl. third-party cert
  branch; `prefill_from_vendor`; `request_template`.
- **Integration:** POST offer per condition → essential-missing returns 422 + inline error;
  valid → `qualification_note` composed + `qualification_status` set; `used`-legacy reads as
  `pulls`; #7 endpoint logs `pending` + returns draft; #8 prefill on modal open.
- **Template / console (e2e session-cookie harness, `tests/e2e/conftest.py`):** load sightings
  offer modal, switch condition → correct panel shows, note preview + meter update live, **no
  Alpine console errors / no broken init**.
- **Live PG verify after deploy** (SQLite masks PG; JSON/JSONB + data migration are PG-specific).

---

## 12. Docs to Update (same PR)
- `docs/APP_MAP_DATABASE.md` — 3 new `offers` columns + `qualification` JSON shape + taxonomy.
- `docs/APP_MAP_INTERACTIONS.md` — `offer_qualification` service, the gate, #7 request flow, #8 prefill.
- `docs/APP_MAP_ARCHITECTURE.md` — new service file `app/services/offer_qualification.py`.

---

## 13. Build Order (for the implementation plan)
1. Migration (3 columns + `used → pulls`) + model fields + taxonomy constants.
2. `offer_qualification.py` service + unit tests (TDD).
3. Wire both offer POST/PUT handlers to the service (validate / compose / status).
4. Reorganized `_offer_form_fields.html` + Alpine factory + note preview + meter.
5. Offer row/detail badge + standardized-note display.
6. #8 prefill on modal open.
7. #7 request endpoint + chips + RFQ-back draft.
8. e2e console check + live-PG verify + APP_MAP docs.
