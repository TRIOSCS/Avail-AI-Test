# Search-button-only sourcing — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every automatic sourcing trigger. The only path to a connector call is a user clicking the per-row refresh icon or the detail-panel "Search" button on `/v2/sightings`. Enforce a 48-hour per-normalized-MPN cooldown via `MaterialCard.last_searched_at`.

**Architecture:** A cooldown helper partitions a requirement's MPNs (primary + substitutes) into `to_search` and `cached` lists. `search_requirement` invokes connectors only for `to_search` MPNs, enqueues ICS+NC for the same, stamps `MaterialCard.last_searched_at`, and returns a per-MPN result map. The detail panel queries sightings by `material_card_id IN (...)` for cross-requirement visibility. Today's row-click POST `/refresh` reverts to GET `/detail` only. The 3 AM cron, requirement-creation auto-enqueue + auto background search, and v1 `/api/requirements/.../search*` routes are deleted entirely.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, HTMX, Alpine.js, pytest, loguru. Spec: `docs/superpowers/specs/2026-05-14-search-button-only-sourcing-design.md`.

---

## File Structure

**Modify:**
- `app/search_service.py` — add `_mpn_cooldown_partition()`, refactor `search_requirement()` to honor cooldown + enqueue ICS/NC, stamp `MaterialCard.last_searched_at` per searched MPN, return per-MPN result map
- `app/routers/sightings.py` — remove `REFRESH_RATE_LIMIT_SECONDS` + `_within_rate_limit()`; modify `sightings_refresh` to surface per-MPN toast; modify `sightings_detail` to query by `material_card_id` set instead of `requirement_id`
- `app/services/sighting_aggregation.py` — already correct after today's fix; verify it still works when the input sighting set comes from cross-MPN material_card_id query
- `app/templates/htmx/partials/sightings/list.html` — revert today's `selectReq`: GET `/detail` only, `clickPending += 1`
- `app/templates/htmx/partials/sightings/table.html` — promote per-row refresh icon to always-visible
- `app/routers/requisitions/requirements.py` — delete `_enqueue_ics_nc_batch`, `_nc_enqueue_batch`, `_ics_enqueue_batch`, `_bg_full_search`; delete the two `background_tasks.add_task` calls at requirement creation; delete the two legacy routes `search_one` (`/api/requirements/{item_id}/search`) and the search-all batch route
- `app/jobs/__init__.py` — remove `register_sourcing_refresh_jobs` import + call
- `app/startup.py` — add idempotent seed: flip `api_sources.icsource` and `api_sources.netcomponents` to `status='live', is_active=true`; insert `ics_worker_status` singleton row if absent

**Delete:**
- `app/jobs/sourcing_refresh_jobs.py` — file gone, daily 3 AM cron removed

**Create:**
- `tests/test_search_service_cooldown.py` — unit tests for `_mpn_cooldown_partition` + integration tests for `search_requirement` honoring the cooldown

**Modify (tests):**
- `tests/test_routers_sightings.py` — invert today's `TestSightingsListTemplateSelectReqShape` assertions (row click fires GET `/detail`, NOT POST `/refresh`); add per-MPN toast tests; remove `REFRESH_RATE_LIMIT_SECONDS`-related tests
- `tests/test_routers_requirements.py` (if exists) — assert deleted endpoints return 404
- `tests/test_startup.py` — assert `api_sources.icsource`/`netcomponents` rows are live + active after startup; assert `ics_worker_status` singleton seeded
- `tests/test_jobs_init.py` (or wherever scheduler registration is tested) — assert `refresh_stale_requisitions` job is NOT registered

**Update docs:**
- `docs/APP_MAP_INTERACTIONS.md` — update Section 2 (search flow); add cooldown narrative; remove auto-enqueue diagram bits
- `docs/APP_MAP_ARCHITECTURE.md` — drop the "auto-search" mention; replace with "user-initiated only"
- `docs/htmx-conventions.md` — flip the click-to-refresh convention back: row click = read only; explicit Search button = write

---

## Task 1: Per-MPN cooldown helper

**Files:**
- Create: `tests/test_search_service_cooldown.py`
- Modify: `app/search_service.py` (add helper near top of module, just below `get_all_pns`)

- [ ] **Step 1: Write failing test**

```python
# tests/test_search_service_cooldown.py
"""Tests for the per-normalized-MPN 48h cooldown helper used by search_requirement.

Called by: pytest
Depends on: app.search_service._mpn_cooldown_partition, MaterialCard
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.search_service import _mpn_cooldown_partition


def _mk_card(db: Session, mpn: str, last_searched_at):
    card = MaterialCard(
        primary_mpn=mpn,
        normalized_mpn=mpn.upper(),
        last_searched_at=last_searched_at,
    )
    db.add(card)
    db.flush()
    return card


class TestMpnCooldownPartition:
    def test_partitions_stale_and_fresh_mpns(self, db_session: Session):
        now = datetime.now(timezone.utc)
        fresh_dt = now - timedelta(hours=12)
        stale_dt = now - timedelta(hours=72)
        _mk_card(db_session, "FRESHMPN", fresh_dt)
        _mk_card(db_session, "STALEMPN", stale_dt)
        db_session.commit()

        to_search, cached_ids = _mpn_cooldown_partition(
            db_session, ["FRESHMPN", "STALEMPN", "NEWMPN"], now=now
        )

        # STALEMPN (>=48h) and NEWMPN (no card) get searched
        assert set(to_search) == {"STALEMPN", "NEWMPN"}
        # FRESHMPN keeps its card.id in cached_ids so detail panel can still
        # surface those sightings
        cached_card = (
            db_session.query(MaterialCard).filter_by(normalized_mpn="FRESHMPN").first()
        )
        assert cached_ids == [cached_card.id]

    def test_null_last_searched_at_is_treated_as_never_searched(
        self, db_session: Session
    ):
        now = datetime.now(timezone.utc)
        _mk_card(db_session, "NULLMPN", None)
        db_session.commit()

        to_search, cached_ids = _mpn_cooldown_partition(
            db_session, ["NULLMPN"], now=now
        )

        assert to_search == ["NULLMPN"]
        assert cached_ids == []

    def test_exactly_48h_boundary_is_searched(self, db_session: Session):
        now = datetime.now(timezone.utc)
        # exactly 48h ago — should be searched (>= 48h)
        _mk_card(db_session, "BOUNDARYMPN", now - timedelta(hours=48))
        db_session.commit()

        to_search, cached_ids = _mpn_cooldown_partition(
            db_session, ["BOUNDARYMPN"], now=now
        )

        assert to_search == ["BOUNDARYMPN"]
```

- [ ] **Step 2: Run test, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_search_service_cooldown.py -v --override-ini="addopts="
```

Expected: `ImportError: cannot import name '_mpn_cooldown_partition' from 'app.search_service'`

- [ ] **Step 3: Implement helper**

Add to `app/search_service.py` immediately after `get_all_pns` (around line 178):

```python
# How long a MaterialCard.last_searched_at "shields" its MPN from being
# re-queried at supplier APIs. Per-MPN, not per-requirement, so two
# requirements that share an MPN don't each burn quota.
MPN_COOLDOWN_HOURS: Final[int] = 48


def _mpn_cooldown_partition(
    db: Session,
    pns: list[str],
    now: datetime | None = None,
) -> tuple[list[str], list[int]]:
    """Split a requirement's MPNs into (to_search, cached_card_ids).

    A display MPN goes into ``to_search`` when its MaterialCard either does
    not exist or has ``last_searched_at`` older than ``MPN_COOLDOWN_HOURS``.
    Otherwise its card id goes into ``cached_card_ids`` so the caller can
    surface existing sightings via material_card_id linkage.

    Lookups use ``normalize_mpn_key`` so case + packaging-suffix variations
    don't escape the cooldown.
    """
    if not pns:
        return [], []

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MPN_COOLDOWN_HOURS)

    keys_in_order = []
    key_to_display: dict[str, str] = {}
    for pn in pns:
        k = normalize_mpn_key(pn)
        if not k or k in key_to_display:
            continue
        keys_in_order.append(k)
        key_to_display[k] = pn

    cards = (
        db.query(MaterialCard)
        .filter(MaterialCard.normalized_mpn.in_(keys_in_order))
        .all()
    )
    card_by_key = {c.normalized_mpn: c for c in cards}

    to_search: list[str] = []
    cached_ids: list[int] = []
    for key in keys_in_order:
        card = card_by_key.get(key)
        if card is None or card.last_searched_at is None or card.last_searched_at < cutoff:
            to_search.append(key_to_display[key])
        else:
            cached_ids.append(card.id)
    return to_search, cached_ids
```

Also add the `timedelta` import if not present (check existing `from datetime import ...` line).

- [ ] **Step 4: Run test, confirm pass**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_search_service_cooldown.py::TestMpnCooldownPartition -v --override-ini="addopts="
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_search_service_cooldown.py app/search_service.py
git commit -m "feat(search): add 48h per-MPN cooldown partition helper"
```

---

## Task 2: search_requirement honors cooldown + stamps MaterialCard

**Files:**
- Modify: `app/search_service.py` (`search_requirement` function, around lines 181-272)
- Modify: `tests/test_search_service_cooldown.py` — add integration test

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_search_service_cooldown.py`:

```python
from unittest.mock import AsyncMock, patch

from app.models import Requirement, Requisition
from app.search_service import search_requirement


class TestSearchRequirementCooldown:
    async def test_only_stale_mpns_hit_connectors(
        self, db_session: Session, test_user
    ):
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-1",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="STALEMPN",
            substitutes=[{"mpn": "FRESHMPN"}],
            created_at=now,
        )
        db_session.add(item)
        db_session.flush()

        # FRESHMPN already searched 12h ago → should be skipped
        _mk_card(db_session, "FRESHMPN", now - timedelta(hours=12))
        # STALEMPN has no card → should be searched
        db_session.commit()

        with patch(
            "app.search_service._fetch_fresh",
            new=AsyncMock(return_value=([], [])),
        ) as fetch_mock:
            result = await search_requirement(item, db_session)

        # _fetch_fresh called with exactly ["STALEMPN"] (FRESHMPN excluded)
        assert fetch_mock.call_count == 1
        called_pns = fetch_mock.call_args[0][0]
        assert called_pns == ["STALEMPN"]

        # Returned per-MPN map reflects partition
        assert result["mpn_results"] == {
            "STALEMPN": "searched",
            "FRESHMPN": "cached",
        }

    async def test_searched_mpn_card_last_searched_at_updates(
        self, db_session: Session, test_user
    ):
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-2",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="NEWMPN",
            created_at=now,
        )
        db_session.add(item)
        db_session.commit()

        with patch(
            "app.search_service._fetch_fresh",
            new=AsyncMock(return_value=([], [])),
        ):
            await search_requirement(item, db_session)

        card = (
            db_session.query(MaterialCard)
            .filter_by(normalized_mpn="NEWMPN")
            .first()
        )
        assert card is not None
        assert card.last_searched_at is not None
        assert (now - card.last_searched_at).total_seconds() < 60
```

- [ ] **Step 2: Run, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_search_service_cooldown.py::TestSearchRequirementCooldown -v --override-ini="addopts="
```

Expected: AssertionError on `result["mpn_results"]` key missing (or `_fetch_fresh` called with all MPNs).

- [ ] **Step 3: Refactor search_requirement to honor cooldown**

In `app/search_service.py` replace `search_requirement` (around lines 181-272). Key changes:
1. After `pns = get_all_pns(req)`, partition via cooldown helper.
2. Only pass `to_search` MPNs to `_fetch_fresh`.
3. After the existing material-card upsert block, set `card.last_searched_at = now` for every card whose normalized_mpn is in `to_search`.
4. Build `mpn_results: dict[str, str]` mapping each display MPN to `"searched"` or `"cached"`.
5. Include `mpn_results` in the returned dict.

```python
async def search_requirement(req: Requirement, db: Session) -> dict:
    """Search APIs for stale MPNs only; surface cached sightings for fresh ones.

    Returns {"sightings": [...], "source_stats": [...], "mpn_results": {mpn: "searched"|"cached"}}.
    """
    pns = get_all_pns(req)
    if not pns:
        return {"sightings": [], "source_stats": [], "mpn_results": {}}

    now = datetime.now(timezone.utc)
    to_search, cached_card_ids = _mpn_cooldown_partition(db, pns, now=now)

    mpn_results: dict[str, str] = {}
    for pn in pns:
        key = normalize_mpn_key(pn)
        if any(normalize_mpn_key(t) == key for t in to_search):
            mpn_results[pn] = "searched"
        else:
            mpn_results[pn] = "cached"

    # Short-circuit: every MPN is within cooldown — no connector calls.
    if not to_search:
        # Surface cached sightings via material_card_id linkage in caller; the
        # detail panel composes them from the requirement's primary card +
        # substitute cards. Return empty fresh-sightings list.
        return {"sightings": [], "source_stats": [], "mpn_results": mpn_results}

    async def _fetch_affinity():
        try:
            return find_vendor_affinity(to_search[0], db)
        except Exception as e:
            logger.warning("Vendor affinity lookup failed for {}: {}", to_search[0], e)
            return []

    fresh_task = _fetch_fresh(to_search, db)
    affinity_task = _fetch_affinity()
    (fresh, source_stats), affinity_matches = await asyncio.gather(fresh_task, affinity_task)

    from sqlalchemy.orm import sessionmaker

    req_id = req.id
    _WriteSession = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False, expire_on_commit=False)
    write_db = _WriteSession()
    try:
        write_req = write_db.get(Requirement, req_id)
        if not write_req:
            logger.error("Requirement {} not found in write session", req_id)
            return {"sightings": [], "source_stats": source_stats, "mpn_results": mpn_results}

        succeeded_sources = {
            stat["source"]
            for stat in source_stats
            if stat["status"] == SourceRunStatus.OK.value and not stat.get("error")
        }
        sightings = _save_sightings(fresh, write_req, write_db, succeeded_sources)
        logger.info("Req {} ({}): {} fresh sightings", req_id, to_search[0], len(sightings))

        # Material card upsert + stamp last_searched_at per searched MPN
        card_ids = set()
        primary_card_id = None
        searched_keys = {normalize_mpn_key(m) for m in to_search}
        for pn in to_search:
            try:
                card = _upsert_material_card(pn, sightings, write_db, now)
                if card:
                    card_ids.add(card.id)
                    # Stamp cooldown on every searched MPN's card
                    if card.normalized_mpn in searched_keys:
                        card.last_searched_at = now
                    if pn == to_search[0] and not primary_card_id:
                        primary_card_id = card.id
            except Exception as e:
                logger.error("MATERIAL_CARD_UPSERT_FAIL: mpn={} error={}", pn, e)
                write_db.rollback()

        # Link requirement to primary card if not yet linked
        if primary_card_id and not write_req.material_card_id:
            write_req.material_card_id = primary_card_id

        await _schedule_background_enrichment(card_ids, write_db)
        fresh_vendors = {s.vendor_name.lower() for s in sightings if s.vendor_name}
        history = _get_material_history(list(card_ids), fresh_vendors, write_db)

        # Requirement.last_searched_at: display-only field kept for now.
        # It is stamped here for back-compat with the table.html "stale" badge
        # logic until that surface is migrated to per-MPN.
        if succeeded_sources:
            write_req.last_searched_at = now
        write_db.commit()

        for s in sightings:
            write_db.expunge(s)
    except Exception:
        write_db.rollback()
        raise
    finally:
        write_db.close()

    results = []
    for s in sightings:
        d = sighting_to_dict(s)
        d["is_historical"] = False
        d["is_material_history"] = False
        results.append(d)
    for h in history:
        results.append(_history_to_result(h, now))

    live_vendors = {r.get("vendor_name", "").lower() for r in results}
    for match in affinity_matches:
        vendor_lower = match.get("vendor_name", "").lower()
        if vendor_lower in live_vendors:
            continue
        live_vendors.add(vendor_lower)
        results.append(match)

    return {"sightings": results, "source_stats": source_stats, "mpn_results": mpn_results}
```

- [ ] **Step 4: Run tests, confirm pass**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_search_service_cooldown.py -v --override-ini="addopts="
```

Expected: all 5 tests pass.

- [ ] **Step 5: Run adjacent test suites to confirm no regression**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_search_service_nightly.py tests/test_sighting_aggregation.py -q --override-ini="addopts="
```

Expected: all existing tests still pass. If any fail because they assumed `_fetch_fresh` was called for every MPN regardless of cooldown, update those tests' MaterialCard fixtures to set `last_searched_at=None` (or remove the card) so the cooldown short-circuit doesn't fire — record each change in the commit message.

- [ ] **Step 6: Commit**

```bash
git add app/search_service.py tests/test_search_service_cooldown.py tests/test_search_service_nightly.py tests/test_sighting_aggregation.py
git commit -m "feat(search): search_requirement honors 48h per-MPN cooldown"
```

---

## Task 3: Enqueue ICS+NC alongside connector search

**Files:**
- Modify: `app/search_service.py` (inside `search_requirement`, after the material-card upsert block)
- Modify: `tests/test_search_service_cooldown.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_search_service_cooldown.py`:

```python
class TestIcsNcEnqueueOnRefresh:
    async def test_enqueues_ics_and_nc_for_each_searched_mpn(
        self, db_session: Session, test_user
    ):
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-3", customer_name="C",
            status="active", created_by=test_user.id, created_at=now,
        )
        db_session.add(req); db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="EM1",
            substitutes=[{"mpn": "EM2"}],
            created_at=now,
        )
        db_session.add(item); db_session.commit()

        with patch(
            "app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))
        ), patch(
            "app.services.ics_worker.queue_manager.enqueue_for_ics_search"
        ) as ics_mock, patch(
            "app.services.nc_worker.queue_manager.enqueue_for_nc_search"
        ) as nc_mock:
            await search_requirement(item, db_session)

        assert ics_mock.call_count == 2
        assert nc_mock.call_count == 2
        # Called with (requirement_id, db_session)
        for m in (ics_mock, nc_mock):
            requirement_ids = [c.args[0] for c in m.call_args_list]
            assert requirement_ids == [item.id, item.id]
```

- [ ] **Step 2: Run, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_search_service_cooldown.py::TestIcsNcEnqueueOnRefresh -v --override-ini="addopts="
```

Expected: `ics_mock.call_count == 0`.

- [ ] **Step 3: Add ICS+NC enqueue inside the write_db block in search_requirement**

Inside `search_requirement` (right after `await _schedule_background_enrichment(card_ids, write_db)`), add:

```python
        # Browser-automation workers: enqueue per searched MPN. They have
        # internal dedup (`recent` lookup via normalized_mpn within the
        # worker's dedup window), so duplicate enqueues for the same MPN on
        # multiple requirements collapse to a single search and link existing
        # sightings to each requirement's primary material card.
        for pn in to_search:
            try:
                from app.services.ics_worker.queue_manager import enqueue_for_ics_search

                enqueue_for_ics_search(req_id, write_db)
            except Exception:
                logger.warning("ICS enqueue failed for requirement {}", req_id, exc_info=True)
            try:
                from app.services.nc_worker.queue_manager import enqueue_for_nc_search

                enqueue_for_nc_search(req_id, write_db)
            except Exception:
                logger.warning("NC enqueue failed for requirement {}", req_id, exc_info=True)
```

Note: `enqueue_for_*_search` takes `(requirement_id, db)` — it reads the requirement to get the MPN to search. Calling it once per `to_search` MPN with the same `req_id` is correct because the dedup logic at the queue level guards against true duplicates; the calls are idempotent.

- [ ] **Step 4: Run, confirm pass**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_search_service_cooldown.py -v --override-ini="addopts="
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/search_service.py tests/test_search_service_cooldown.py
git commit -m "feat(search): enqueue ICS+NC per searched MPN in search_requirement"
```

---

## Task 4: Detail panel queries by material_card_id (cross-MPN visibility)

**Files:**
- Modify: `app/routers/sightings.py` (`sightings_detail` function, around lines 394-...)
- Modify: `tests/test_routers_sightings.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_routers_sightings.py`:

```python
class TestCrossMpnSightingVisibility:
    """Detail panel surfaces sightings linked via material_card_id from
    prior searches on other requirements that share the same primary or
    substitute MPN.
    """

    def test_detail_shows_sightings_from_other_req_via_material_card(
        self, client, db_session, test_user_auth
    ):
        from app.models import (
            MaterialCard, Requirement, Requisition, Sighting,
            VendorSightingSummary,
        )

        # Two requisitions, two requirements, but both point at the same MPN
        # via a shared MaterialCard.
        req1 = Requisition(name="R1", customer_name="C", status="active",
                           created_by=test_user_auth.id, created_at=datetime.utcnow())
        req2 = Requisition(name="R2", customer_name="C", status="active",
                           created_by=test_user_auth.id, created_at=datetime.utcnow())
        db_session.add_all([req1, req2]); db_session.flush()

        card = MaterialCard(primary_mpn="SHARED", normalized_mpn="SHARED")
        db_session.add(card); db_session.flush()

        item1 = Requirement(requisition_id=req1.id, primary_mpn="SHARED",
                            material_card_id=card.id, created_at=datetime.utcnow())
        item2 = Requirement(requisition_id=req2.id, primary_mpn="SHARED",
                            material_card_id=card.id, created_at=datetime.utcnow())
        db_session.add_all([item1, item2]); db_session.flush()

        # Sighting created during req1's search — linked to material_card,
        # NOT to req2's requirement_id directly.
        s = Sighting(
            requirement_id=item1.id,
            material_card_id=card.id,
            vendor_name="DigiKey",
            normalized_mpn="SHARED",
            source_type="api",
            unit_price=1.0,
            qty_available=100,
            created_at=datetime.utcnow(),
        )
        db_session.add(s); db_session.commit()

        # Rebuild summaries so detail panel has rows to render
        from app.services.sighting_aggregation import rebuild_vendor_summaries
        rebuild_vendor_summaries(db_session, item2.id)
        db_session.commit()

        # GET /detail for item2 — should include DigiKey via card linkage
        resp = client.get(f"/v2/partials/sightings/{item2.id}/detail")
        assert resp.status_code == 200
        assert "DigiKey" in resp.text
```

The summary rebuild path needs to know how to find sightings via material_card too. Section "Step 3" below adjusts `rebuild_vendor_summaries` to include cross-card sightings.

- [ ] **Step 2: Run, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestCrossMpnSightingVisibility -v --override-ini="addopts="
```

Expected: 200 but "DigiKey" not in response body (current behavior queries by requirement_id only).

- [ ] **Step 3: Extend rebuild_vendor_summaries to walk material_card_id set**

In `app/services/sighting_aggregation.py`, update `rebuild_vendor_summaries` (lines 78-186). Replace the initial `query = db.query(Sighting).filter(Sighting.requirement_id == requirement_id, ...)` with a query that pulls sightings linked to ANY of the requirement's material cards (primary + sub cards), plus any sightings directly attached to the requirement that have no material_card_id:

```python
def rebuild_vendor_summaries(
    db: Session,
    requirement_id: int,
    vendor_names: list[str] | None = None,
) -> list[VendorSightingSummary]:
    """Rebuild VendorSightingSummary rows for the requirement.

    Pulls sightings via material_card_id set so prior searches on other
    requirements that share an MPN are visible. Falls back to
    requirement_id-direct sightings for rows missing material_card_id.
    """
    from app.models import Requirement, MaterialCard
    from app.utils.normalization import normalize_mpn_key

    req = db.get(Requirement, requirement_id)
    if not req:
        return []

    pns: list[str] = []
    if req.primary_mpn:
        pns.append(req.primary_mpn)
    for sub in req.substitutes or []:
        if isinstance(sub, dict):
            v = (sub.get("mpn") or "").strip()
        else:
            v = str(sub).strip() if sub else ""
        if v:
            pns.append(v)
    norm_keys = [k for k in (normalize_mpn_key(p) for p in pns) if k]

    card_ids = set()
    if norm_keys:
        rows = (
            db.query(MaterialCard.id)
            .filter(MaterialCard.normalized_mpn.in_(norm_keys))
            .all()
        )
        card_ids = {r[0] for r in rows}

    base_filter = [Sighting.is_unavailable.isnot(True)]
    if card_ids:
        base_filter.append(
            (Sighting.material_card_id.in_(card_ids))
            | (
                (Sighting.material_card_id.is_(None))
                & (Sighting.requirement_id == requirement_id)
            )
        )
    else:
        base_filter.append(Sighting.requirement_id == requirement_id)

    query = db.query(Sighting).filter(*base_filter)
    if vendor_names:
        query = query.filter(Sighting.vendor_name.in_(vendor_names))

    sightings = query.all()
    # ... rest of function unchanged from current body (grouping, vendor_phones,
    # upsert, db.flush()).
```

Keep everything below the `query = db.query(Sighting)...` line as-is in the current file.

- [ ] **Step 4: Run cross-MPN test, confirm pass**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestCrossMpnSightingVisibility -v --override-ini="addopts="
```

Expected: 1 passed.

- [ ] **Step 5: Run aggregation suite to confirm no regression**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_sighting_aggregation.py -q --override-ini="addopts="
```

Expected: all 46 + new tests still pass. If `test_unavailable_sightings_excluded` or `test_vendor_filter` fail because they assume the old requirement_id-only query, update each affected test by either:
  - Adding a MaterialCard linked to the requirement, OR
  - Removing the `material_card_id` set up so they fall into the `card_ids = empty → use requirement_id` branch.

- [ ] **Step 6: Commit**

```bash
git add app/services/sighting_aggregation.py tests/test_routers_sightings.py tests/test_sighting_aggregation.py
git commit -m "feat(sightings): cross-MPN visibility via material_card_id set in rebuild_vendor_summaries"
```

---

## Task 5: /refresh endpoint — remove old cooldown, add per-MPN toast

**Files:**
- Modify: `app/routers/sightings.py` (lines 80, 96, 642-695)
- Modify: `tests/test_routers_sightings.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_routers_sightings.py`:

```python
class TestRefreshPerMpnToast:
    def test_toast_describes_searched_and_cached_mpns(
        self, client, db_session, test_user_auth
    ):
        # Item with one fresh MPN (cached) and one stale MPN (searched).
        # Mock _fetch_fresh + ICS/NC enqueue.
        from datetime import datetime, timedelta, timezone
        from app.models import MaterialCard, Requirement, Requisition

        now = datetime.now(timezone.utc)
        r = Requisition(name="R", customer_name="C", status="active",
                        created_by=test_user_auth.id, created_at=now)
        db_session.add(r); db_session.flush()
        item = Requirement(
            requisition_id=r.id,
            primary_mpn="ALPHA",
            substitutes=[{"mpn": "BETA"}],
            created_at=now,
        )
        db_session.add(item)
        # ALPHA cached (12h ago), BETA stale
        db_session.add(MaterialCard(primary_mpn="ALPHA", normalized_mpn="ALPHA",
                                    last_searched_at=now - timedelta(hours=12)))
        db_session.commit()

        with patch(
            "app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))
        ), patch(
            "app.services.ics_worker.queue_manager.enqueue_for_ics_search"
        ), patch(
            "app.services.nc_worker.queue_manager.enqueue_for_nc_search"
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")

        assert resp.status_code == 200
        # HX-Trigger header carries a showToast with both counts
        hx = resp.headers.get("HX-Trigger", "")
        assert '"showToast"' in hx
        assert "1 cached" in hx
        assert "1 search" in hx or "Searched 1" in hx

    def test_all_cached_returns_no_search_toast(
        self, client, db_session, test_user_auth
    ):
        from datetime import datetime, timedelta, timezone
        from app.models import MaterialCard, Requirement, Requisition

        now = datetime.now(timezone.utc)
        r = Requisition(name="R", customer_name="C", status="active",
                        created_by=test_user_auth.id, created_at=now)
        db_session.add(r); db_session.flush()
        item = Requirement(requisition_id=r.id, primary_mpn="ONLY",
                           created_at=now)
        db_session.add(item)
        db_session.add(MaterialCard(primary_mpn="ONLY", normalized_mpn="ONLY",
                                    last_searched_at=now - timedelta(hours=1)))
        db_session.commit()

        with patch(
            "app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))
        ) as fetch_mock:
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")

        assert resp.status_code == 200
        # _fetch_fresh NOT called because all MPNs cached
        fetch_mock.assert_not_called()
        hx = resp.headers.get("HX-Trigger", "")
        assert "All MPNs" in hx or "cached" in hx
```

- [ ] **Step 2: Run, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestRefreshPerMpnToast -v --override-ini="addopts="
```

Expected: no HX-Trigger toast carrying the new content (current code returns generic toast or no toast on the happy path).

- [ ] **Step 3: Rewrite sightings_refresh + delete REFRESH_RATE_LIMIT_SECONDS**

In `app/routers/sightings.py`:

A. Delete lines around 80 and 96:
```python
REFRESH_RATE_LIMIT_SECONDS: Final[int] = 300  # Per-requirement cooldown between manual searches
```
and the helper `_within_rate_limit(...)`. Remove the `Final` import if it becomes unused.

B. Replace `sightings_refresh` body (around lines 642-695):

```python
@router.post("/v2/partials/sightings/{requirement_id}/refresh", response_class=HTMLResponse)
async def sightings_refresh(
    request: Request,
    requirement_id: int,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Re-run sourcing pipeline for a requirement, gated by 48h per-MPN cooldown.

    Returns the rendered detail panel + HX-Trigger toast describing per-MPN result.
    """
    from ..search_service import search_requirement

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    is_sse = source == "sse"
    refresh_failed = False
    mpn_results: dict[str, str] = {}
    try:
        result = await search_requirement(requirement, db)
        mpn_results = result.get("mpn_results", {})
    except Exception:
        logger.warning("Search refresh failed for requirement {}", requirement_id, exc_info=True)
        refresh_failed = True

    # Force a fresh read; search_requirement uses a separate write session.
    db.expire(requirement)

    await _publish_if_user_source(source, user.id, requirement_id)

    response = await sightings_detail(request, requirement_id, db, user)

    if not is_sse:
        toast_msg = _build_mpn_toast(mpn_results, refresh_failed)
        toast_type = "warning" if refresh_failed else (
            "info" if all(v == "cached" for v in mpn_results.values()) else "success"
        )
        if toast_msg:
            response.headers["HX-Trigger"] = (
                f'{{"showToast": {{"message": "{toast_msg}", "type": "{toast_type}"}}}}'
            )
    return response


def _build_mpn_toast(mpn_results: dict[str, str], refresh_failed: bool) -> str:
    """Build the per-MPN toast message from search_requirement's result map."""
    if refresh_failed:
        return "Search refresh failed - showing cached results"
    if not mpn_results:
        return ""
    searched = sum(1 for v in mpn_results.values() if v == "searched")
    cached = sum(1 for v in mpn_results.values() if v == "cached")
    if searched and cached:
        return f"Searched {searched} MPN{'s' if searched != 1 else ''}, {cached} cached"
    if searched:
        return f"Searched {searched} MPN{'s' if searched != 1 else ''}"
    return "All MPNs searched within 48h \\u2014 showing cached"
```

The literal `—` keeps the unicode em-dash escaped inside the JSON header. If your editor produces a literal em-dash, ensure the resulting HX-Trigger JSON stays valid.

- [ ] **Step 4: Run new tests + existing sightings router tests**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py -q --override-ini="addopts="
```

Expected: new tests pass; existing tests that asserted `REFRESH_RATE_LIMIT_SECONDS`-based 5-min cooldown behavior fail. Each such failing test is now obsolete — delete its body. Search for `REFRESH_RATE_LIMIT_SECONDS` and `Already searched within` in the test file and remove those tests:

```
grep -nE "REFRESH_RATE_LIMIT_SECONDS|Already searched within" /root/availai/tests/test_routers_sightings.py
```

Remove each matching test (whole `def test_*` block) and any helpers used only by them. Re-run.

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_routers_sightings.py
git commit -m "feat(sightings): per-MPN toast on /refresh; drop 5-min per-req cooldown"
```

---

## Task 6: Revert row-click POST /refresh

**Files:**
- Modify: `app/templates/htmx/partials/sightings/list.html` (`selectReq` function around lines 58-78)
- Modify: `tests/test_routers_sightings.py` — invert today's `TestSightingsListTemplateSelectReqShape`

- [ ] **Step 1: Update existing tests**

Replace the two assertions in `tests/test_routers_sightings.py::TestSightingsListTemplateSelectReqShape`:

```python
class TestSightingsListTemplateSelectReqShape:
    """Row click on /v2/sightings is read-only: fires GET /detail only.
    The only way to trigger a connector search is the per-row refresh icon
    or the detail panel's Search button.
    """

    def test_selectreq_fires_detail_get(self):
        path = Path(__file__).parent.parent / "app" / "templates" / "htmx" / "partials" / "sightings" / "list.html"
        text = path.read_text()
        assert "htmx.ajax('GET', '/v2/partials/sightings/' + id + '/detail'" in text

    def test_selectreq_does_not_fire_refresh_post(self):
        path = Path(__file__).parent.parent / "app" / "templates" / "htmx" / "partials" / "sightings" / "list.html"
        text = path.read_text()
        # The detail panel's m.search_button and the per-row icon are the only
        # places that POST /refresh — selectReq must not.
        # Scope the check to the selectReq function body via a static slice.
        select_req_start = text.index("selectReq(id) {")
        select_req_end = text.index("closeMobileDetail()", select_req_start)
        select_req_body = text[select_req_start:select_req_end]
        assert "/refresh" not in select_req_body
```

Also update `TestSightingsClickPendingCounter::test_click_pending_counter_present_in_sightings_list_template`:

```python
    def test_click_pending_counter_present_in_sightings_list_template(self):
        """selectReq fires ONE request (GET /detail), so the counter
        increments by 1. SSE handler still consults it to suppress
        background refreshes while a user click is in flight."""
        path = Path(__file__).parent.parent / "app" / "templates" / "htmx" / "partials" / "sightings" / "list.html"
        text = path.read_text()
        assert "store.clickPending += 1" in text
```

- [ ] **Step 2: Run, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestSightingsListTemplateSelectReqShape tests/test_routers_sightings.py::TestSightingsClickPendingCounter -v --override-ini="addopts="
```

Expected: failures because selectReq still POSTs /refresh and increments by 2.

- [ ] **Step 3: Revert selectReq**

In `app/templates/htmx/partials/sightings/list.html` (lines ~57-78), replace:

```html
       // GET /detail renders cached panel <100ms; POST /refresh runs search in
       // background and swaps in fresh results when done. clickPending+=2 keeps
       // the SSE handler suppressed for both legs; X-Rendered-Req-Id correlation
       // already protects against mid-flight row changes.
       selectReq(id) {
         const store = Alpine.store('sightingSelection');
         this.selectedReqId = id;
         store.selectedReqId = id;
         store.clickPending += 2;
         if (window.innerWidth < 1024) { this.mobileDetailOpen = true; }
         htmx.ajax('GET', '/v2/partials/sightings/' + id + '/detail', {
           target: '#sightings-detail',
           swap: 'innerHTML',
           indicator: '#sightings-detail-skeleton',
           headers: { 'X-Click-Req-Id': String(id) },
         });
         htmx.ajax('POST', '/v2/partials/sightings/' + id + '/refresh?source=user', {
           target: '#sightings-detail',
           swap: 'innerHTML',
           indicator: '#sightings-detail-skeleton',
           headers: { 'X-Click-Req-Id': String(id) },
         });
       },
```

with:

```html
       // Row click is read-only: GET /detail renders the cached panel. The
       // only way to fire a connector search is the per-row refresh icon
       // (table.html) or the "Search" button in the detail panel header
       // (_macros.html::search_button). Both POST /v2/.../refresh which is
       // gated by the 48h per-MPN cooldown enforced in search_requirement.
       selectReq(id) {
         const store = Alpine.store('sightingSelection');
         this.selectedReqId = id;
         store.selectedReqId = id;
         store.clickPending += 1;
         if (window.innerWidth < 1024) { this.mobileDetailOpen = true; }
         htmx.ajax('GET', '/v2/partials/sightings/' + id + '/detail', {
           target: '#sightings-detail',
           swap: 'innerHTML',
           indicator: '#sightings-detail-skeleton',
           headers: { 'X-Click-Req-Id': String(id) },
         });
       },
```

- [ ] **Step 4: Run, confirm pass**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestSightingsListTemplateSelectReqShape tests/test_routers_sightings.py::TestSightingsClickPendingCounter -v --override-ini="addopts="
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/sightings/list.html tests/test_routers_sightings.py
git commit -m "revert(sightings): row click is read-only — no POST /refresh"
```

---

## Task 7: Promote per-row refresh icon to always-visible

**Files:**
- Modify: `app/templates/htmx/partials/sightings/table.html` (around lines 153-175)
- Modify: `tests/test_routers_sightings.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_routers_sightings.py`:

```python
class TestPerRowSearchIconAlwaysVisible:
    """The per-row refresh icon must render on every row regardless of
    last_searched_at (no 'stale' conditional). Its hx-post target is the
    same /refresh endpoint the detail-panel button uses.
    """

    def test_row_refresh_icon_has_no_stale_only_conditional(self):
        path = Path(__file__).parent.parent / "app" / "templates" / "htmx" / "partials" / "sightings" / "table.html"
        text = path.read_text()
        # The icon block must NOT be wrapped in a {% if ... is_stale %} or
        # similar Jinja conditional. Locate the hx-post for /refresh and
        # walk backwards to assert there's no conditional opening since the
        # nearest {% block / for loop / etc.
        idx = text.index('hx-post="/v2/partials/sightings/{{ r.id }}/refresh"')
        prefix = text[:idx]
        # The icon is inside the {% for r in items %} loop; the only
        # Jinja control immediately enclosing the button should be that loop.
        # Look for a stale-conditional pattern within ~10 lines above.
        recent = "\n".join(prefix.splitlines()[-10:])
        assert "is_stale" not in recent
        assert "stale_warning" not in recent
        assert ("{% if " not in recent) or ("{% if r" not in recent and "{% if not " not in recent)
```

- [ ] **Step 2: Run, confirm failure (or pass — investigate)**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestPerRowSearchIconAlwaysVisible -v --override-ini="addopts="
```

If the current template already shows the icon unconditionally, the test passes — skip to step 4. Otherwise:

- [ ] **Step 3: Make icon unconditional**

Read `app/templates/htmx/partials/sightings/table.html` lines 153-175. Remove any `{% if ... %}` / `{% elif %}` / `{% endif %}` wrapping the refresh-icon button. Also remove the `title="Stale — click to refresh"` text — replace with `title="Search this requirement"`.

- [ ] **Step 4: Run, confirm pass**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestPerRowSearchIconAlwaysVisible -v --override-ini="addopts="
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html tests/test_routers_sightings.py
git commit -m "feat(sightings): per-row search icon always visible"
```

---

## Task 8: Delete daily cron + auto-enqueue + legacy v1 routes

**Files:**
- Delete: `app/jobs/sourcing_refresh_jobs.py`
- Modify: `app/jobs/__init__.py` (lines 22 and 32)
- Modify: `app/routers/requisitions/requirements.py` (multiple regions)
- Modify or delete: any tests targeting the deleted endpoints

- [ ] **Step 1: Write failing tests for the removals**

Append to `tests/test_routers_sightings.py` (or create `tests/test_no_auto_search.py`):

```python
class TestAutoSearchRemoved:
    def test_v1_search_endpoint_returns_404(self, client):
        # Used to be /api/requirements/{id}/search — removed
        resp = client.post("/api/requirements/1/search")
        assert resp.status_code == 404

    def test_v1_search_all_endpoint_returns_404(self, client):
        # Used to be /api/requisitions/{id}/search-all — removed
        resp = client.post("/api/requisitions/1/search-all")
        assert resp.status_code == 404

    def test_sourcing_refresh_cron_not_registered(self):
        # The 3 AM cron is gone; the import path itself should not exist.
        import importlib
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("app.jobs.sourcing_refresh_jobs")

    def test_creating_requirement_does_not_enqueue_ics_nc(
        self, client, db_session, test_user_auth
    ):
        from app.models import IcsSearchQueue, NcSearchQueue, Requisition

        r = Requisition(name="R", customer_name="C", status="draft",
                        created_by=test_user_auth.id, created_at=datetime.utcnow())
        db_session.add(r); db_session.commit()

        with patch(
            "app.services.ics_worker.queue_manager.enqueue_for_ics_search"
        ) as ics, patch(
            "app.services.nc_worker.queue_manager.enqueue_for_nc_search"
        ) as nc:
            resp = client.post(
                f"/api/requisitions/{r.id}/requirements",
                json={"parts": [{"mpn": "AUTO-CHECK"}]},
            )
        assert resp.status_code in (200, 201)
        ics.assert_not_called()
        nc.assert_not_called()
```

The exact create-requirements endpoint URL may differ — verify by `grep -n "@router.post" /root/availai/app/routers/requisitions/requirements.py | head` and update the URL accordingly.

- [ ] **Step 2: Run, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestAutoSearchRemoved -v --override-ini="addopts="
```

Expected: endpoints return 200 (not 404), module imports succeed, enqueue mocks called.

- [ ] **Step 3a: Delete the cron file + registration**

```bash
git rm app/jobs/sourcing_refresh_jobs.py
```

Edit `app/jobs/__init__.py`:
- Remove line 22: `from .sourcing_refresh_jobs import register_sourcing_refresh_jobs`
- Remove line 32: `register_sourcing_refresh_jobs(scheduler, settings)`

- [ ] **Step 3b: Delete auto-enqueue + auto background search in requirements.py**

In `app/routers/requisitions/requirements.py`:

1. Delete `_enqueue_ics_nc_batch` function (lines 237-251).
2. Delete the inline `_nc_enqueue_batch`, `_ics_enqueue_batch`, `_bg_full_search` functions inside the create-requirements handler (around lines 455-485 plus the bg-search block that follows). Locate and remove the `background_tasks.add_task(_nc_enqueue_batch, ...)`, `_ics_enqueue_batch`, and `_bg_full_search` calls in the create path.
3. Delete the `search_one` route handler at line 861 (the `@router.post("/api/requirements/{item_id}/search")` block) and the `background_tasks.add_task(_enqueue_ics_nc_batch, [r.id])` line inside it.
4. Delete the search-all batch route (the route containing `background_tasks.add_task(_enqueue_ics_nc_batch, req_ids)` near line 851). Use `grep -nB2 "_enqueue_ics_nc_batch" /root/availai/app/routers/requisitions/requirements.py` to find both routes and delete each full handler.
5. Remove now-unused imports at the top of the file: `enqueue_for_ics_search`, `enqueue_for_nc_search`. (Or leave them if other call sites remain — grep first.)

```bash
grep -n "enqueue_for_ics_search\|enqueue_for_nc_search\|_enqueue_ics_nc_batch\|_bg_full_search\|_nc_enqueue_batch\|_ics_enqueue_batch" /root/availai/app/routers/requisitions/requirements.py
```

After edits, this command should return zero lines.

- [ ] **Step 4: Run all removal tests**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_sightings.py::TestAutoSearchRemoved -v --override-ini="addopts="
```

Expected: 4 passed.

- [ ] **Step 5: Run broader test suite to catch tests that referenced removed endpoints**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q --override-ini="addopts=" --timeout=60 -x 2>&1 | tail -40
```

For each failure: if the test asserts behavior of a now-deleted route or function, delete the test (whole `def test_*` block). Do not keep dead tests "just in case". Commit each test removal in the same commit as the source removal.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(sourcing): remove auto-search — daily cron, requirement-creation enqueue, v1 /search routes"
```

---

## Task 9: Flip api_sources + seed ics_worker_status singleton in startup

**Files:**
- Modify: `app/startup.py` — add idempotent seed at end of startup tasks
- Create: `tests/test_startup_seed.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_startup_seed.py
"""Tests for app.startup's idempotent seed of api_sources flips + ics_worker_status singleton.

Called by: pytest
Depends on: app.startup, app.models (ApiSource, IcsWorkerStatus)
"""

from sqlalchemy.orm import Session


def test_startup_flips_icsource_to_live(db_session: Session):
    from app.models import ApiSource
    from app.startup import seed_browser_worker_sources

    # Pre-condition: row exists, disabled
    db_session.add(ApiSource(name="icsource", display_name="ICsource",
                             category="search", source_type="broker",
                             status="disabled", is_active=False))
    db_session.add(ApiSource(name="netcomponents", display_name="NetComponents",
                             category="search", source_type="broker",
                             status="pending", is_active=False))
    db_session.commit()

    seed_browser_worker_sources(db_session)
    db_session.commit()

    ics = db_session.query(ApiSource).filter_by(name="icsource").one()
    nc = db_session.query(ApiSource).filter_by(name="netcomponents").one()
    assert ics.status == "live"
    assert ics.is_active is True
    assert nc.status == "live"
    assert nc.is_active is True


def test_startup_seeds_ics_worker_status_singleton(db_session: Session):
    from app.models import IcsWorkerStatus
    from app.startup import seed_ics_worker_status_singleton

    # Pre: no row
    db_session.query(IcsWorkerStatus).delete()
    db_session.commit()

    seed_ics_worker_status_singleton(db_session)
    db_session.commit()

    row = db_session.query(IcsWorkerStatus).filter_by(id=1).one()
    assert row.is_running is False  # Worker sets True on its own startup

    # Idempotent — running again leaves the row alone
    seed_ics_worker_status_singleton(db_session)
    db_session.commit()
    count = db_session.query(IcsWorkerStatus).count()
    assert count == 1
```

- [ ] **Step 2: Run, confirm failure**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_startup_seed.py -v --override-ini="addopts="
```

Expected: `ImportError: cannot import name 'seed_browser_worker_sources' from 'app.startup'`

- [ ] **Step 3: Add seed functions to startup.py**

Append to `app/startup.py`:

```python
def seed_browser_worker_sources(db: Session) -> None:
    """Flip icsource + netcomponents api_sources rows to live + active.

    Idempotent; safe to run on every startup. The browser workers
    (avail-ics-worker.service, avail-nc-worker.service) are queue-driven
    and need their api_sources rows surfaced as 'live' so the search
    orchestrator's _build_connectors doesn't exclude them.
    """
    from app.models import ApiSource

    for name in ("icsource", "netcomponents"):
        row = db.query(ApiSource).filter_by(name=name).one_or_none()
        if row is None:
            continue
        row.status = "live"
        row.is_active = True


def seed_ics_worker_status_singleton(db: Session) -> None:
    """Insert ics_worker_status id=1 row if absent.

    The worker's update_worker_status() is a no-op when the row is missing,
    so heartbeats and daily stats silently never persist. Seeding makes the
    worker's writes effective from first startup.
    """
    from app.models import IcsWorkerStatus

    existing = db.query(IcsWorkerStatus).filter_by(id=1).one_or_none()
    if existing is not None:
        return
    row = IcsWorkerStatus(id=1, is_running=False)
    db.add(row)
```

Then in the main `run_startup_tasks()` (or equivalent entry point — grep `def run_startup` in `app/startup.py`), add calls at the end:

```python
    seed_browser_worker_sources(db)
    seed_ics_worker_status_singleton(db)
    db.commit()
```

If `app/startup.py` uses a context manager / different session pattern, follow that pattern. The key is: both seeds run within the existing startup transaction.

- [ ] **Step 4: Run, confirm pass**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_startup_seed.py -v --override-ini="addopts="
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/startup.py tests/test_startup_seed.py
git commit -m "feat(startup): seed ics/nc api_sources live + ics_worker_status singleton"
```

---

## Task 10: Update docs

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md` (Section 2 "Search (All Connectors in Parallel)")
- Modify: `docs/APP_MAP_ARCHITECTURE.md` (one-liner about search)
- Modify: `docs/htmx-conventions.md`

- [ ] **Step 1: Update APP_MAP_INTERACTIONS.md Section 2**

Replace the existing trigger line (`Browser POST /v2/partials/requisitions/{id}/search-all`) and add a cooldown narrative:

```markdown
## 2. Search (User-Initiated Only)

```
User clicks per-row refresh icon OR detail-panel "Search" button on /v2/sightings
    |
    v
POST /v2/partials/sightings/{requirement_id}/refresh?source=user
    |
    v
search_requirement(req, db)
    |
    +---> _mpn_cooldown_partition(pns) → (to_search, cached_card_ids)
    |     Per-MPN 48h cooldown via MaterialCard.last_searched_at.
    |     Skip every MPN whose card was searched within 48h; surface
    |     prior sightings via material_card_id linkage instead.
    |
    +---> _fetch_fresh(to_search) — every live HTTP connector in parallel
    |       +---> brokerbin, digikey, mouser, element14, oemsecrets, ...
    |
    +---> enqueue_for_ics_search(requirement_id, db)    # browser worker
    +---> enqueue_for_nc_search(requirement_id, db)     # browser worker
    |
    +---> _save_sightings + scoring + material card upsert
    |
    +---> Stamp MaterialCard.last_searched_at = now on every searched card
    |
    +---> Return {sightings, source_stats, mpn_results: {mpn: "searched"|"cached"}}
```

The detail panel (`GET /v2/partials/sightings/{id}/detail`) queries sightings via the requirement's MaterialCard set (primary + substitutes) so prior searches on other requirements that share an MPN are visible.

**Removed (2026-05-14):**
- Daily 3 AM `_job_refresh_stale_requisitions` cron — no more background refresh
- Requirement-creation auto-enqueue of ICS + NC + background full-connector search
- Legacy `POST /api/requirements/{id}/search` and `POST /api/requisitions/{id}/search-all` routes
- Row-click POST `/refresh` (row click is read-only GET `/detail` only)
```

(Surround the new section with the existing top-level `## 2.` heading and replace the old content. If older content under that section described the v1 endpoints, delete those subsections.)

- [ ] **Step 2: Update APP_MAP_ARCHITECTURE.md**

Find the line that mentions auto-search and replace with: "Sourcing is strictly user-initiated; clicking the refresh icon on a sightings row or the detail-panel Search button triggers connectors gated by a 48h per-MPN cooldown."

- [ ] **Step 3: Update htmx-conventions.md**

Find the section added today about the click-to-refresh pattern. Replace it with:

```markdown
### Sightings click pattern

- Row click on `/v2/sightings` → `GET /v2/partials/sightings/{id}/detail` only.
  Read-only. No connector calls. `clickPending += 1`.
- Per-row refresh icon → `POST /v2/partials/sightings/{id}/refresh` (gated 48h per MPN).
- Detail-panel "Search" button (m.search_button macro) → same POST.
- SSE-driven re-render → `POST /v2/partials/sightings/{id}/refresh?source=sse`
  (skips broker.publish to break the SSE re-fire loop).

X-Rendered-Req-Id header echo + Alpine clickPending counter remain the
correlation + suppression mechanism. See app/static/htmx_app.js.
```

- [ ] **Step 4: Commit docs**

```bash
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_ARCHITECTURE.md docs/htmx-conventions.md
git commit -m "docs: search-button-only sourcing (2026-05-14)"
```

---

## Final verification

- [ ] **Run full test suite**

```
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q --override-ini="addopts=" --timeout=60 2>&1 | tail -10
```

Expected: all tests pass (target ≥ 13610 baseline minus the deleted tests for removed endpoints).

- [ ] **Run pre-commit on all files**

```
pre-commit run --all-files 2>&1 | tail -10
```

Expected: all hooks pass.

- [ ] **Deploy**

```
./deploy.sh --no-commit
```

Expected: build tag matches HEAD, app health endpoint OK, no errors in `docker compose logs app --tail 30`.

- [ ] **Manual smoke checks (in DB + logs)**

```
docker compose exec -T db psql -U availai -d availai -c "SELECT name, status, is_active FROM api_sources WHERE name IN ('icsource', 'netcomponents');"
```

Expected: both rows show `status='live'`, `is_active=true`.

```
docker compose exec -T db psql -U availai -d availai -c "SELECT id, is_running FROM ics_worker_status;"
```

Expected: singleton row with id=1 exists.

```
journalctl -u avail-ics-worker.service --since "2 minutes ago" --no-pager | tail -5
```

Expected: worker still polling (queue empty messages, not crash logs).

- [ ] **Push + update PR**

```
git push origin chore/gradient-vestiges-and-docfmt
gh pr comment 107 --body "Implements docs/superpowers/specs/2026-05-14-search-button-only-sourcing-design.md. Removed auto-search (3 AM cron, requirement-create auto-enqueue, v1 /search routes, row-click /refresh). Per-MPN 48h cooldown gates the two user-initiated triggers (per-row icon + detail-panel button). Cross-MPN sighting visibility via material_card_id."
```
