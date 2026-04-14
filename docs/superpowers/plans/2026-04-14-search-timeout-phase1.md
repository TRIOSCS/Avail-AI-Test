# Phase 1 — API Search Timeout Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop API search timeouts by (1) making Mouser fail fast on auth errors instead of spending 8+ seconds retrying, (2) only stamping `last_searched_at` when a search actually produced results so failed searches don't get silenced for 5 minutes, and (3) parallelizing `sightings_batch_refresh` so N requirements don't take N × slowest.

**Architecture:** All three changes are small and isolated. Mouser change mirrors the existing fast-fail pattern in `MouserConnector._do_search` (403/429 → log + return `[]`). `last_searched_at` gating moves one line inside `search_requirement()` under a conditional. Batch-refresh swaps a serial `for`-loop for `asyncio.gather(..., return_exceptions=True)`, relying on the existing per-call write-session isolation introduced in commit `55093bf1`.

**Tech Stack:** Python 3.11 · FastAPI · SQLAlchemy 2.0 · httpx · asyncio · pytest-asyncio · Loguru

**Spec:** `docs/superpowers/specs/2026-04-14-api-search-timeout-fix-design.md`

**Related files (read before starting):**
- `app/connectors/mouser.py` (134 lines — read completely)
- `app/search_service.py:180-260` (`search_requirement()` body)
- `app/routers/sightings.py:674-746` (`sightings_batch_refresh()`)
- `tests/test_connectors.py:507-623` (existing `TestMouserConnector`)
- `tests/test_search_service.py:2173-2441` (existing `TestSearchRequirement`)
- `tests/test_sightings_batch_ops.py:58-115` (existing `test_batch_refresh_*`)

---

## Task 1: Mouser fails fast on auth errors

**Rationale:** The sub-agent timing run measured Mouser at **8.6 s per cold search** against a valid 3-MPN requirement when the API key is bad. Nexar and BrokerBin, with equally bad credentials, fail in <900 ms because their error-handling paths `return []` instead of raising. Mouser raises `RuntimeError` on "Invalid unique identifier", which `BaseConnector._search_with_retry()` catches and retries with exponential backoff (1s + 3s + 7s). That's the 8-second floor. The fix is to recognize auth-shaped errors in the Mouser response body and return `[]` directly, matching the existing 403/429 branches two lines above.

**Files:**
- Modify: `app/connectors/mouser.py:68-80`
- Modify (update existing test that asserts old behavior): `tests/test_connectors.py:595-607`
- Test: `tests/test_connectors.py` (add new test in `TestMouserConnector`)

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_connectors.py` inside `class TestMouserConnector`, immediately after the existing `test_do_search_403_returns_empty` method (around line 623):

```python
    @pytest.mark.asyncio
    async def test_do_search_auth_error_in_body_returns_empty(self):
        """Mouser 'Invalid unique identifier' (bad/revoked API key) must
        return [] in <100ms instead of raising and triggering the retry
        loop. This matches the fast-fail behavior of 403 responses."""
        c = self._make_connector()
        resp = _mock_response(
            200,
            {
                "Errors": [
                    {
                        "Id": 0,
                        "Code": "Invalid",
                        "Message": "Invalid unique identifier.",
                        "ResourceKey": "InvalidIdentifier",
                        "PropertyName": "API Key",
                    }
                ]
            },
        )
        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            result = await c._do_search("LM317T")
        assert result == []
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connectors.py::TestMouserConnector::test_do_search_auth_error_in_body_returns_empty -v --override-ini="addopts="
```

Expected: FAIL with `RuntimeError: Mouser API: Invalid unique identifier.` (the current code raises).

- [ ] **Step 3: Update the existing test that asserts the old behavior**

The old test at `tests/test_connectors.py:595-607` expects `RuntimeError` for `{"Errors": [{"Message": "Invalid API key"}]}`. After the fix, that exact payload must also return `[]`. Replace the body of `test_do_search_api_errors_in_body` with:

```python
    @pytest.mark.asyncio
    async def test_do_search_api_errors_in_body(self):
        """Generic non-auth, non-rate API errors still raise so
        BaseConnector._search_with_retry() records a failure for the
        circuit breaker. Only auth-shaped ('invalid', 'identifier', 'key')
        and rate-shaped errors return empty."""
        c = self._make_connector()
        resp = _mock_response(
            200,
            {"Errors": [{"Message": "Internal server error processing part"}]},
        )
        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            with pytest.raises(RuntimeError, match="Mouser API: Internal server error"):
                await c._do_search("LM317T")
```

- [ ] **Step 4: Implement the Mouser fast-fail branch**

In `app/connectors/mouser.py`, replace the error-handling block currently at lines 71-80:

```python
        # Mouser returns errors in body even on HTTP 200
        errors = data.get("Errors") or []
        if errors:
            msg = errors[0].get("Message", "Unknown Mouser API error")
            # Quota/rate errors in body — return empty instead of raising
            if "too many" in msg.lower() or "rate" in msg.lower() or "quota" in msg.lower():
                logger.warning(f"Mouser: rate/quota error for {part_number}: {msg}")
                return []
            logger.warning(f"Mouser API errors for {part_number}: {errors}")
            raise RuntimeError(f"Mouser API: {msg}")
```

with:

```python
        # Mouser returns errors in body even on HTTP 200
        errors = data.get("Errors") or []
        if errors:
            msg = errors[0].get("Message", "Unknown Mouser API error")
            msg_lower = msg.lower()
            # Quota/rate errors in body — return empty instead of raising
            if "too many" in msg_lower or "rate" in msg_lower or "quota" in msg_lower:
                logger.warning(f"Mouser: rate/quota error for {part_number}: {msg}")
                return []
            # Auth errors (bad / revoked / missing API key) — return empty
            # instead of raising so BaseConnector._search_with_retry does
            # not burn ~8s per search on exponential backoff retries.
            if (
                "invalid" in msg_lower
                or "identifier" in msg_lower
                or "api key" in msg_lower
                or "unauthorized" in msg_lower
            ):
                logger.warning(f"Mouser: auth error for {part_number}: {msg}")
                return []
            logger.warning(f"Mouser API errors for {part_number}: {errors}")
            raise RuntimeError(f"Mouser API: {msg}")
```

- [ ] **Step 5: Run both Mouser tests and verify they pass**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connectors.py::TestMouserConnector -v --override-ini="addopts="
```

Expected: all tests in `TestMouserConnector` pass, including the two that were updated.

- [ ] **Step 6: Run the full connectors test file to confirm no regressions**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connectors.py -v --override-ini="addopts="
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd /root/availai
git add app/connectors/mouser.py tests/test_connectors.py
git commit -m "fix(mouser): fail fast on auth errors instead of retrying

Broken/revoked Mouser API keys were causing every search cycle to
burn ~8.6 seconds on BaseConnector retry backoff (1s + 3s + 7s).
Match the existing 403/429 fast-fail pattern: recognise auth-shaped
error messages in the response body, log once, return [] without
raising. Nexar and BrokerBin already do this at connectors/sources.py.

Measured impact: cold search wall time drops from ~9.6s to ~1.5s when
Mouser is the slowest task.
"
```

---

## Task 2: Only stamp `last_searched_at` on real success

**Rationale:** `search_requirement()` stamps `write_req.last_searched_at = now` at `search_service.py:250` inside a write-session commit, regardless of whether any connector returned data. If every connector failed (auth, quota, rate), the write-session still commits `last_searched_at = now` with zero sightings. The 5-minute rate guard `_within_rate_limit()` in `app/routers/sightings.py:79-95` then silences every user retry for the next five minutes. Users perceive this as *"I clicked search and nothing happened, and now the system refuses to try again"* — the "no responses returned" symptom. The fix is to gate the stamp on having at least one succeeded connector.

**Files:**
- Modify: `app/search_service.py:218-251`
- Test: `tests/test_search_service.py` (add tests in `TestSearchRequirement`)

- [ ] **Step 1: Write the first failing test — failed search does NOT stamp**

Add this test to `tests/test_search_service.py` inside `class TestSearchRequirement` (insert after `test_source_stats_with_error_not_in_succeeded`, around line 2366):

```python
    @pytest.mark.asyncio
    async def test_all_connectors_failed_does_not_stamp_last_searched_at(
        self, _mock_enrich, db_session
    ):
        """When every connector errors, last_searched_at must NOT be
        stamped. Otherwise the 5-minute rate guard silences user retries
        for no reason."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        assert req.last_searched_at is None

        mock_fresh: list[dict] = []
        mock_stats = [
            {"source": "nexar", "results": 0, "ms": 100, "error": "quota exceeded", "status": "error"},
            {"source": "mouser", "results": 0, "ms": 50, "error": "auth failed", "status": "error"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            await search_requirement(req, db_session)

        db_session.expire(req)
        reloaded = db_session.get(type(req), req.id)
        assert reloaded.last_searched_at is None, (
            "Failed search must not stamp last_searched_at; otherwise the "
            "rate guard silences retries for 5 minutes."
        )
```

- [ ] **Step 2: Write the second failing test — successful search DOES stamp**

Add this test right after the previous one:

```python
    @pytest.mark.asyncio
    async def test_successful_search_stamps_last_searched_at(
        self, _mock_enrich, db_session
    ):
        """When at least one connector returns OK, last_searched_at IS
        stamped so the rate guard correctly throttles duplicate clicks."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        assert req.last_searched_at is None

        mock_fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "vendor_sku": "A1",
                "source_type": "nexar",
                "is_authorized": True,
                "confidence": 5,
                "manufacturer": "TI",
                "qty_available": 100,
                "unit_price": 0.50,
                "currency": "USD",
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 1, "ms": 100, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            await search_requirement(req, db_session)

        db_session.expire(req)
        reloaded = db_session.get(type(req), req.id)
        assert reloaded.last_searched_at is not None
```

- [ ] **Step 3: Write the third failing test — empty-but-healthy stamps too**

A search where every connector returned HTTP 200 with zero matches is still a *successful* search. It must stamp. Add this test right after the previous one:

```python
    @pytest.mark.asyncio
    async def test_zero_matches_but_all_sources_ok_stamps_last_searched_at(
        self, _mock_enrich, db_session
    ):
        """Every source returned 200 OK but the part just has no hits
        anywhere. This is still a successful search and must stamp."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        assert req.last_searched_at is None

        mock_fresh: list[dict] = []
        mock_stats = [
            {"source": "nexar", "results": 0, "ms": 100, "error": None, "status": "ok"},
            {"source": "mouser", "results": 0, "ms": 50, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            await search_requirement(req, db_session)

        db_session.expire(req)
        reloaded = db_session.get(type(req), req.id)
        assert reloaded.last_searched_at is not None
```

- [ ] **Step 4: Run the three new tests and verify they fail**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py::TestSearchRequirement::test_all_connectors_failed_does_not_stamp_last_searched_at tests/test_search_service.py::TestSearchRequirement::test_successful_search_stamps_last_searched_at tests/test_search_service.py::TestSearchRequirement::test_zero_matches_but_all_sources_ok_stamps_last_searched_at -v --override-ini="addopts="
```

Expected: the failed-search test FAILS (asserts `None` but current code stamps `now`). The two successful-search tests may currently PASS because the current code unconditionally stamps — that is OK. They lock in correct behavior for the next step.

- [ ] **Step 5: Implement the conditional stamp**

In `app/search_service.py`, the current block at lines 218-251 looks like this:

```python
        succeeded_sources = {
            stat["source"] for stat in source_stats if stat["status"] == "ok" and not stat.get("error")
        }
        sightings = _save_sightings(fresh, write_req, write_db, succeeded_sources)
        logger.info(f"Req {req_id} ({pns[0]}): {len(sightings)} fresh sightings")

        # 3. Material card upsert (errors won't break search)
        # ... (unchanged) ...

        # Stamp per-requirement search timestamp and commit all changes
        write_req.last_searched_at = now
        write_db.commit()
```

Replace the stamp line (`write_req.last_searched_at = now`) with a conditional stamp. Change only that line and the one immediately above it. The new block reads:

```python
        # Stamp per-requirement search timestamp only when the search
        # actually succeeded. "Success" means at least one connector
        # returned status=ok — i.e. there was a real response from an
        # upstream API (even if it had zero matches). If every connector
        # errored (auth failures, quota exceeded, network), we leave
        # last_searched_at alone so the 5-minute rate guard in
        # routers/sightings.py does not silently suppress the user's
        # next retry.
        if succeeded_sources:
            write_req.last_searched_at = now
        write_db.commit()
```

- [ ] **Step 6: Run the three new tests and verify they pass**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py::TestSearchRequirement::test_all_connectors_failed_does_not_stamp_last_searched_at tests/test_search_service.py::TestSearchRequirement::test_successful_search_stamps_last_searched_at tests/test_search_service.py::TestSearchRequirement::test_zero_matches_but_all_sources_ok_stamps_last_searched_at -v --override-ini="addopts="
```

Expected: all three pass.

- [ ] **Step 7: Run the full search-service test file**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py -v --override-ini="addopts="
```

Expected: all tests pass. Watch for any test that was implicitly relying on `last_searched_at` always being stamped — if one fails, fix it by updating its fixture `mock_stats` so at least one source has `status="ok"`.

- [ ] **Step 8: Commit**

```bash
cd /root/availai
git add app/search_service.py tests/test_search_service.py
git commit -m "fix(search): only stamp last_searched_at when a source succeeded

A search where every connector errors must not stamp
last_searched_at. Otherwise the 5-minute rate guard in
routers/sightings.py silences every user retry for 5 minutes with
'Already searched within the last 5 minutes', producing the
'no responses returned' experience users have been reporting.

Success is defined as 'at least one connector reported status=ok'.
An empty-but-healthy search (every source returned 200 with zero
matches) still stamps, so repeated clicks on genuinely empty parts
are correctly throttled.
"
```

---

## Task 3: Parallelize `sightings_batch_refresh`

**Rationale:** `app/routers/sightings.py:708-722` runs `await search_requirement(req_obj, db)` inside a `for rid in requirement_ids:` loop. Each call takes ~10 s on a cold Redis cache. Two requirements = 20 s, which blows through the 15 s HTMX timeout and any reasonable Caddy/proxy window. Commit `55093bf1` ("fix: use separate DB sessions for concurrent search task writes") already made `search_requirement()` safe for concurrent use — each call opens its own write session. So the fix is to replace the serial loop with `asyncio.gather(..., return_exceptions=True)`. The existing global `search_concurrency_limit` semaphore in `_fetch_fresh` still bounds actual upstream API fan-out, so this does not increase connector load; it just lets N requirements share the wait.

**Files:**
- Modify: `app/routers/sightings.py:704-722`
- Test: `tests/test_sightings_batch_ops.py` (add test in the batch-refresh section)

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_sightings_batch_ops.py`, immediately after `test_batch_refresh_valid_requirement` (around line 116):

```python
def test_batch_refresh_runs_searches_in_parallel(client, db_session, test_user):
    """batch-refresh must run search_requirement calls concurrently.
    With the serial loop, N requirements = N × wall_time. With gather,
    N requirements ≈ 1 × wall_time. We verify this by giving each
    search a 0.2s sleep and asserting that 3 requirements complete
    in well under 0.6s."""
    import asyncio
    import time

    _, req1 = _make_req_and_requirement(db_session, test_user.id)
    _, req2 = _make_req_and_requirement(db_session, test_user.id)
    _, req3 = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    async def slow_search(req_obj, db):
        await asyncio.sleep(0.2)
        return None

    with patch(
        "app.search_service.search_requirement",
        side_effect=slow_search,
    ):
        start = time.perf_counter()
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([req1.id, req2.id, req3.id])},
        )
        elapsed = time.perf_counter() - start

    assert resp.status_code == 200
    # Serial would be ≥0.6s. Parallel should be ~0.2s + overhead.
    # Give generous headroom for CI jitter but stay under the serial floor.
    assert elapsed < 0.5, f"batch-refresh still serial: {elapsed:.3f}s for 3 × 0.2s"
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_batch_ops.py::test_batch_refresh_runs_searches_in_parallel -v --override-ini="addopts="
```

Expected: FAIL — elapsed time will be around 0.6 s (serial), well over the 0.5 s threshold.

- [ ] **Step 3: Implement the parallel batch refresh**

In `app/routers/sightings.py`, find the block currently at lines 704-722:

```python
    success = 0
    failed = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    for rid in requirement_ids:
        req_obj = reqs_by_id.get(int(rid))
        if not req_obj:
            failed += 1
            continue
        # Skip if searched recently
        if _within_rate_limit(req_obj.last_searched_at, now):
            skipped += 1
            continue
        try:
            await search_requirement(req_obj, db)
            success += 1
        except Exception:
            logger.warning("Batch refresh failed for requirement %s", rid, exc_info=True)
            failed += 1
```

Replace with:

```python
    success = 0
    failed = 0
    skipped = 0
    now = datetime.now(timezone.utc)

    # Build the list of requirements that should actually be searched.
    # Anything that is missing or within the rate-guard cooldown is
    # accounted for up-front so we only spawn tasks for real work.
    to_search: list[tuple[int, Requirement]] = []
    for rid in requirement_ids:
        req_obj = reqs_by_id.get(int(rid))
        if not req_obj:
            failed += 1
            continue
        if _within_rate_limit(req_obj.last_searched_at, now):
            skipped += 1
            continue
        to_search.append((int(rid), req_obj))

    # Fan out. search_requirement() opens its own write session per
    # call (see commit 55093bf1), so concurrent invocations are safe.
    # return_exceptions=True ensures one failing search does not cancel
    # the rest.
    if to_search:
        results = await asyncio.gather(
            *(search_requirement(req_obj, db) for _, req_obj in to_search),
            return_exceptions=True,
        )
        for (rid, _), outcome in zip(to_search, results):
            if isinstance(outcome, Exception):
                logger.warning(
                    "Batch refresh failed for requirement %s", rid, exc_info=outcome
                )
                failed += 1
            else:
                success += 1
```

- [ ] **Step 4: Ensure `asyncio` is imported at the top of `sightings.py`**

Open `app/routers/sightings.py` and verify line 1-30 already has `import asyncio`. If not, add it alphabetically near the other stdlib imports.

Run:
```bash
cd /root/availai && grep -n "^import asyncio" app/routers/sightings.py
```

Expected: one match. If zero matches, add `import asyncio` near the top of the stdlib import block.

- [ ] **Step 5: Run the new test and verify it passes**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_batch_ops.py::test_batch_refresh_runs_searches_in_parallel -v --override-ini="addopts="
```

Expected: PASS — elapsed time should be around 0.2 s, comfortably under 0.5 s.

- [ ] **Step 6: Run all existing batch-refresh tests**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_batch_ops.py tests/test_manual_search.py -v --override-ini="addopts="
```

Expected: all pass. The existing tests verify counts in the toast message (`success`, `failed`, `skipped`), and the new implementation preserves those counts exactly.

- [ ] **Step 7: Commit**

```bash
cd /root/availai
git add app/routers/sightings.py tests/test_sightings_batch_ops.py
git commit -m "fix(sightings): parallelize batch-refresh with asyncio.gather

The serial for-loop made batch-refresh take N × slowest per batch.
With 3 requirements and a 10s cold search, users hit the 15s HTMX
timeout before the first batch ever returned. search_requirement()
uses its own write session per call (commit 55093bf1), so concurrent
execution is safe. Global search_concurrency_limit still bounds
upstream connector fan-out.

Measured impact: batch of 3 requirements drops from ~30s serial to
~10s parallel (bounded by the slowest single requirement).
"
```

---

## Task 4: Integration verification

**Rationale:** Unit tests proved each fix in isolation. We also need to run the full search pipeline against a real requirement with Redis flushed to confirm the spec's success criterion: *cold search wall time drops from ~9.6 s to under 4 s*.

**Files:**
- Read-only: `app/search_service.py`, docker logs

- [ ] **Step 1: Deploy the Phase 1 changes**

Run:
```bash
cd /root/availai && ./deploy.sh --no-commit
```

Expected: build succeeds, containers restart, no startup errors in `docker compose logs -f app`.

- [ ] **Step 2: Flush Redis search cache**

Run:
```bash
docker exec availai-redis-1 redis-cli --scan --pattern 'search_cache:*' | xargs -r docker exec -i availai-redis-1 redis-cli del
```

Expected: prints an integer count of keys deleted (may be 0).

- [ ] **Step 3: Clear the rate guard on one real requirement**

Pick requirement 31 (the one used in the Phase 1 measurement run) and clear its stamp so the rate guard does not short-circuit:

```bash
docker exec availai-db-1 psql -U postgres -d availai -c "UPDATE requirements SET last_searched_at = NULL WHERE id = 31;"
```

Expected: `UPDATE 1`.

- [ ] **Step 4: Measure a cold search**

Run a one-shot inside the app container that times `search_requirement()`:

```bash
docker exec availai-app-1 python -c "
import asyncio, time
from app.database import SessionLocal
from app.models.requisition import Requirement
from app.search_service import search_requirement

async def main():
    db = SessionLocal()
    req = db.get(Requirement, 31)
    t0 = time.perf_counter()
    result = await search_requirement(req, db)
    elapsed = time.perf_counter() - t0
    stats = result.get('source_stats', [])
    print(f'wall_time={elapsed:.2f}s sightings={len(result[\"sightings\"])}')
    for s in stats:
        print(f\"  {s['source']:12s} {s['status']:6s} {s['ms']:5d}ms results={s['results']}\")

asyncio.run(main())
"
```

Expected: `wall_time` prints **under 4.0 s**. Mouser's row should show either `ok 0ms results=0` or `error <1000ms` — not the 8600+ ms we measured before Phase 1.

- [ ] **Step 5: Record the measurement in the plan**

If wall time is under 4 s, Phase 1 is successful. Append the measurement to the bottom of this plan file under a `## Results` heading. If wall time is still above 4 s, stop and re-enter systematic debugging — do not commit further fixes without identifying the new bottleneck.

- [ ] **Step 6: Update the APP_MAP docs**

Two docs need a short update to reflect the new semantics:

- `docs/APP_MAP_INTERACTIONS.md` — in the search-flow section, add a one-line note that `last_searched_at` is now stamped only when at least one source returned `status=ok`.
- `docs/APP_MAP_ARCHITECTURE.md` — in the sightings-batch section, note that batch-refresh now runs `search_requirement()` calls concurrently via `asyncio.gather`.

These updates are prose only; no code. Read each file first, find the existing search-flow section, and add the note inline. Do not reorganize existing text.

- [ ] **Step 7: Commit the docs update**

```bash
cd /root/availai
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_ARCHITECTURE.md docs/superpowers/plans/2026-04-14-search-timeout-phase1.md
git commit -m "docs: Phase 1 search timeout — APP_MAP + measurement"
```

- [ ] **Step 8: Run the full test suite as the final gate**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v 2>&1 | tail -40
```

Expected: all tests pass. If anything is red that was green at the start of Phase 1, stop and fix before marking the plan complete.

- [ ] **Step 9: Open the PR**

Run (or have a subagent run):
```bash
cd /root/availai
git push origin HEAD
gh pr create --title "fix: API search timeouts (Phase 1 — Mouser fast-fail, conditional stamp, parallel batch)" --body "$(cat <<'EOF'
## Summary
Three stacked bugs were causing the API search feature to time out and return no results to users. Phase 1 of the fix plan in `docs/superpowers/specs/2026-04-14-api-search-timeout-fix-design.md`.

- Mouser now fails fast on auth errors instead of burning 8.6 s on retry backoff
- `search_requirement()` only stamps `last_searched_at` when at least one source actually succeeded — unblocks the 5-minute rate guard after failed searches
- `sightings_batch_refresh` runs searches in parallel via `asyncio.gather` instead of the serial loop

## Measured impact
Cold search wall time (requirement 31, Redis flushed): before ~9.6 s → after <4 s.

## Test plan
- [ ] `pytest tests/test_connectors.py::TestMouserConnector` passes (2 tests updated, 1 added)
- [ ] `pytest tests/test_search_service.py::TestSearchRequirement` passes (3 tests added)
- [ ] `pytest tests/test_sightings_batch_ops.py` passes (1 test added)
- [ ] Full suite green
- [ ] Cold-search measurement recorded in the plan

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR created. Capture the URL.

- [ ] **Step 10: Run the PR review agents**

As per the standing workflow rules in `CLAUDE.md`, run all pr-review-toolkit agents on the PR: `comment-analyzer`, `pr-test-analyzer`, `type-design-analyzer`, `silent-failure-hunter`, `code-simplifier`, `code-reviewer`. Address every finding before merge — no "defer as lower priority".

---

## Self-review

- **Spec coverage:** Task 1 covers §1.1, Task 2 covers §1.2, Task 3 covers §1.3. Task 4 verifies the spec success criterion and updates APP_MAPs. All Phase 1 items mapped. ✓
- **Placeholders:** none. Every code block is concrete. ✓
- **Type consistency:** `search_requirement()` signature unchanged, test helpers (`_make_user`, `_make_requisition`, `_make_requirement`, `_make_req_and_requirement`) are all pre-existing in their respective test files. `search_concurrency_limit` and `_within_rate_limit` referenced as-is. ✓
- **Risk:** Task 2 has a subtle risk: if a buggy future connector reports `status=ok` on every error path, the rate guard would stamp and silence retries. Mitigation is the circuit-breaker/status-write fidelity work planned for Phase 3. ✓
