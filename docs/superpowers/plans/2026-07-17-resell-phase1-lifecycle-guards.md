# Resell Rework — Phase 1: Lifecycle Guards — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Retire the Resell module's state-machine corruption by adding the two missing lifecycle guards (publish, award/withdraw), converting accepted-bid re-assembly to immutable history (D3), and remapping legacy statuses (D5) — each deployable, migration claimed.

**Architecture:** Guards live in the SERVICE layer (routers stay thin), mirroring the existing `close_list` precondition pattern (`excess_service.py:927-928`). A data-only Alembic migration remaps legacy statuses first (order-coupled with the publish guard). Tests copy the `tests/test_resell_list_lifecycle.py` precondition-pack pattern.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL 16, pytest (SQLite in tests).

## Global Constraints
- Status values ALWAYS from `app/constants.py:ExcessListStatus` / offer-status enums — never raw strings.
- Guards raise `HTTPException(409)` from the service (mirror `excess_service.py:927-928`); routers already delegate.
- Migration claims the next free number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` — **193** (192 reserved by the concurrent Approvals-Workspace branch). Re-chain `down_revision` onto whatever is `alembic heads` at merge; keep the number.
- Every migration: round-trip upgrade→downgrade→upgrade on a THROWAWAY PG 16 (never staging).
- Decisions in force: **D3** (accepted-bid revision = new immutable `CustomerBid` row per revision), **D5** (keep `CLOSED` distinct; legacy `closed→CLOSED`, NOT `→bid_out`; caption `bid_out`→"Bids out").
- After code changes, update `docs/APP_MAP_*` where the lifecycle/model is described.
- CLOSED forward-writer (a new "Close without bidding" action) is OUT OF SCOPE here — later phase, needs UI approval.

---

### Task 1: Legacy-status remap migration (193) — MUST land with/before the publish guard

**Files:**
- Create: `alembic/versions/193_resell_legacy_status_remap.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt` (append claim in same commit)
- Test: `tests/test_resell_legacy_status_remap.py`

**Interfaces:**
- Produces: an `excess_lists` table where no row has status in `{active, bidding}` (remapped to `open`/`collecting`); legacy `closed` rows remapped to canonical `CLOSED` (`"closed"` — same string, so this is a no-op for the string but documents intent + covers any casing); `open_at` stamped where NULL for remapped open/collecting rows.

- [ ] **Step 1: Confirm the live legacy row set (read-only) before writing the UPDATE**

Run against the throwaway/staging-mirror DB: `SELECT id, status FROM excess_lists WHERE status IN ('active','bidding','closed');` — record the id set (plan claims ids 2/3/4; verify, do not assume).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_resell_legacy_status_remap.py
import pytest
from sqlalchemy import text
from app.constants import ExcessListStatus

def test_remap_maps_legacy_active_and_bidding(db_session, make_excess_list):
    a = make_excess_list(status="active", open_at=None)
    b = make_excess_list(status="bidding")
    # simulate the migration's data UPDATE (the migration body factored into a helper)
    from alembic_helpers.resell_remap import remap_legacy_statuses  # created in Step 4
    remap_legacy_statuses(db_session.connection())
    db_session.expire_all()
    assert a.status == ExcessListStatus.OPEN
    assert a.open_at is not None           # stamped where NULL
    assert b.status == ExcessListStatus.COLLECTING

def test_remap_keeps_closed_distinct(db_session, make_excess_list):
    c = make_excess_list(status="closed")
    from alembic_helpers.resell_remap import remap_legacy_statuses
    remap_legacy_statuses(db_session.connection())
    db_session.expire_all()
    assert c.status == ExcessListStatus.CLOSED   # NOT bid_out
```

- [ ] **Step 3: Run it — expect ImportError / FAIL.**

- [ ] **Step 4: Write the migration + shared remap helper**

Migration `upgrade()`: three `op.execute(text(...))` UPDATEs — `active→open` (+ `open_at = COALESCE(open_at, now())`), `bidding→collecting`, `closed→closed` (identity/casing normalize). `downgrade()`: documented no-op (irreversible many-to-one; mirror 093/100/189). `down_revision` = current `alembic heads`. Factor the SQL into `remap_legacy_statuses(connection)` so the test drives the same code.

- [ ] **Step 5: Run the test — PASS. Then round-trip on throwaway PG:** `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`; `alembic heads` shows one head.

- [ ] **Step 6: Claim 193 in `MIGRATION_NUMBERS_IN_FLIGHT.txt` + commit** (migration + claim + test together).

---

### Task 2: Publish guard — 409 unless DRAFT + clear stale close_at

**Files:**
- Modify: `app/services/excess_mirror.py:297-319` (`publish_list`)
- Test: `tests/test_resell_mirror.py` (add pack; rework `test_publish_twice_no_second_virtual_req:330`)

**Interfaces:**
- Consumes: `ExcessListStatus.DRAFT`.
- Produces: `publish_list` raises 409 unless `status == DRAFT`; on publish, `open_at` set, `updated_at` set, and any stale `close_at` cleared to `None`.

- [ ] **Step 1: Write failing tests** — publish rejects each of `collecting/bid_out/awarded/expired/closed` with 409; publish of a draft that somehow carries a `close_at` clears it; the happy draft→open path still mirrors. Rework `test_publish_twice_no_second_virtual_req` to assert the 2nd publish 409s (move the single-virtual-req assertion to a direct `sync_list_mirror` call).

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** — at the top of `publish_list`, before setting OPEN: `if el.status != ExcessListStatus.DRAFT: raise HTTPException(409, ...)` (message mirrors `close_list`). Set `el.close_at = None` alongside `el.open_at = now`.

- [ ] **Step 4: Run — PASS.**

- [ ] **Step 5: Commit.**

---

### Task 3: Award guard + service-level withdraw guard

**Files:**
- Modify: `app/services/excess_service.py:777-843` (`award_offer`), `:634-655` (`withdraw_offer`)
- Test: `tests/test_resell_award.py` (add pack)

**Interfaces:**
- Consumes: `_WITHDRAWABLE_OFFER_STATUSES = (OPEN, LATE)` (`resell.py:87`) — reuse the same membership (import or mirror in the service).
- Produces: `award_offer` 409 unless `offer.status in (OPEN, LATE)` (after the WON-idempotency check); `withdraw_offer` 409 unless `offer.status in (OPEN, LATE)`.

- [ ] **Step 1: Write failing tests** — award of a `withdrawn` offer → 409; award of a `lost` offer → 409; happy award of `open`/`late` still WONs; `withdraw_offer` **service** call on a WON offer → 409 (the router path is already covered at `:903`).

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** — in `award_offer`, after the WON idempotency guard (`:801-802`), add `if offer.status not in (OfferStatus.OPEN, OfferStatus.LATE): raise HTTPException(409, ...)`. In `withdraw_offer`, add the same membership guard after the 404 check (`:642-643`). Keep the router guard (defence in depth).

- [ ] **Step 4: Run — PASS.**

- [ ] **Step 5: Commit.**

---

### Task 4: Re-assemble terminal guard (D3 — new immutable row)

**Files:**
- Modify: `app/services/bid_back_service.py:58-174` (`build_bid_back`, re-assemble branch `:116-134`)
- Modify (docstrings): `bid_back_service.py:76-80`, `app/models/excess.py:255-258`, `alembic/versions/128_bid_back_schema.py` (reconcile to new-row semantics)
- Test: `tests/test_resell_bid_lifecycle.py` (add pack)

**Interfaces:**
- Consumes: `CustomerBidStatus` (DRAFT/SENT/ACCEPTED/REJECTED).
- Produces: when the latest `CustomerBid` is terminal (`ACCEPTED`/`REJECTED`), `build_bid_back` INSERTs a NEW `CustomerBid` row (`revision = latest.revision + 1`, `status = DRAFT`) instead of mutating the frozen one; the terminal row is left untouched. `rejected→revise` still works (produces a new row). `_latest_bid` (`resell.py:690`) + the id-desc select (`:116`) surface the newest.

- [ ] **Step 1: Write failing tests** — re-assemble after ACCEPTED creates a NEW row (old ACCEPTED row unchanged: sent_at/responded_at/responded_by_id preserved), new row is DRAFT rev+1; re-assemble after REJECTED creates a new DRAFT row; re-assemble on a DRAFT/SENT bid keeps prior in-place behavior OR (decide) also new-row — pick per-plan: only terminal statuses fork a new row, DRAFT stays in-place (bump), SENT resets to fresh draft (existing `:148` behavior preserved).

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** — branch on `latest.status`: terminal → insert new row; non-terminal → existing behavior. Reconcile the three docstrings to the new-row semantics.

- [ ] **Step 4: Run — PASS.**

- [ ] **Step 5: Commit.**

---

### Task 5: Lifecycle-precondition test pack completion + caption

**Files:**
- Modify: `app/templates/htmx/partials/shared/_macros.html:106-135` (bid_out label → "Bids out")
- Test: consolidate/verify the packs from Tasks 2–4; add any missing exclusion cases; ratchet `scripts/assertion_theater_baseline.txt` if touched.

- [ ] **Step 1:** Add the caption override so `bid_out` renders "Bids out" (explicit label map, not the generic `replace('_',' ')|capitalize`, to keep it distinct from CLOSED's "Closed").
- [ ] **Step 2:** Full suite green: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_resell_*.py -v`.
- [ ] **Step 3:** `pre-commit run --all-files`; update `docs/APP_MAP_DATABASE.md`/`INTERACTIONS.md` lifecycle notes.
- [ ] **Step 4:** Commit. Open PR; run the pr-review-fleet; live-verify on staging (publish/award/withdraw/re-assemble 409s; legacy rows remapped).

---

## Self-Review
- **Spec coverage:** #2 publish (T2), #3 award+withdraw (T3), #4 re-assemble/D3 (T4), legacy remap/D5 (T1), caption/D5 (T5). ✓
- **Order constraint:** T1 (remap) lands with/before T2 (publish guard) — same PR. ✓
- **CLOSED forward-writer** intentionally deferred (later phase, UI approval). ✓
- **Type consistency:** guards reuse `_WITHDRAWABLE_OFFER_STATUSES`; migration reuses `ExcessListStatus`. ✓
