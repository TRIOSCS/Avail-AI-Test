# Unavailability v2 — Condition-Aware Snapshots

**Status:** Design approved (brainstorm), pending implementation plan.
**Date:** 2026-06-29
**Supersedes/extends:** the v1 "Two Windows, Real Proof" temporal policy
(`docs/superpowers/specs/2026-06-10-unavailability-temporal-policy.md`) — that policy is
preserved unchanged; this adds a condition dimension on top of it.

## Context & problem

`VendorPartUnavailability` records the durable fact "this vendor's stock of this part is
gone." Today the unique key is `(vendor_name_normalized, normalized_mpn)` — **one row per
vendor+part, condition-blind.** So when a vendor is marked unavailable for a part in
*one* condition (e.g. a cancelled NEW purchase order), that single record suppresses the
vendor's sightings of the part in **every** condition — masking REFURB/used stock the
vendor genuinely has. This over-suppression hides real buyable inventory and wrongly
excludes vendors from RFQs.

The unavailability reasons split into two natural kinds:
- **Specific-stock-gone** — `bought_by_us`, `sold_elsewhere`, `broken`: a particular
  lot/unit is gone; the vendor may still have *other-condition* stock.
- **Part-isn't-really-there** — `not_really_there`, `different_part`, `other`: the vendor
  doesn't genuinely have this part *in any condition*.

v1 treats both identically. v2 makes unavailability condition-aware so specific-stock-gone
marks stop masking other-condition inventory, while part-isn't-really-there marks keep
suppressing everything.

## Goals
- A condition-specific mark suppresses only same-condition sightings/offers.
- A condition-agnostic mark suppresses all conditions (v1 behavior, explicitly chosen).
- Existing v1 rows keep their exact current behavior after migration (no regression).
- Preserve every v1 invariant: `is_active` (in `app/services/vendor_unavailability.py`)
  remains the sole suppression authority; `Sighting.is_unavailable` remains a render
  cache; the two temporal windows, `qty_at_mark` O2 restock override, and the
  `released_at`/`release_trigger` O3 release-pair CHECK all carry forward.

## Non-goals
- No new condition vocabulary. Reuse `normalize_condition()` + the `new/refurb/used/other`
  vocab (`app/utils/normalization.py`, the `chk_sight_condition`/`chk_offer_condition`
  constraints).
- No change to the temporal-window policy itself (durations, reason classes).
- No DROP of any existing column/table; no change to the reason enum.

## Design

### Condition model: nullable, "specific OR all"
Add `condition` to `vendor_part_unavailability`:
- Type `String(16)`, **nullable**. `NULL` ≡ "all conditions" (the catch-all).
- Allowed non-NULL values: `new` / `refurb` / `used`, enforced by an
  `@validates("condition")` that accepts a value already in that set or `None`.
  (Marks in practice produce `new`/`refurb`/`used`/`NULL`.)
  **Implementation deviation (post-review):** the design originally also accepted `other`
  on this column, on the premise that a `condition='other'` sighting *could* be matched by
  an explicit `other` mark. That premise is false — `normalize_condition()` never emits
  `other` (it returns `None`), so a read-side `other` sighting normalizes to `None` and is
  matched only by NULL records, and an `other` mark would match nothing. Worse, the mark
  form offering "Other" silently collapsed (via `normalize_condition`) to a NULL
  all-conditions record — the maximal over-suppression this feature exists to prevent. So
  `other` was dropped from this column's vocabulary (form + `@validates`); the existing
  "All conditions" (NULL) option already covers the broad case. (The separate
  `chk_sight_condition`/`chk_offer_condition` vocab on the Sighting/Offer columns is
  unchanged.)

### Uniqueness (DB-enforced, replaces the single unique constraint)
Drop `uq_vendor_part_unavail_vendor_mpn`. Add **two partial unique indexes** so the
invariant "≤1 row per specific condition AND ≤1 all-conditions row per (vendor, mpn)" is
structural:
- `uq_vpu_vendor_mpn_condition` — `UNIQUE (vendor_name_normalized, normalized_mpn,
  condition) WHERE condition IS NOT NULL`
- `uq_vpu_vendor_mpn_allcond` — `UNIQUE (vendor_name_normalized, normalized_mpn)
  WHERE condition IS NULL`

(Partial indexes are used rather than `NULLS NOT DISTINCT` for PG-version portability and
because the two-index form documents the two distinct uniqueness rules.)

### Matching semantics (the fix)
A sighting/offer of condition **X** (its stored canonical condition) is suppressed **iff an
`is_active` record exists with `condition = X` OR `condition IS NULL`.** This is the only
semantic change to the read path; the temporal `is_active` predicate is applied per-row
exactly as today.

### Mark path — `record_unavailability()`
Add parameter `condition: str | None = None`. The **reason→condition policy is applied
inside the function** (single enforcement point), AFTER `normalize_condition()` on any
passed value:
- `not_really_there` / `different_part` / `other` → coerce `condition = NULL` regardless of
  what was passed (part isn't there in any condition).
- `bought_by_us` / `sold_elsewhere` / `broken` → use `normalize_condition(condition)`
  (`None` when unknown/off-vocab → `NULL`, i.e. all-conditions — conservative, never worse
  than v1).

Upsert keys on `(vendor, mpn, condition)`. The existing upsert semantics (refresh
reason/note/created_by/created_at, NULL the release pair, refresh `requirement_id`,
re-snapshot `qty_at_mark` keeping the old value when the new computation is NULL) apply
per-`(vendor, mpn, condition)` row.

**Call-site wiring:**
- `app/services/po_cancellation_service.py` (PO-cancel / SP-4 fall-down) → pass
  `offer.condition`. This is the headline fix: a cancelled NEW PO marks only NEW unavailable.
- `app/routers/sightings.py` (manual vendor-unavailable mark) → pass the new condition
  selector value (defaults to blank → `NULL`/all, so a manual mark is never worse than v1).
  The selector is a small addition to the existing mark control; it is only meaningful for
  the specific-stock-gone reasons (for agnostic reasons the policy forces `NULL` anyway, so
  the UI may hide/disable it for those reasons).

`qty_at_mark` becomes a per-`(vendor, mpn, condition)` snapshot (same computation, scoped
to the row's condition where the source sightings carry one; an all-conditions/`NULL` row
snapshots across the part's sightings as today).

### Read path — `is_active` + `sighting_status`
- `app/services/vendor_unavailability.py` suppression/exclusion queries and
  `app/services/sighting_status.py` (the reader-authority branch that stamps
  `Sighting.is_unavailable`) gain the condition clause: match the sighting's canonical
  condition against active records with `condition = X OR condition IS NULL`.
- `is_active` itself (temporal windows, release state) is unchanged and still evaluated
  per-row — a row is only a suppressor if it is both condition-matching AND active.

### RFQ vendor exclusion (deliberate v2 behavior change)
RFQ vendor suggestions exclude a vendor for a part **only on a `NULL` (all-conditions)
active record.** A vendor merely out of a specific condition may still have other-condition
stock, so we should still RFQ them; only "doesn't have it at all" suppresses the RFQ. (v1
excluded on any record — this narrowing is intentional and is the RFQ-facing benefit.)

### Release — O3 (buyer-routed vendor email) + offer hook
An incoming offer/email proves the vendor has stock again. Release targets the records the
proof contradicts:
- Offer/email condition **X** (known) → release the `condition = X` record **and** the
  `NULL` catch-all (a real offer disproves both "out of X" and "doesn't have it at all"),
  via the existing `release(trigger, now)` transition. **Other** specific-condition records
  are left intact (a NEW offer does not release a REFURB-specific mark).
- Unknown/`NULL` offer condition → release all active records for that `(vendor, mpn)` (any
  unavailability claim is disproven by a real offer).

The O2 restock override (`qty_at_mark`) is evaluated per row, so a restock of condition X
overrides only the X (and not the NULL) record — consistent with the per-row model.

## Migration (claims the next free number — verify `alembic heads` at implementation; head
is `170_prospecting_persistence` at design time, so likely `171`)
1. `add_column` `condition String(16)` nullable.
2. `drop_constraint` `uq_vendor_part_unavail_vendor_mpn`.
3. `create_index` the two partial unique indexes above.
4. **Backfill:** every existing row → `condition = NULL`. Each existing row is already unique
   on `(vendor, mpn)`, so it becomes that pair's single catch-all row — **identical
   suppression behavior for all legacy data.**
5. **Downgrade:** drop the two partial indexes, drop the `condition` column, re-create
   `uq_vendor_part_unavail_vendor_mpn` (safe because post-downgrade every row is back to one
   per `(vendor, mpn)`).
6. Round-trip `upgrade → downgrade → upgrade` on a throwaway Postgres (never staging);
   confirm a single alembic head. Claim line appended to `MIGRATION_NUMBERS_IN_FLIGHT.txt`.

## Testing plan
- **Model:** `@validates("condition")` (accepts `new/refurb/used`/`None`, rejects
  off-vocab); the two partial indexes (reject a 2nd NEW row and a 2nd NULL row for a
  `(vendor, mpn)`; allow NEW + REFURB + NULL coexisting).
- **Mark:** reason→condition coercion (each agnostic reason forces NULL; each specific
  reason keeps `normalize_condition(value)`; off-vocab → NULL); PO-cancel uses
  `offer.condition`; per-condition `qty_at_mark`.
- **Read (headline):** a NEW-specific active record suppresses a NEW sighting but **not** a
  REFURB sighting of the same part/vendor; a NULL record suppresses all conditions; the two
  temporal windows still bound suppression per row.
- **RFQ exclusion:** a specific-condition record does **not** exclude the vendor from RFQ; a
  NULL record does.
- **Release:** an offer of condition X releases the X record + the NULL record, leaves a
  REFURB-specific record intact; an unknown-condition offer releases all for the pair.
- **Migration:** legacy rows backfill to NULL and behave identically (a regression test that
  pre-migration suppression == post-migration suppression for an all-conditions mark);
  round-trip clean.

## Files touched (implementation preview, not part of approval)
- `app/models/vendor_part_unavailability.py` — column, `@validates`, `__table_args__`.
- `alembic/versions/NNN_unavailability_condition.py` — new migration.
- `app/services/vendor_unavailability.py` — `record_unavailability(condition=...)` + the
  reason→condition policy + condition-aware suppression/exclusion/release.
- `app/services/sighting_status.py` — condition-aware reader branch.
- `app/services/po_cancellation_service.py` — pass `offer.condition`.
- `app/routers/sightings.py` (+ its mark template) — condition selector on the manual mark.
- `docs/APP_MAP_DATABASE.md` + `docs/APP_MAP_INTERACTIONS.md` — schema row + read/mark/release flow.
- Tests as above.
