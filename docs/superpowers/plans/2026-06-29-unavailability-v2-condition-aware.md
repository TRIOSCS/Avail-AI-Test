# Unavailability v2 (Condition-Aware) Implementation Plan

> **Status (2026-06-29):** ✅ BUILT — PR #579 open, CI green, awaiting human review/merge (brings alembic head 170→171). **Delete this plan and its design spec once #579 merges.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `VendorPartUnavailability` condition-aware so a vendor marked unavailable in one condition (e.g. NEW) no longer masks their other-condition stock (e.g. REFURB).

**Architecture:** Add a nullable `condition` column (`NULL` = all-conditions catch-all) keyed by two partial unique indexes. Suppression matches iff `condition = X OR condition IS NULL`. A reason→condition policy lives in `record_unavailability`. All v1 invariants preserved (`is_active` sole authority; `Sighting.is_unavailable` render cache; two-window temporal policy; `qty_at_mark` O2; release-pair CHECK).

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16 (partial indexes), Alembic, pytest (SQLite in tests — note SQLite ignores partial-index uniqueness, so DB-uniqueness is migration-round-tripped on PG, not asserted in unit tests).

**Spec:** `docs/superpowers/specs/2026-06-29-unavailability-v2-condition-aware-design.md` (read it first).

## Global Constraints
- Reuse `normalize_condition()` (`app/utils/normalization.py:246` → returns `new`/`refurb`/`used`/`None`). Vocab: `new`/`refurb`/`used`/`other` + `NULL`.
- Run tests with `TESTING=1 PYTHONPATH=$(pwd) pytest ... ` (or `/root/availai` outside a worktree). `pre-commit run --all-files` before each push; run twice if docformatter rewraps.
- Migration: claim the next free number (verify `TESTING=1 alembic heads`; head is `170_prospecting_persistence` at plan time → likely `171`). Append a claim line to `MIGRATION_NUMBERS_IN_FLIGHT.txt`. The migration-guard hook blocks editing `alembic/versions/*` with the Edit tool only for ALREADY-APPLIED files — a brand-new migration file is created with Write (fine).
- `is_active(record, now)` (`app/services/vendor_unavailability.py:130`) is unchanged — it stays the sole temporal authority, evaluated per-row.
- Do NOT change reason enum, temporal-window durations, or any existing column.

---

### Task 1: Model — `condition` column + `@validates` + partial indexes

**Files:**
- Modify: `app/models/vendor_part_unavailability.py`
- Test: `tests/test_vendor_part_unavailability.py` (create if absent; else add a class)

**Interfaces:**
- Produces: `VendorPartUnavailability.condition` (`str | None`); `@validates("condition")` accepting `{new,refurb,used,other}`/`None`, raising `ValueError` otherwise.

- [ ] **Step 1: Write the failing test**
```python
import pytest
from app.models.vendor_part_unavailability import VendorPartUnavailability

def _row(**kw):
    base = dict(vendor_name_normalized="acme", normalized_mpn="abc123", reason="broken")
    base.update(kw); return VendorPartUnavailability(**base)

def test_condition_accepts_vocab_and_null():
    assert _row(condition="new").condition == "new"
    assert _row(condition="refurb").condition == "refurb"
    assert _row(condition=None).condition is None

def test_condition_rejects_offvocab():
    with pytest.raises(ValueError):
        _row(condition="brand-new-ish")
```
- [ ] **Step 2: Run → FAIL** `pytest tests/test_vendor_part_unavailability.py -k condition -v` (AttributeError/no validation).
- [ ] **Step 3: Implement.** In `vendor_part_unavailability.py`: add `condition = Column(String(16))` after `normalized_mpn`. Add:
```python
    @validates("condition")
    def _validate_condition(self, _key, value):
        if value is None:
            return None
        if value not in {"new", "refurb", "used", "other"}:
            raise ValueError(f"condition={value!r} not in new/refurb/used/other or NULL")
        return value
```
Replace the `UniqueConstraint(... name="uq_vendor_part_unavail_vendor_mpn")` line in `__table_args__` with two partial unique indexes:
```python
        Index(
            "uq_vpu_vendor_mpn_condition", "vendor_name_normalized", "normalized_mpn", "condition",
            unique=True, postgresql_where=text("condition IS NOT NULL"),
        ),
        Index(
            "uq_vpu_vendor_mpn_allcond", "vendor_name_normalized", "normalized_mpn",
            unique=True, postgresql_where=text("condition IS NULL"),
        ),
```
Add `from sqlalchemy import text` to the imports. Update the module docstring's "One row per (vendor, MPN)" line to "(vendor, MPN, condition) where condition NULL = all conditions".
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `git add app/models/vendor_part_unavailability.py tests/test_vendor_part_unavailability.py && git commit -m "feat(unavail): condition column + @validates + partial unique indexes"`

---

### Task 2: Migration

**Files:**
- Create: `alembic/versions/NNN_unavailability_condition.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`
- Test: `tests/test_migration_unavailability_condition.py`

**Interfaces:** Consumes Task 1's model. Produces the `condition` column + the two partial indexes on the DB; drops `uq_vendor_part_unavail_vendor_mpn`.

- [ ] **Step 1:** Confirm head: `TESTING=1 alembic heads` → use that as `down_revision`. Pick the lowest free 3-digit number not in `alembic/versions/` and not in `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
- [ ] **Step 2: Write the migration** (Write tool):
```python
"""unavailability condition column + partial unique indexes."""
from alembic import op
import sqlalchemy as sa

revision = "NNN_unavail_condition"          # <=32 chars
down_revision = "170_prospecting_persistence"  # replace with actual head
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("vendor_part_unavailability", sa.Column("condition", sa.String(16), nullable=True))
    op.drop_constraint("uq_vendor_part_unavail_vendor_mpn", "vendor_part_unavailability", type_="unique")
    op.create_index("uq_vpu_vendor_mpn_condition", "vendor_part_unavailability",
                    ["vendor_name_normalized", "normalized_mpn", "condition"],
                    unique=True, postgresql_where=sa.text("condition IS NOT NULL"))
    op.create_index("uq_vpu_vendor_mpn_allcond", "vendor_part_unavailability",
                    ["vendor_name_normalized", "normalized_mpn"],
                    unique=True, postgresql_where=sa.text("condition IS NULL"))
    # Existing rows are condition=NULL automatically (add_column default) → behavior unchanged.

def downgrade():
    op.drop_index("uq_vpu_vendor_mpn_allcond", "vendor_part_unavailability")
    op.drop_index("uq_vpu_vendor_mpn_condition", "vendor_part_unavailability")
    op.drop_column("vendor_part_unavailability", "condition")
    op.create_unique_constraint("uq_vendor_part_unavail_vendor_mpn", "vendor_part_unavailability",
                                ["vendor_name_normalized", "normalized_mpn"])
```
- [ ] **Step 3:** Append claim line to `MIGRATION_NUMBERS_IN_FLIGHT.txt`: `NNN feat/unavailability-v2 condition column + 2 partial unique indexes; chains onto <head>`.
- [ ] **Step 4: Round-trip on throwaway PG** (NOT staging): spin `docker run --rm -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:16`, point a DATABASE_URL at it, `alembic upgrade head → downgrade -1 → upgrade head`; verify `alembic heads` = 1. Insert two rows `(acme, abc, 'new')` + `(acme, abc, NULL)` succeed; a 2nd `(acme, abc, 'new')` and a 2nd `(acme, abc, NULL)` violate the partial uniques.
- [ ] **Step 5:** A migration-presence test in `tests/test_migration_unavailability_condition.py` asserting the upgrade source contains `add_column("vendor_part_unavailability"` and both index names (SQLite can't enforce partial uniqueness; assert at source level + rely on the PG round-trip). Run + `pre-commit`.
- [ ] **Step 6: Commit** `git add alembic/versions/ MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_migration_unavailability_condition.py && git commit -m "feat(unavail): migration — condition column + partial unique indexes"`

---

### Task 3: Reason→condition policy + condition-keyed mark

**Files:**
- Modify: `app/services/vendor_unavailability.py` (`record_unavailability` at :322; add a `_condition_for_reason` helper)
- Test: `tests/test_vendor_unavailability.py` (existing — add cases)

**Interfaces:**
- Produces: `record_unavailability(db, requirement, vendor_name, reason, note, user, condition=None)`; helper `_condition_for_reason(reason, condition) -> str | None`. Upsert key becomes `(vendor_norm, key, condition)`.

- [ ] **Step 1: Write failing tests** (use existing fixtures in that test file for requirement/vendor/sighting):
```python
from app.constants import UnavailabilityReason as R
from app.services.vendor_unavailability import _condition_for_reason

def test_agnostic_reasons_force_null():
    for r in (R.NOT_REALLY_THERE, R.DIFFERENT_PART, R.OTHER):
        assert _condition_for_reason(r, "new") is None

def test_specific_reasons_keep_normalized_condition():
    assert _condition_for_reason(R.BROKEN, "New") == "new"     # normalize_condition lowercases
    assert _condition_for_reason(R.SOLD_ELSEWHERE, "pulls") == "used"
    assert _condition_for_reason(R.BOUGHT_BY_US, None) is None  # unknown → NULL (conservative)

# Integration: marking NEW then REFURB for same vendor+part creates TWO rows
def test_mark_two_conditions_creates_two_rows(db_session, requirement_with_vendor_sightings):
    req, vendor = requirement_with_vendor_sightings
    record_unavailability(db_session, req, vendor, R.BROKEN, None, None, condition="new")
    record_unavailability(db_session, req, vendor, R.BROKEN, None, None, condition="refurb")
    db_session.commit()
    rows = db_session.query(VendorPartUnavailability).all()
    conds = sorted(r.condition for r in rows)
    assert conds == ["new", "refurb"]
```
- [ ] **Step 2: Run → FAIL** (`_condition_for_reason` missing; `condition` kwarg unknown).
- [ ] **Step 3: Implement.** Add near the top of the service:
```python
def _condition_for_reason(reason: UnavailabilityReason, condition: str | None) -> str | None:
    """Agnostic reasons (part isn't really there) → NULL (all conditions);
    specific-stock-gone reasons → normalize_condition(value) (None → NULL)."""
    from ..utils.normalization import normalize_condition
    agnostic = {UnavailabilityReason.NOT_REALLY_THERE, UnavailabilityReason.DIFFERENT_PART, UnavailabilityReason.OTHER}
    if reason in agnostic:
        return None
    return normalize_condition(condition)
```
In `record_unavailability`: add `condition: str | None = None` to the signature; compute `cond = _condition_for_reason(reason, condition)` once; thread `cond` into the per-key upsert lookup (the existing `.filter(... vendor_norm, key ...)` query gains `VendorPartUnavailability.condition == cond` — use `.is_(None)` when `cond is None`) and set `record.condition = cond` on create. Keep all existing upsert behavior. (Read the function body around :322–:410 and add condition to the WHERE + the new-row constructor.)
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `git commit -am "feat(unavail): reason→condition policy + condition-keyed mark"`

---

### Task 4: Condition-aware suppression (read path)

**Files:**
- Modify: `app/services/vendor_unavailability.py` (`apply_to_fresh_sightings` :528) and `app/services/sighting_status.py` (`compute_vendor_statuses` :44, the unavailable batch :95–:146)
- Test: `tests/test_vendor_unavailability.py`, `tests/test_sighting_status.py`

**Interfaces:** Consumes Task 1/3. Produces: a helper `_condition_matches(record_condition, sighting_condition) -> bool` reused by both readers.

- [ ] **Step 1: Write failing tests** (the headline behavior):
```python
def test_new_mark_does_not_suppress_refurb(db_session, req_with_new_and_refurb_sightings):
    req, vendor = req_with_new_and_refurb_sightings  # vendor has a NEW and a REFURB sighting of the part
    record_unavailability(db_session, req, vendor, R.BROKEN, None, None, condition="new")
    db_session.commit()
    apply_to_fresh_sightings(db_session, req)
    statuses = {(s.condition): s.is_unavailable for s in _vendor_sightings(db_session, req, normalize_vendor_name(vendor))}
    assert statuses["new"] is True
    assert statuses["refurb"] is False        # NOT masked

def test_null_mark_suppresses_all(db_session, req_with_new_and_refurb_sightings):
    req, vendor = req_with_new_and_refurb_sightings
    record_unavailability(db_session, req, vendor, R.NOT_REALLY_THERE, None, None)  # → NULL
    db_session.commit()
    apply_to_fresh_sightings(db_session, req)
    assert all(s.is_unavailable for s in _vendor_sightings(db_session, req, normalize_vendor_name(vendor)))
```
- [ ] **Step 2: Run → FAIL** (REFURB currently masked).
- [ ] **Step 3: Implement.** Add:
```python
def _condition_matches(record_condition: str | None, sighting_condition: str | None) -> bool:
    """A record suppresses a sighting iff the record is all-conditions (NULL) or
    its condition equals the sighting's normalized condition."""
    if record_condition is None:
        return True
    from ..utils.normalization import normalize_condition
    return record_condition == normalize_condition(sighting_condition)
```
In `apply_to_fresh_sightings` (:528): where a record is matched to a sighting for stamping (around :581), add `and _condition_matches(rec.condition, sighting.condition)` to the active-record predicate. In `sighting_status.compute_vendor_statuses` unavailable batch (:95–:146): the "any active record" check (:146 `any(is_active(rec, now) for rec in matching)`) must become "any active record that matches THIS vendor's sighting condition." Since that branch works per-vendor across the vendor's sightings, evaluate per-sighting: a vendor sighting is "unavailable" iff some active record matches its condition; aggregate to the vendor pill per the existing rows-win rule. (Read :95–:167 and thread `_condition_matches(rec.condition, s.condition)` into the matching loop.)
- [ ] **Step 4: Run → PASS** (+ existing suppression tests still green).
- [ ] **Step 5: Commit** `git commit -am "feat(unavail): condition-aware suppression in apply + sighting_status"`

---

### Task 5: RFQ exclusion only on NULL records

**Files:**
- Modify: `app/services/vendor_unavailability.py` (`excluded_vendor_norms` :716)
- Test: `tests/test_vendor_unavailability.py`

**Interfaces:** Consumes Task 1/3. Behavior change: exclusion fires only for `condition IS NULL` active rows.

- [ ] **Step 1: Write failing tests:**
```python
def test_specific_condition_does_not_exclude_from_rfq(db_session, req_with_vendor):
    req, vendor = req_with_vendor
    record_unavailability(db_session, req, vendor, R.BROKEN, None, None, condition="new")
    db_session.commit()
    assert normalize_vendor_name(vendor) not in excluded_vendor_norms(db_session, [req])

def test_allconditions_record_excludes_from_rfq(db_session, req_with_vendor):
    req, vendor = req_with_vendor
    record_unavailability(db_session, req, vendor, R.NOT_REALLY_THERE, None, None)
    db_session.commit()
    assert normalize_vendor_name(vendor) in excluded_vendor_norms(db_session, [req])
```
- [ ] **Step 2: Run → FAIL** (v1 excludes on the specific-condition row too).
- [ ] **Step 3: Implement.** In `excluded_vendor_norms` (:740 return): add `and rec.condition is None` to the comprehension filter: `return {rec.vendor_name_normalized for rec in rows if rec.condition is None and is_active(rec, now)}`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `git commit -am "feat(unavail): RFQ exclusion fires only on all-conditions records"`

---

### Task 6: Condition-aware release (O3 / offer hook)

**Files:**
- Modify: `app/services/vendor_unavailability.py` (`release_on_offer` :623, `maybe_release_on_offer` :683)
- Test: `tests/test_vendor_unavailability.py`

**Interfaces:** Consumes Task 1/3. Behavior: an offer of condition X releases the `X` record + the `NULL` record; unknown condition releases all for the pair.

- [ ] **Step 1: Write failing tests:**
```python
def test_offer_releases_matching_and_null_not_other(db_session, req_with_vendor):
    req, vendor = req_with_vendor
    record_unavailability(db_session, req, vendor, R.BROKEN, None, None, condition="new")
    record_unavailability(db_session, req, vendor, R.BROKEN, None, None, condition="refurb")
    record_unavailability(db_session, req, vendor, R.NOT_REALLY_THERE, None, None)  # NULL
    db_session.commit()
    release_on_offer(db_session, vendor_name=vendor, mpn_key="<key for req>", condition="new", now=<now>)
    db_session.commit()
    rows = {r.condition: r for r in db_session.query(VendorPartUnavailability).all()}
    assert rows["new"].released_at is not None        # released
    assert rows[None].released_at is not None          # NULL catch-all released
    assert rows["refurb"].released_at is None          # other-condition intact
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Read `release_on_offer`/`maybe_release_on_offer` (:623–:716) — they currently load rows for `(vendor, key)` and release active ones. Add a `condition` arg (the offer's `normalize_condition(offer.condition)`): release a row iff `row.condition is None` OR `row.condition == cond` OR `cond is None` (unknown → release all). Use the existing `_release_record(...)` / `record.release(...)` transition (do NOT bypass the release-pair CHECK). Thread the offer condition from the caller `maybe_release_on_offer`.
- [ ] **Step 4: Run → PASS** (+ existing release tests green).
- [ ] **Step 5: Commit** `git commit -am "feat(unavail): condition-aware release targets X + NULL records"`

---

### Task 7: Call-site wiring (PO-cancel offer.condition + manual-mark selector)

**Files:**
- Modify: `app/services/po_cancellation_service.py:171` (pass `offer.condition`)
- Modify: `app/routers/sightings.py:1124` + the mark template (add a condition selector; thread to `record_unavailability(..., condition=...)`)
- Test: `tests/test_po_cancellation.py` (or the SP-4 test), `tests/test_sightings_router*.py`

**Interfaces:** Consumes Task 3's `condition=` param. No new exports.

- [ ] **Step 1: Write failing tests:** (a) PO-cancel of a NEW offer writes a `condition='new'` unavailability (not NULL); (b) the manual-mark route forwards a posted `condition` form field to `record_unavailability`; with no field, `condition=None`.
```python
def test_po_cancel_marks_offer_condition(db_session, offer_new, ...):
    po_cancellation_service.<cancel fn>(db_session, offer_new, ...)
    rec = db_session.query(VendorPartUnavailability).filter_by(vendor_name_normalized=...).one()
    assert rec.condition == "new"
```
- [ ] **Step 2: Run → FAIL** (currently NULL).
- [ ] **Step 3: Implement.** `po_cancellation_service.py:171`: `record_unavailability(db, requirement, offer.vendor_name, unavailability_reason, note, user, condition=offer.condition)`. `sightings.py:1124`: read an optional `condition` form field (validate via `normalize_condition` or pass raw — the policy handles it), pass `condition=...`. Add a small `<select name="condition">` (blank + new/refurb/used) to the mark control template, shown for specific-stock-gone reasons (mirror an existing select; per the UI guardrail, copy an existing pattern — do not invent chrome). Verify no Alpine init breakage.
- [ ] **Step 4: Run → PASS.** Then full affected suite: `pytest tests/ -k "unavail or sighting or po_cancel or resource" -q`. `pre-commit run --all-files`.
- [ ] **Step 5: Update docs + commit.** Update `docs/APP_MAP_DATABASE.md` (condition column) + `docs/APP_MAP_INTERACTIONS.md` (mark/read/RFQ/release flow). `git commit -am "feat(unavail): wire PO-cancel offer.condition + manual-mark condition selector + docs"`. Open PR.

---

## Self-Review (done)
- **Spec coverage:** schema (T1) · migration+backfill (T2) · mark policy (T3) · suppression match (T4) · RFQ-only-NULL (T5) · release X+NULL (T6) · call-site wiring + UI selector (T7). All spec sections covered.
- **Type consistency:** `condition: str | None`, `_condition_for_reason`, `_condition_matches`, `record_unavailability(..., condition=None)` consistent across tasks.
- **Open implementation note:** T4 and T6 require reading the exact current bodies (`apply_to_fresh_sightings`, `compute_vendor_statuses`, `release_on_offer`) — the plan gives the precise predicate to add at the named anchors; the executing subagent reads those functions before editing.
