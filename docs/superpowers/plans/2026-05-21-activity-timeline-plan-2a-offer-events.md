# Activity Timeline — Plan 2a: Offer Events

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every offer creation and offer status change writes an `activity_log` row through the canonical `log_activity()` writer, so offer events appear on the requisition Activity tab.

**Architecture:** Plan 1 shipped `log_activity()` (canonical writer) and the `ActivityType` enum. This plan adds a `log_activity()` call at all 10 offer-creation sites and all 10 offer-status-change sites. Every offer status transition logs `ActivityType.OFFER_STATUS_CHANGED` (the specific transition lives in `description`/`details`) — there is no per-transition enum member. No schema migration.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest (in-memory SQLite), Loguru.

**Spec:** `docs/superpowers/specs/2026-05-20-activity-timeline-design.md` (build step 2, offers portion).

**Branch:** Create `feat/activity-timeline-2a` off `feat/activity-timeline` (Plan 1, PR #120) — Plan 2a depends on `log_activity()` and `ActivityType` from Plan 1. If #120 has merged to `main`, branch off `main` instead.

---

## Conventions for every task

**Canonical call shape — offer creation:**

```python
log_activity(
    db,
    activity_type=ActivityType.OFFER_CREATED,
    requisition_id=<req id in scope>,
    requirement_id=<requirement id in scope, or None>,
    user_id=<user id in scope, or None>,
    vendor_card_id=<vendor_card id in scope, or None>,
    description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
    details={"offer_id": offer.id, "source": offer.source},
)
```

**Canonical call shape — offer status change:**

```python
log_activity(
    db,
    activity_type=ActivityType.OFFER_STATUS_CHANGED,
    requisition_id=offer.requisition_id,
    user_id=user.id,
    vendor_card_id=offer.vendor_card_id,
    description=f"Offer {offer.vendor_name} status: {old_status} → {new_status}",
    details={"offer_id": offer.id, "old_status": str(old_status), "new_status": str(new_status)},
)
```

**Rules for every task:**
- The `log_activity` call goes **after** the offer row has an `id` (after `db.add` + `db.flush`, or after the existing `db.commit`) and **before** the function returns.
- In loops, the call goes **inside** the loop, once per offer (Plan 2a does not aggregate — offer events are individually meaningful).
- If a creation site has no per-iteration `db.flush()`, add `db.flush()` after `db.add(...)` so `offer.id` is populated before `log_activity`.
- Imports: add `from app.constants import ActivityType` and `from app.services.activity_service import log_activity` — **match each file's existing import style** (relative vs absolute, and depth). Verify whether the file already imports either name before adding.
- **Verify every line number against the current file before editing** — this plan was written against a specific revision and the numbers will drift as tasks land commits. Use the quoted anchor code (function name + the `Offer(` / `offer.status =` line) to locate the site, not the raw line number.
- TDD: write the failing test first, run it, confirm it fails for the expected reason, then implement.
- Tests run: `TESTING=1 PYTHONPATH=/root/availai pytest <file> -v --override-ini="addopts="`
- Loguru not print; Ruff clean; follow existing patterns.
- Each task ends in its own commit. Do not push or open a PR until the plan owner approves.

**Helper used by every test** — assert an activity row exists:

```python
from app.models import ActivityLog


def _activity_rows(db, requisition_id, activity_type):
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.activity_type == activity_type,
        )
        .all()
    )
```

Put this helper at the top of the new test file (Task 1 creates it; later tasks import or reuse it).

---

### Task 1: `offer_created` — router paths (`crm/offers.py`, `htmx_views.py add_offer`)

Instrument the two single-offer router creation paths.

**Sites:**
- `app/routers/crm/offers.py` — `create_offer()`. `Offer(...)` ~line 349, `db.add` ~376, `db.commit` ~397. Scope: `db` (param), `user.id`, `req_id`, `payload.requirement_id`, `card.id` (vendor_card). Insert after the `db.commit()`.
- `app/routers/htmx_views.py` — `add_offer()`. `Offer(...)` ~line 1970, `db.add` ~1997, `db.commit` ~1998. Scope: `db`, `user.id`, `req_id`, requirement id via `_safe_int(form.get("requirement_id"))`, no vendor_card. Insert after the `db.commit()`.

**Files:**
- Modify: `app/routers/crm/offers.py`
- Modify: `app/routers/htmx_views.py`
- Test: `tests/test_offer_activity_logging.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_offer_activity_logging.py`:

```python
"""test_offer_activity_logging.py — offer events write activity_log rows.

Covers Plan 2a: offer_created at all 10 creation sites and offer_status_changed
at all 10 status-change sites route through activity_service.log_activity().

Called by: pytest
Depends on: app/services/activity_service.py, app/constants.py, conftest.py
"""

from app.constants import ActivityType
from app.models import ActivityLog


def _activity_rows(db, requisition_id, activity_type):
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.activity_type == activity_type,
        )
        .all()
    )


def test_create_offer_route_logs_offer_created(client, db_session, test_requisition, test_vendor_card):
    """POST to the offer-create API writes an offer_created activity row."""
    before = len(_activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED))
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/offers",
        json={
            "requirement_id": None,
            "vendor_card_id": test_vendor_card.id,
            "mpn": "LM317T",
            "vendor_name": test_vendor_card.display_name,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) == before + 1
```

Before writing this test, **verify the offer-create API route and payload schema** in `app/routers/crm/offers.py` (`create_offer()` decorator + its Pydantic body model). Adjust the URL and JSON body to match the actual route and required fields. If `create_offer()` requires fields not shown, add the minimum to make the request valid. If the route cannot be driven cleanly via `client`, instead call `create_offer()`'s logic through the lower-level path and assert the row — but prefer the route.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py -v --override-ini="addopts="`
Expected: FAIL — `assert len(rows) == before + 1` fails (no offer_created row written yet).

- [ ] **Step 3: Instrument `create_offer()` in `app/routers/crm/offers.py`**

Add the imports (match the file's style — it is `app/routers/crm/offers.py`, so 3-dot relative: `from ...constants import ActivityType`, `from ...services.activity_service import log_activity`). Then, immediately after the `db.commit()` near line 397 and before the function's return / side-effects, insert:

```python
        log_activity(
            db,
            activity_type=ActivityType.OFFER_CREATED,
            requisition_id=offer.requisition_id,
            requirement_id=offer.requirement_id,
            user_id=user.id,
            vendor_card_id=offer.vendor_card_id,
            description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
            details={"offer_id": offer.id, "source": offer.source},
        )
        db.commit()
```

(The extra `db.commit()` persists the activity row; `log_activity` only flushes.)

- [ ] **Step 4: Instrument `add_offer()` in `app/routers/htmx_views.py`**

Add the imports (match `app/routers/htmx_views.py` style — 2-dot relative: `from ..constants import ActivityType`, `from ..services.activity_service import log_activity`; check whether `log_activity` is already imported from Plan 1's change to this file and reuse it). Immediately after the `db.commit()` near line 1998, insert:

```python
    log_activity(
        db,
        activity_type=ActivityType.OFFER_CREATED,
        requisition_id=offer.requisition_id,
        requirement_id=offer.requirement_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
        details={"offer_id": offer.id, "source": offer.source},
    )
    db.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py tests/test_offers_overhaul.py -v --override-ini="addopts="`
Expected: PASS — new test passes; existing offer tests still pass.

- [ ] **Step 6: Commit**

```bash
git add app/routers/crm/offers.py app/routers/htmx_views.py tests/test_offer_activity_logging.py
git commit -m "feat: log offer_created activity from offer-create router paths"
```

---

### Task 2: `offer_created` — email-parsed and proactive-win offers

Instrument the two service paths that create offers from parsed vendor email and proactive-match conversion. Both create offers **in a loop** — log once per offer, inside the loop, after `db.flush()`.

**Sites:**
- `app/email_service.py` — `_auto_create_offers_from_parse(vr, parsed, db)`. `Offer(...)` ~line 1066 inside a loop over draft offers, `db.add` ~1089, `db.flush` ~1090. Scope: `db`, `vr.requisition_id`, requirement id via `mpn_to_req_id.get(mpn_key)`, **no `user_id`** (function has no user param → pass `user_id=None`), no vendor_card on the offer.
- `app/services/proactive_service.py` — `convert_proactive_to_win()`. `Offer(...)` ~line 457 inside a loop, `db.add` ~474, `db.flush` ~475. Scope: `db`, `user.id`, `req.id`, `requirement.id`, vendor_card via the cloned offer.

**Files:**
- Modify: `app/email_service.py`
- Modify: `app/services/proactive_service.py`
- Test: `tests/test_offer_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_offer_activity_logging.py`:

```python
from app.services.activity_service import log_activity  # noqa: F401  (ensures import path valid)


def test_email_parsed_offer_logs_offer_created(db_session, test_requisition, test_user):
    """An offer auto-created from a parsed vendor email writes offer_created."""
    from app.email_service import _auto_create_offers_from_parse
    from app.models.offers import VendorResponse

    vr = VendorResponse(
        requisition_id=test_requisition.id,
        from_email="vendor@example.com",
        subject="RE: RFQ",
        body_text="We can supply.",
    )
    db_session.add(vr)
    db_session.flush()

    parsed = {
        "offers": [
            {"vendor_name": "Vendor X", "mpn": "LM317T", "unit_price": 0.5, "qty_available": 100}
        ]
    }
    _auto_create_offers_from_parse(vr, parsed, db_session)
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1
```

Before writing, **verify**: the exact name and signature of the email-parsed offer-creation function in `app/email_service.py` (the dossier calls it `_auto_create_offers_from_parse(vr, parsed, db)` — confirm), the `VendorResponse` model's required non-nullable fields (adjust the constructor to satisfy them), and the shape of the `parsed` dict the function expects (read the function body). Fix the test to match reality. If the function cannot be unit-tested directly because of heavy dependencies, STOP and report NEEDS_CONTEXT.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py -k email_parsed -v --override-ini="addopts="`
Expected: FAIL — no `offer_created` row.

- [ ] **Step 3: Instrument the email-parsed loop in `app/email_service.py`**

Add imports (file is `app/email_service.py`: `from .constants import ActivityType`, `from .services.activity_service import log_activity`). Inside the offer-creation loop, immediately after `db.flush()` (~line 1090) and still inside the loop, insert:

```python
            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=offer.requisition_id,
                requirement_id=offer.requirement_id,
                user_id=None,
                vendor_card_id=offer.vendor_card_id,
                description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
                details={"offer_id": offer.id, "source": offer.source},
            )
```

- [ ] **Step 4: Instrument the proactive-win loop in `app/services/proactive_service.py`**

Add imports (file is `app/services/proactive_service.py`: `from ..constants import ActivityType`, `from .activity_service import log_activity`). Inside the loop, immediately after `db.flush()` (~line 475), insert (note the offer variable here is `new_offer`):

```python
            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=new_offer.requisition_id,
                requirement_id=new_offer.requirement_id,
                user_id=user.id,
                vendor_card_id=new_offer.vendor_card_id,
                description=f"Offer added: {new_offer.vendor_name} — {new_offer.mpn}",
                details={"offer_id": new_offer.id, "source": new_offer.source},
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py tests/ -k "proactive or email_parse" -v --override-ini="addopts="`
Expected: PASS — new test passes; existing proactive/email tests still pass.

- [ ] **Step 6: Commit**

```bash
git add app/email_service.py app/services/proactive_service.py tests/test_offer_activity_logging.py
git commit -m "feat: log offer_created for email-parsed and proactive-win offers"
```

---

### Task 3: `offer_created` — AI offer service (`save_parsed_offers`, `save_freeform_offers`)

Both functions in `app/services/ai_offer_service.py` create offers in a loop. Same pattern as Task 2.

**Sites:**
- `save_parsed_offers()` — `Offer(...)` ~line 162 in a loop, `db.add` ~185, `db.flush` ~186. Scope: `db`, `user_id` (param), `requisition_id` (param), `req_id` (matched requirement), no vendor_card.
- `save_freeform_offers()` — `Offer(...)` ~line 299 in a loop, `db.add` ~322, `db.flush` ~323. Scope: `db`, `user_id` (param), `requisition_id` (param), `req_id`, `card.id` (vendor_card).

**Files:**
- Modify: `app/services/ai_offer_service.py`
- Test: `tests/test_offer_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_offer_activity_logging.py`:

```python
def test_save_parsed_offers_logs_offer_created(db_session, test_requisition, test_user):
    """save_parsed_offers writes one offer_created row per saved offer."""
    from app.services.ai_offer_service import save_parsed_offers

    save_parsed_offers(
        db=db_session,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        offers=_one_parsed_offer(),
    )
    db_session.commit()
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1
```

Before writing, **verify** the exact signature of `save_parsed_offers` (param names and order — the dossier says `db, requisition_id, user_id, offers`; confirm) and the exact type each parsed offer must be (a Pydantic model? a dict? read the function — it iterates `offers` and reads `o.mpn`, `o.vendor_name`). Write a small `_one_parsed_offer()` helper in the test file that builds one valid parsed-offer object of the correct type. If `save_parsed_offers` needs a material-card lookup or other setup, provide the minimum. Adjust the test to the real signature.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py -k save_parsed -v --override-ini="addopts="`
Expected: FAIL — no `offer_created` row.

- [ ] **Step 3: Instrument both loops in `app/services/ai_offer_service.py`**

Add imports (`from ..constants import ActivityType`, `from .activity_service import log_activity`). In **both** `save_parsed_offers()` and `save_freeform_offers()`, immediately after the per-offer `db.flush()` and still inside the loop, insert:

```python
            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=offer.requisition_id,
                requirement_id=offer.requirement_id,
                user_id=user_id,
                vendor_card_id=offer.vendor_card_id,
                description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
                details={"offer_id": offer.id, "source": offer.source},
            )
```

(`offer` is the loop variable in both functions; `user_id` is a parameter of both.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py tests/ -k "ai_offer or save_parsed or save_freeform" -v --override-ini="addopts="`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/ai_offer_service.py tests/test_offer_activity_logging.py
git commit -m "feat: log offer_created from AI offer service (parsed + freeform)"
```

---

### Task 4: `offer_created` — excess matching and requisition clone/duplicate

Four loop-based service sites. Each creates offers without a per-iteration `db.flush()` — **add `db.flush()` after `db.add(...)`** so `offer.id` is set before `log_activity`.

**Sites:**
- `app/services/excess_service.py` — `apply_excess_list_to_requirements()`: `Offer(...)` ~line 405, `db.add` ~421 (nested loop). Scope: `db`, `user_id` (param), `req.requisition_id`, `req.id`, no vendor_card.
- `app/services/excess_service.py` — `create_proactive_matches_for_excess()`: `Offer(...)` ~line 647, `db.add` ~663, `db.flush` ~664 (has flush). Scope: `db`, `user_id` (param), `req.requisition_id`, `req.id`, no vendor_card.
- `app/routers/crm/clone.py` — `clone_requisition()`: `Offer(...)` ~line 73 (var `new_o`), `db.add` ~93, `db.commit` ~94. Scope: `db`, `user.id`, `new_req.id`, requirement id via `req_map.get(...)`, `o.vendor_card_id`.
- `app/services/requisition_service.py` — `duplicate_requisition()`: `Offer(...)` ~line 136 (var `new_o`), `db.add` ~156, `safe_commit` ~158. Scope: `db`, `user_id` (param), `new_req.id`, requirement via `req_map.get(...)`, `o.vendor_card_id`.

**Files:**
- Modify: `app/services/excess_service.py`
- Modify: `app/routers/crm/clone.py`
- Modify: `app/services/requisition_service.py`
- Test: `tests/test_offer_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_offer_activity_logging.py`:

```python
def test_clone_requisition_logs_offer_created(db_session, test_requisition, test_user, test_offer):
    """Cloning a requisition that has offers logs offer_created for each cloned offer."""
    from app.services.requisition_service import duplicate_requisition

    before = db_session.query(ActivityLog).filter(
        ActivityLog.activity_type == ActivityType.OFFER_CREATED
    ).count()
    new_req = duplicate_requisition(db=db_session, source_req_id=test_requisition.id, user_id=test_user.id)
    db_session.commit()
    after = db_session.query(ActivityLog).filter(
        ActivityLog.activity_type == ActivityType.OFFER_CREATED
    ).count()
    assert after > before
    rows = _activity_rows(db_session, new_req.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1
```

Before writing, **verify**: the `duplicate_requisition` signature (the dossier says `db, source_req_id, user_id` — confirm param names), that the `test_offer` fixture's offer is attached to `test_requisition` (check `tests/conftest.py` ~line 384), and that cloning copies offers. Adjust the test to reality. If `duplicate_requisition` does not copy offers when none match, ensure `test_offer` is wired so the clone path runs.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py -k clone_requisition -v --override-ini="addopts="`
Expected: FAIL — no `offer_created` row for the cloned requisition.

- [ ] **Step 3: Instrument the four sites**

For each site, add the file's imports (`ActivityType` from constants, `log_activity` from activity_service — match each file's relative-import style). At each creation loop, ensure `db.flush()` runs after `db.add(...)` (add it where missing — sites in `excess_service.apply_excess_list_to_requirements`, `clone.py`, `requisition_service.py` lack a per-offer flush), then insert immediately after the flush, inside the loop:

```python
            db.flush()  # ensure offer.id is set (add only where no per-offer flush exists)
            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=<offer var>.requisition_id,
                requirement_id=<offer var>.requirement_id,
                user_id=<user.id or user_id>,
                vendor_card_id=<offer var>.vendor_card_id,
                description=f"Offer added: {<offer var>.vendor_name} — {<offer var>.mpn}",
                details={"offer_id": <offer var>.id, "source": <offer var>.source},
            )
```

Substitute the real loop variable per site: `offer` in `excess_service.py` (both functions), `new_o` in `clone.py` and `requisition_service.py`. Use `user_id` (the param) in the service functions and `user.id` in `clone.py`. Do **not** add a redundant `db.flush()` in `create_proactive_matches_for_excess()` — it already flushes per offer.

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py tests/ -k "excess or clone or duplicate or requisition_service" -v --override-ini="addopts="`
Expected: PASS — new test passes; existing excess/clone tests still pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/excess_service.py app/routers/crm/clone.py app/services/requisition_service.py tests/test_offer_activity_logging.py
git commit -m "feat: log offer_created from excess-match and requisition-clone paths"
```

---

### Task 5: `offer_status_changed` — `crm/offers.py` (5 sites)

Instrument the five offer-status mutations in `app/routers/crm/offers.py`. Every transition logs `ActivityType.OFFER_STATUS_CHANGED`.

**Sites (all in `app/routers/crm/offers.py`):**
- `approve_offer()` — `offer.status = OfferStatus.ACTIVE` ~line 623; `old_status` already captured ~621.
- `reject_offer()` — `offer.status = OfferStatus.REJECTED` ~line 648; `old_status` already captured ~646.
- `mark_offer_sold()` — `offer.status = OfferStatus.SOLD` ~line 677; `old_status` already captured ~675.
- `promote_offer()` — `offer.status = OfferStatus.ACTIVE` ~line 984; **no `old_status` captured — add one** before the status line.
- `reject_offer_t4_review()` — `offer.status = OfferStatus.REJECTED` ~line 1011; **no `old_status` — add one** before the status line.

**Files:**
- Modify: `app/routers/crm/offers.py`
- Test: `tests/test_offer_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_offer_activity_logging.py`:

```python
def test_approve_offer_logs_status_changed(client, db_session, test_requisition, test_offer):
    """Approving an offer writes an offer_status_changed activity row."""
    # test_offer must be in pending_review for approve to be allowed
    test_offer.status = "pending_review"
    db_session.commit()
    resp = client.post(f"/api/offers/{test_offer.id}/approve")
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED)
    assert len(rows) == 1
    assert "status:" in (rows[0].notes or "")
```

Before writing, **verify** the approve route path and method in `app/routers/crm/offers.py` (`approve_offer()` decorator), and that the `test_offer` fixture (`tests/conftest.py` ~line 384) belongs to `test_requisition`. Adjust URL/body to match. Confirm `record_changes`/transition validation will accept the `pending_review → active` move for the fixture offer.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py -k approve_offer -v --override-ini="addopts="`
Expected: FAIL — no `offer_status_changed` row.

- [ ] **Step 3: Instrument the five functions**

Add imports if not already present (`from ...constants import ActivityType`, `from ...services.activity_service import log_activity`).

For `promote_offer()` and `reject_offer_t4_review()`, **first add `old_status` capture** immediately before the `offer.status = ...` line:

```python
    old_status = offer.status
```

Then in **all five** functions, immediately after the `offer.status = <new>` assignment and before the function's `db.commit()`, insert:

```python
        log_activity(
            db,
            activity_type=ActivityType.OFFER_STATUS_CHANGED,
            requisition_id=offer.requisition_id,
            user_id=user.id,
            vendor_card_id=offer.vendor_card_id,
            description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
            details={
                "offer_id": offer.id,
                "old_status": str(old_status),
                "new_status": str(offer.status),
            },
        )
```

(`offer.status` is the new value, already assigned. Indentation: match each function's body.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py tests/test_offers_overhaul.py tests/test_sprint2_offer_mgmt.py -v --override-ini="addopts="`
Expected: PASS — new test passes; existing offer tests still pass.

- [ ] **Step 5: Commit**

```bash
git add app/routers/crm/offers.py tests/test_offer_activity_logging.py
git commit -m "feat: log offer_status_changed from crm/offers.py status mutations"
```

---

### Task 6: `offer_status_changed` — `htmx_views.py` (5 sites) + APP_MAP doc

Instrument the five offer-status mutations in `app/routers/htmx_views.py`, then update the APP_MAP doc.

**Sites (all in `app/routers/htmx_views.py`):**
- `review_offer()` — one if/else: `offer.status = OfferStatus.APPROVED` (~1915) / `OfferStatus.REJECTED` (~1921), shared `db.commit()` ~1923. **No `old_status` — capture once before the if/else.** Log once after the if/else.
- `mark_offer_sold_htmx()` — `offer.status = OfferStatus.SOLD` ~line 2173; `old_status` already captured ~2171.
- `promote_offer_htmx()` — `offer.status = OfferStatus.ACTIVE` ~line 2227; **no `old_status` — add one.**
- `reject_offer_htmx()` — `offer.status = OfferStatus.REJECTED` ~line 2253; **no `old_status` — add one.**

Note: these are separate UI handlers; the `crm/offers.py` set (Task 5) are the JSON-API equivalents. Both code paths exist; instrument both — an action taken through either path must appear on the timeline. Do not attempt to consolidate the two implementations in this plan.

**Files:**
- Modify: `app/routers/htmx_views.py`
- Modify: `docs/APP_MAP_INTERACTIONS.md`
- Test: `tests/test_offer_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_offer_activity_logging.py`:

```python
def test_review_offer_htmx_logs_status_changed(client, db_session, test_requisition, test_offer):
    """Approving an offer through the HTMX review handler logs offer_status_changed."""
    test_offer.status = "pending_review"
    db_session.commit()
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/offers/{test_offer.id}/review",
        data={"action": "approve"},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED)
    assert len(rows) == 1
```

Before writing, **verify** the `review_offer()` route path, method, and form/query param name for the approve/reject action in `app/routers/htmx_views.py` (the dossier shows it branches on an `action` variable — confirm whether `action` comes from a form field, query param, or path). Adjust the request to match.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py -k review_offer_htmx -v --override-ini="addopts="`
Expected: FAIL — no `offer_status_changed` row.

- [ ] **Step 3: Instrument the four htmx handlers**

Add imports if not already present (`from ..constants import ActivityType`, `from ..services.activity_service import log_activity` — reuse if Plan 1 already imported `log_activity` here).

For `review_offer()`: add `old_status = offer.status` before the `if action == "approve":` branch; after the if/else block and before the shared `db.commit()`, insert one call:

```python
    log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )
```

For `mark_offer_sold_htmx()`, `promote_offer_htmx()`, `reject_offer_htmx()`: capture `old_status = offer.status` before the status assignment where it is missing (promote, reject; sold already has it), then insert the same `log_activity(...)` block (above) after the status assignment and before that handler's `db.commit()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offer_activity_logging.py -v --override-ini="addopts="`
Expected: PASS — all tests in the file pass.

- [ ] **Step 5: Update the APP_MAP doc**

In `docs/APP_MAP_INTERACTIONS.md`, find the activity-logging section (the Plan 1 note about `log_activity()` / `get_requisition_activities()`). Add a sentence: offer creation and offer status changes now route through `activity_service.log_activity()` (`ActivityType.OFFER_CREATED` / `ActivityType.OFFER_STATUS_CHANGED`) so offer events appear on the requisition Activity tab. Match the doc's existing prose style; do not restructure.

- [ ] **Step 6: Run the full offer + activity suite + lint, then commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "offer or activity" -v --override-ini="addopts="
ruff check app/routers/crm/offers.py app/routers/htmx_views.py app/email_service.py app/services/proactive_service.py app/services/ai_offer_service.py app/services/excess_service.py app/routers/crm/clone.py app/services/requisition_service.py
git add app/routers/htmx_views.py docs/APP_MAP_INTERACTIONS.md tests/test_offer_activity_logging.py
git commit -m "feat: log offer_status_changed from htmx offer handlers"
```

Expected: all offer/activity-tagged tests pass; ruff reports no errors.

---

## Self-Review

**Spec coverage (build step 2 — offers portion):**
- `offer_created` at every creation site → Tasks 1-4 cover all 10 sites (the spec's 11th, `import_ai_offers`, does not exist in the codebase) ✓
- `offer_status_changed` at every status mutation → Tasks 5-6 cover all 10 sites ✓
- All writes route through the canonical `log_activity()` ✓
- Tests alongside each task ✓

**Placeholder scan:** Task 4's Step 3 uses `<offer var>` / `<user.id or user_id>` placeholders deliberately — the per-site substitution is spelled out in the sentence immediately after the code block (loop var: `offer` for excess, `new_o` for clone/duplicate; actor: `user_id` param in services, `user.id` in `clone.py`). Every other code step is complete.

**Type consistency:** `log_activity()` is called with the exact keyword args from its Plan 1 signature (`db` positional; `activity_type`, `requisition_id`, `requirement_id`, `user_id`, `vendor_card_id`, `description`, `details` keyword). `ActivityType.OFFER_CREATED` / `ActivityType.OFFER_STATUS_CHANGED` are the Plan 1 enum members. `details` is a plain `dict` as the signature expects.

**Scope:** Plan 2a is offers only. Tasks/assignment/archive/notes are Plan 2b. Sightings (with batch aggregation) are deferred to Plan 4 per the design decision of 2026-05-21. The `OfferStatus.APPROVED` vs `ACTIVE` inconsistency between the two approve paths is pre-existing and out of scope — `offer_status_changed` logs whatever transition actually occurred.

**No migration:** confirmed — reuses existing `activity_log` columns.
