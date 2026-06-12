# Bulk Cross-Requisition RFQ Composer (Track B) — Design

**Date:** 2026-06-12
**Status:** Approved feature (user selected Track B from the next-build options; this spec
resolves the design). Track A (Offers tab) and durable unavailability (PR #270) are shipped.
**Origin:** deferred as "separate spec" in `2026-06-05-sightings-offers-tab-design.md` §11.

## Problem

The sightings vendor modal already composes/previews/sends a multi-part RFQ
(`vendor_modal.html`, `sightings_vendor_modal` / `preview-inquiry` / `send-inquiry`),
invoked from the table's multi-select. But:

1. **Cross-requisition tracking is broken (load-bearing bug):** `send-inquiry` collapses
   the selected requirements' requisitions to ONE via
   `requisition_id = next(iter(requisition_ids))` (undefined set order) and passes it to
   `send_batch_rfq`, which writes every `Contact` row, every `[ref:{requisition_id}]`
   subject token, and reply-matching state against that single requisition. Outreach
   spanning requisitions A+B+C records history only on one of them — silently.
2. **Vendor selection is dumb:** a flat engagement-ordered top-20 of vendors that have
   any sighting on the selected parts. No part-coverage ranking, no way to reach a vendor
   with no sighting (affinity), no picking an arbitrary DB vendor, no adding a new vendor.

## Decision summary

Fix tracking at the root (per-requisition `Contact` rows sharing one email), and upgrade
the modal's vendor panel: coverage-ranked suggestions, affinity suggestions on demand,
any-vendor autocomplete, inline vendor creation. Reuse the existing compose→preview→send
skeleton, `send_batch_rfq`, and the unavailability exclusion. **No new tables; no
migration** (Contact keeps its singular `requisition_id` — multiplicity moves to rows).

## Part 1 — Cross-requisition tracking fix

**Semantics: one email per vendor covering ALL selected parts** (that is the point of a
bulk composer), with full per-requisition tracking:

- `sightings_send_inquiry` stops collapsing requisitions. It groups the selected
  requirements per requisition and passes the full mapping down.
- `send_batch_rfq` (or a thin evolution of it — keep ONE canonical send path) sends one
  Graph email per vendor, then writes **one `Contact` row per (requisition, vendor)
  pair**, all sharing that email's `graph_message_id` / `graph_conversation_id`. Each
  Contact's `parts_included` holds only that requisition's parts.
- **Subject ref tokens:** the subject carries one `[ref:{id}]` token per involved
  requisition (e.g. `RFQ — 6 parts [ref:12] [ref:34]`). The implementation MUST first
  read the inbox-monitor/reply-matching parser: if it `search()`es a single token,
  multiple tokens are fine; if it strictly anchors, extend the parser to find all tokens
  and attribute the reply to every matching Contact (root-cause, not a workaround).
  Whichever the parser needs, replies must attribute to ALL involved requisitions.
- Activity logging already iterates per requirement (`log_rfq_activity(rfq_id=
  r.requisition_id, requirement_id=r.id)`) — keep, now consistent with Contacts.
- Status auto-progress (OPEN→SOURCING) applies per involved requisition, not just one.
- `X-RFQ-*` headers unchanged in meaning (per-vendor counts).

## Part 2 — Vendor panel upgrade (`vendor_modal.html` + `sightings_vendor_modal`)

The modal's vendor list becomes four sections in one selectable checklist:

1. **Suggested (coverage-ranked).** One grouped query over `VendorSightingSummary`
   filtered to the selected requirement ids: per vendor — covered-part count, avg
   summary score, engagement score. Order: coverage desc, then engagement desc; keep the
   20-row cap. Each row shows `N/M parts` (M = selected part count) plus the existing
   response-rate/engagement badges; `title` lists the covered MPNs (computed in the
   route, template stays dumb). Vendors in `excluded_vendor_norms` stay excluded from
   this list (as today).
2. **Affinity (on demand).** A "Suggest more vendors" button (`hx-get`, htmx partial
   swap into the section — explicit `hx-target`) calls a new
   `GET /v2/partials/sightings/vendor-affinity?requirement_ids=…` which runs
   `find_vendor_affinity(mpn, db)` for each selected requirement's primary MPN,
   merges/dedupes by vendor (keep highest confidence), drops vendors already in the
   suggested list or excluded by unavailability, and returns rows rendered with a
   bordered indigo "affinity" chip + confidence % + the service's reasoning string in
   `title`. L3 (the Claude call) stays enabled inside the service as designed (the
   button gate makes the latency opt-in). Cap: 10 rows (the service's own cap).
3. **Find any vendor.** An autocomplete input reusing `GET /api/autocomplete/names`
   (vendors only — filter `type == "vendor"` client-side from the existing response;
   do NOT fork the endpoint). Selecting a result appends a checked row to the list. If
   the picked vendor is unavailability-excluded for the selected parts, the row renders
   with the rose "marked unavailable" chip and a **disabled** checkbox (the send-time
   re-validation remains the backstop).
4. **Add vendor on the fly.** An inline mini-form (name required; website + contact
   email optional) behind an "Add new vendor" toggle — the Enter-Offer modal's "New
   Vendor" pattern. POST to a new `POST /v2/partials/sightings/composer-vendor`
   endpoint that: runs the duplicate check (`/api/vendors/check-duplicate` logic —
   call the shared service path, not HTTP); on a confident duplicate, returns the
   existing vendor row instead (with a "matched existing vendor" notice); otherwise
   creates the minimal `VendorCard` (the crm/offers.py:331 pattern: normalized_name,
   display_name, optional domain) plus a `VendorContact` when an email was given, and
   returns the new checked row. Vendors created here without an email show the
   existing "no contact email" treatment and are reported via `X-RFQ-Skipped` at send
   (current behavior, unchanged).

Selection state stays in the modal's existing Alpine component; all new rows join the
same `vendor_names` form field. Tailwind: full literal classes only; follow the modal's
existing chip/badge vocabulary (indigo = informational chip family, rose = unavailable).

## Out of scope (deliberate)

- No change to email composition itself (plain-text body → `_build_html_body`).
- No per-vendor part subsetting in the composer (every selected vendor is asked about
  all selected parts; the per-requisition Contact split is a tracking concern, not a
  content concern).
- No affinity persistence/caching beyond the service's own behavior.
- No vendor-modal redesign outside the vendor panel (steps/preview/send UX unchanged).
- The requisitions-page RFQ flows (if any exist outside this modal) are untouched.

## Testing

- **Tracking (the core):** send spanning 2 requisitions × 2 vendors → exactly 4
  `Contact` rows, each with its own requisition_id and only that requisition's
  `parts_included`, sharing graph ids per vendor; subject contains both ref tokens;
  reply-matching attributes a simulated reply to both requisitions' contacts; both
  requisitions auto-progress OPEN→SOURCING; ActivityLog rows per requirement.
  Single-requisition sends behave byte-identically to today (regression).
- **Coverage ranking:** vendor with 3/4 parts ranks above 1/4 with higher engagement;
  excluded vendor absent; `N/M parts` rendered.
- **Affinity endpoint:** returns merged/deduped rows; drops already-suggested and
  excluded vendors; renders chip + confidence; L3 path mocked at the source module.
- **Autocomplete add:** excluded vendor renders disabled + rose chip.
- **Inline create:** duplicate → existing card returned, no new row in DB; new vendor →
  card + contact created, row checked; empty name → 400 JSON error format.
- **Headers/regression:** X-RFQ-Sent/Total/Skipped/Unavailable semantics unchanged.

## Risks

- Reply-matching parser is the one place with unknown behavior — read it FIRST
  (architect review must verify multi-token attribution is sound before build).
- `send_batch_rfq` is shared — single-requisition callers elsewhere must be traced and
  kept behavior-identical (grep all call sites before changing its signature; prefer an
  additive parameter or a mapping argument with a compatible default).
- SQLite tests vs PG: the coverage GROUP BY uses plain columns/aggregates only.
