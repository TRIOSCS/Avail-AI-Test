# Sightings detail — Offers tab (Track A)

**Date:** 2026-06-05
**Status:** Approved (brainstorming) → planning
**Scope:** Track A of a two-track effort. Track B (bulk cross-requisition RFQ
composer + vendor suggestion / DB-pick / add-on-the-fly) is **out of scope here**
and gets its own spec.

---

## 1. Goal

On the sightings detail pane (the right-hand pane of `/v2/partials/sightings`),
give buyers first-class access to **offers** for the part they are looking at:

1. A new **Offers** tab beside Vendors and Activity.
2. The tab lists **every offer for the part number** (part-centric, *not*
   requirement-scoped) — including offers entered against **substitute MPNs**.
3. A **Convert to offer** action on each vendor row (next to Send RFQ / Mark
   Unavail) that opens a pre-filled offer form.
4. A generic **Enter offer** action on the Offers tab for an offer not tied to
   any sighting.
5. Entered/converted offers appear in the tab immediately and are logged to the
   Activity timeline (existing `OFFER_CREATED` behaviour).

This reuses the existing `Offer` model, offer-creation logic, offer mutation
endpoints, the shared activity timeline, and the global modal mechanism. It does
**not** introduce a parallel offer code path.

---

## 2. Resolved decisions (no TBDs)

| Decision | Resolution |
|---|---|
| Offers shown | **Part-centric**: all offers whose normalized MPN ∈ {requirement primary MPN} ∪ {each substitute MPN}. Requirement/requisition of origin does **not** filter the list. |
| Substitutes | **Included** in the part's offer set. |
| Source hint | **Keep** a per-row `↳ <customer> · Req #<id>` line (context only; does not filter). |
| Convert-to-offer UX | **Pre-filled modal form** (review then save), not one-click. |
| Form presentation | **Modal overlay** (same mechanism as Send-RFQ modal). One shared form used by Convert + Enter-offer. |
| Pending-review block | **Moved** out of the Vendors panel into the Offers tab (single home for offers). |
| Offer status on manual/convert create | `active` (matches existing manual-entry path). |
| DRY | Offer-form field grid extracted to a shared partial; create + mutations routed through the existing offer service/handler, not duplicated. |

---

## 3. Tab structure

`sightings/detail.html` tab nav changes from **Vendors · Activity** to
**Vendors · Offers · Activity**. Alpine `activeTab` still defaults to `'vendors'`.
The Offers panel (`x-show="activeTab === 'offers'"`) hosts a container
`#sightings-offers-panel` whose inner HTML is `sightings/offers_panel.html`.

The existing "Pending Review (Approve/Reject)" block currently rendered inside
the Vendors panel is **removed from there** and rendered inside the Offers panel
(pending offers are just offers with `status == pending_review`, shown with their
Approve/Reject actions in-line via the row kebab).

---

## 4. Part-centric offer query

Given the open requirement `r`:

```
parts   = [r.primary_mpn] + sub_mpns(r)                       # display strings
keys    = { normalize_mpn_key(p) for p in parts } ∪ { normalize_mpn(p) for p in parts }   # both forms
cards   = { MaterialCard.id where normalized_mpn == normalize_mpn_key(p) }  for each p
offers  = Offer where ( Offer.material_card_id IN cards
                        OR Offer.normalized_mpn IN keys )
          order by created_at desc
          joinedload(requisition)   # for the source hint
```

- **Why both `material_card_id` and `normalized_mpn`:** the two existing creation
  paths write `Offer.normalized_mpn` differently — `create_offer` uses
  `normalize_mpn_key` (dash-stripped, e.g. `lm2596s50`), while the requisitions
  `add_offer` HTMX handler uses `normalize_mpn` (display form, keeps dashes). So a
  single-form match misses offers from the other path. Matching on
  `material_card_id` **OR** `normalized_mpn ∈ {both forms}` captures every offer.
- **Root-cause fix (in scope):** correct `add_offer` (`htmx_views.py`) to write
  `normalized_mpn = normalize_mpn_key(mpn)` so new offers are consistent. The
  dual-form query still covers any legacy rows.
- Substitute MPNs come from the same source the `|sub_mpns` filter uses
  (`parse_substitute_mpns`); `None`/<3-char normalized results dropped.
- Encapsulate this in a helper `part_offers_for(requirement, db) -> list[Offer]`
  in the sightings router (or a small `app/services/` helper) used by both the
  detail view and the panel re-render.
- Status visibility: show **all** statuses (the status pill differentiates).
  `pending_review` offers are shown with Approve/Reject (this is the consolidated
  replacement for the old Vendors-panel pending block). This matches the existing
  sightings behaviour of surfacing pending offers for action.

The view passes `part_offers` (list) to the template. Each row needs:
`vendor_name`, `unit_price`, `qty_available`, `lead_time`, `status`,
`requisition.customer_name`, `requisition_id`, `id`.

---

## 5. Templates (new / changed)

- **`sightings/detail.html`** (changed): add Offers tab button + panel; move the
  pending block; add `#sightings-offers-panel` include.
- **`sightings/offers_panel.html`** (new): `[+ Enter Offer]` button, "All offers
  for `<MPN>` · N" heading, list of `_offer_row.html`, empty state.
- **`sightings/_offer_row.html`** (new): compact row — vendor, `$price · qty ·
  lead`, status pill, `↳ customer · Req #`, kebab actions (Edit, Approve/Reject
  for pending, Reconfirm, Mark Sold, Delete) that re-render `#sightings-offers-panel`.
- **`sightings/_vendor_row.html`** (changed): add **Convert to offer** as a third
  button in the existing line-2 action group, inside the same
  `vs != 'blacklisted' and vs != 'unavailable'` guard. Opens the modal via
  `$dispatch('open-modal', {url: '/v2/partials/sightings/<rid>/offer-form?...prefill'})`.
- **`offers/_offer_form_fields.html`** (new, shared): the field grid only (vendor,
  mpn, qty, price, mfr, lead time, date code, condition, moq, spq, packaging,
  firmware, hardware code, warranty, country, valid until, notes). Included by:
  the new sightings modal wrapper, **and** refactored into the existing
  `requisitions/add_offer_form.html` so both share one field source of truth.
- **`sightings/offer_form_modal.html`** (new): modal wrapper around
  `_offer_form_fields.html`; title "Convert to Offer — <vendor>" or "Enter Offer";
  posts to the sightings create endpoint; on success closes modal + toast +
  refreshes `#sightings-offers-panel`.

> Field-grid extraction must preserve the exact existing field names/markup the
> requisitions form already posts; the requisitions form keeps working unchanged.

---

## 6. Endpoints (new, on the sightings router)

All under the sightings router prefix (paths confirmed against the router in the
plan step):

- `GET  /v2/partials/sightings/{requirement_id}/offers`
  → render `offers_panel.html` (the part-centric list). Used for the tab body and
  post-action refresh (target `#sightings-offers-panel`).
- `GET  /v2/partials/sightings/{requirement_id}/offer-form`
  → render `offer_form_modal.html`. Query params prefill it:
  - Convert: `?vendor=<name>&unit_price=&qty=&lead_time=&moq=&manufacturer=` taken
    from the `VendorSightingSummary` row; `mpn` defaults to the part's primary MPN.
  - Enter (blank): no prefill except `mpn` = part's primary MPN (editable).
- `POST /v2/partials/sightings/{requirement_id}/offers`
  → build an `OfferCreate` from the form and **call the canonical
  `create_offer(...)` function** (`app/routers/crm/offers.py`) directly — it already
  does vendor-card + material-card resolution, sets the dedup-correct
  `normalized_mpn`, logs `OFFER_CREATED`, and auto-progresses requisition +
  requirement status. We reuse it rather than re-implementing (the requisitions
  `add_offer` HTMX handler is the existing *duplicate*; we deliberately do **not**
  extend that duplication). `requirement_id` + `requisition_id` are set so the
  offer has a home; it surfaces for the part everywhere. Then return the refreshed
  `offers_panel.html` swapped into `#sightings-offers-panel`; the modal form closes
  itself client-side via `hx-on::after-request` → `$dispatch('close-modal')`, and an
  OOB toast div reports success.

Offer **mutations** (approve / reject / reconfirm / mark-sold / edit / delete): add
**thin sightings-scoped HTMX endpoints** that call the existing canonical
offer-operation functions in `app/routers/crm/offers.py` directly (`approve_offer`,
`reject_offer`, `reconfirm_offer`, `mark_sold`, `update_offer`, `delete_offer`) and
then re-render `offers_panel.html` into `#sightings-offers-panel`. No logic is
copied — we call the same functions the API uses. We do **not** parameterise the
requisitions HTMX endpoints (they target `#tab-content`); separate thin wrappers
avoid regressing the requisitions Offers tab.

> Why call router functions directly: they are plain `async def`s taking
> `(…, user, db)` (and a Pydantic body where relevant). Invoking them in-process
> reuses 100% of their behaviour with zero duplication and zero refactor blast
> radius. Their JSON return value is ignored; the sightings endpoint re-renders the
> panel for the HTMX swap.

---

## 7. Convert-to-offer field mapping (from `VendorSightingSummary` `s`)

| Offer field | Source |
|---|---|
| vendor_name | `s.vendor_name` |
| mpn | requirement primary MPN |
| manufacturer | `requirement.manufacturer` |
| unit_price | `s.best_price` |
| qty_available | `s.estimated_qty` |
| lead_time | `s.best_lead_time_days` → `"<n> days"` if set |
| moq | `s.min_moq` |
| status | `active` |
| requirement_id / requisition_id | the open requirement's |

Buyer edits/fills the remaining commercial fields (date code, condition,
packaging, etc.) before saving.

---

## 8. Activity

No new event type. Offer creation already logs `ActivityType.OFFER_CREATED`; that
shows in the Activity tab automatically. Convert and Enter both go through the
same creation path, so both are logged.

---

## 9. Testing

Render / endpoint (pytest, SQLite test client):
- Offers tab renders; tab nav has Vendors/Offers/Activity (extend
  `test_renders_tab_structure`).
- `GET .../offers` lists an offer entered on **a different requisition** for the
  same MPN (part-centric proof) **and** an offer entered against a **substitute**
  MPN (substitute inclusion proof).
- Source hint (`customer` / `Req #`) present in a row.
- `GET .../offer-form?vendor=...` prefills vendor + price (Convert) and blank +
  MPN-prefilled (Enter).
- `POST .../offers` creates an offer; it appears in the refreshed panel; an
  `OFFER_CREATED` activity row exists; requirement status auto-progresses as on the
  requisitions path.
- Convert button present on the vendor row's collapsed action group, before
  `x-show="expanded"` (extends the Track-0 assertion).
- Pending-review offer renders in the Offers panel with Approve/Reject and **not**
  in the Vendors panel.

Service:
- Part-centric query returns cross-requisition + substitute offers and excludes
  unrelated MPNs.
- Convert field-mapping builds the expected create payload.

Run `pre-commit run --files <changed>` and the sightings test modules before PR.

---

## 10. Edge cases

- Part with no MaterialCard → query still works via `normalized_mpn`.
- No offers → empty state ("No offers yet for this part").
- Blacklisted/unavailable vendor row → no Convert button (shares existing guard).
- Re-render after action keeps the user on the Offers tab (panel-scoped swap, not
  full-detail re-render which would reset `activeTab`).
- Modal `@click.stop` on the vendor-row Convert button so opening it doesn't toggle
  the row's expand state.

---

## 11. Out of scope (Track B)

Bulk cross-requisition RFQ composer; ranking sighting vendors by part coverage;
"Suggest a vendor" (affinity, no sighting); pick any DB vendor; add vendor on the
fly; cross-requisition RFQ tracking. Separate spec.
