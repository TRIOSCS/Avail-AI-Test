# Part-Search Background-Task Session-Lifetime Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the part-search feature (`/v2/partials/search/run`) by giving the streaming worker its own SQLAlchemy session instead of sharing the request-scoped one that gets closed before the worker runs.

**Architecture:** `search_run` schedules `stream_search_mpn` as a fire-and-forget `asyncio.Task` via `_safe_bg`. Today it passes the request `db: Session` into the task; FastAPI's `get_db` finalizer closes that session as soon as the response is sent, and the task crashes on its first `db.query(...)`. `_safe_bg` swallows the exception, no SSE events are ever published, and the browser hangs on the spinner. Fix: drop the `db` parameter from `stream_search_mpn` and open a fresh `SessionLocal()` inside it with a `try/finally` close, mirroring the existing precedent at `app/search_service.py:1775-1793` (`_enrich_cards`).

**Tech Stack:** FastAPI, SQLAlchemy 2.0, asyncio, in-process SSE broker, pytest + pytest-asyncio.

**Out of scope (separate follow-ups):**
- SSE replay buffer in `app/services/sse_broker.py` to fix publish-before-subscribe race (latent, not user-fatal once this fix lands).
- Connector credential rotation (BrokerBin protocol/auth, Mouser key, Nexar key + quota, Apollo enrichment 401) — operational, not code.
- Duplicate-POST de-dup on `search_id` channel — minor concern, not regression-blocking.

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `app/search_service.py` | Streaming search worker | Drop `db: Session` parameter from `stream_search_mpn`; open `SessionLocal()` inside; wrap body in `try/finally close()` |
| `app/routers/htmx_views.py` | Search route handler | Stop passing the request `db` into `stream_search_mpn` |
| `tests/test_search_service.py` (or new `tests/test_stream_search_mpn.py`) | Regression test | New failing test that proves the worker opens and closes its own session and survives the request session being closed |

No new files unless `tests/test_search_service.py` does not exist — task 1 covers that decision.

---

## Task 1: Regression test that locks in the bug

**Files:**
- Test: `tests/test_search_service.py` (append) OR `tests/test_stream_search_mpn.py` (new) — see Step 1 for the decision.

- [ ] **Step 1: Decide test file**

Run: `ls tests/test_search_service.py 2>/dev/null && echo EXISTS || echo MISSING`

If `EXISTS`, append the new test to it.
If `MISSING`, create a new file `tests/test_stream_search_mpn.py` with the standard project header comment block:

```python
"""Regression tests for app.search_service.stream_search_mpn.

What this tests: the streaming part-search worker correctly manages its own
SQLAlchemy session lifetime — it opens a fresh SessionLocal() and closes it
in a try/finally, instead of relying on the caller's request session which
FastAPI closes immediately after the response is sent.

Called by: pytest test runner.
Depends on: app.search_service (stream_search_mpn), app.services.sse_broker
(broker monkeypatch seam at module level).
"""
```

- [ ] **Step 2: Write the failing test**

Add the following test. It mocks `SessionLocal` so we can observe that the worker opens its own session, mocks `_build_connectors` to return an empty list (forces the early-return `done` path so we don't need to mock connectors or `VendorCard`), and asserts the worker completes without raising.

```python
import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_stream_search_mpn_opens_and_closes_its_own_session(monkeypatch):
    """The worker must not depend on the caller's session.

    It must open a fresh SessionLocal() at start and close it in a finally
    block, surviving the request session being already closed by the time
    the background task runs.
    """
    from app import search_service

    # Track sessions created and closed
    sessions_created: list[MagicMock] = []

    def fake_session_local():
        s = MagicMock(name="WorkerSession")
        s.closed = False

        def _close():
            s.closed = True

        s.close.side_effect = _close
        # query(...).all() returns an empty list (for VendorCard lookup)
        s.query.return_value.all.return_value = []
        sessions_created.append(s)
        return s

    monkeypatch.setattr(search_service, "SessionLocal", fake_session_local, raising=False)

    # No connectors → worker takes the early-return done path
    monkeypatch.setattr(
        search_service,
        "_build_connectors",
        lambda _db: ([], {}, []),
    )

    # Capture broker publishes instead of touching the real broker
    publishes: list[tuple[str, str, str]] = []

    class FakeBroker:
        async def publish(self, channel, event, data):
            publishes.append((channel, event, data))

    monkeypatch.setattr(search_service, "broker", FakeBroker(), raising=False)

    # Call without a db argument — this will fail today (TypeError) and pass after the fix.
    await search_service.stream_search_mpn("test-search-id", "LM317")

    assert len(sessions_created) == 1, "worker must open exactly one session"
    assert sessions_created[0].closed is True, "worker must close its session in finally"
    assert any(evt == "done" for _, evt, _ in publishes), "worker must publish a done event"
```

- [ ] **Step 3: Run the test to verify it fails (today)**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py::test_stream_search_mpn_opens_and_closes_its_own_session -v --override-ini="addopts="`

(Adjust path if the test was placed in `tests/test_stream_search_mpn.py`.)

Expected: **FAIL** with `TypeError: stream_search_mpn() missing 1 required positional argument: 'db'` — this is the lock-in evidence that the function currently has the wrong signature.

- [ ] **Step 4: Commit the failing test**

```bash
git add tests/test_search_service.py  # or tests/test_stream_search_mpn.py
git commit -m "test: add regression test for stream_search_mpn session lifetime"
```

---

## Task 2: Fix `stream_search_mpn` to own its session

**Files:**
- Modify: `app/search_service.py:1959-2114` (the entire `stream_search_mpn` function body)

- [ ] **Step 1: Read the existing function**

Read `app/search_service.py:1959-2114` in full so the edit covers the whole body. Note: the request session is referenced at exactly two places — line 1982 (`_build_connectors(db)`) and line 1995 (`db.query(VendorCard...)`). Lines 2014-2114 do not reference `db`. The `try/finally` must still wrap the entire body, because future maintainers may add DB calls anywhere in the loop.

- [ ] **Step 2: Replace the function with the corrected version**

Replace lines 1959-2114 of `app/search_service.py` with the following. The signature loses `db: Session`; the body opens `SessionLocal()` immediately after the broker is resolved and wraps everything in a `try/finally`.

```python
async def stream_search_mpn(search_id: str, mpn: str) -> None:
    """Stream search results via SSE as each connector completes.

    Instead of waiting for all connectors (like _fetch_fresh with asyncio.gather),
    this fires all connectors as tasks and uses asyncio.wait(FIRST_COMPLETED) to
    publish results incrementally via the SSE broker.

    Opens its own SessionLocal() so the worker is not tied to the caller's
    request session (which FastAPI closes once the response is sent).

    Called by: routers/htmx_views.py (search stream endpoint)
    Depends on: _build_connectors, _incremental_dedup, services/sse_broker.broker
    """
    # Allow test mocks to override the broker via module-level patching
    import app.search_service as _self_mod

    from .database import SessionLocal
    from .services.sse_broker import broker as _broker

    active_broker = getattr(_self_mod, "broker", _broker)

    channel = f"search:{search_id}"
    accumulated: list[dict] = []
    total_results = 0
    sources_completed = 0
    t_start = time.time()

    db = SessionLocal()
    try:
        connectors, source_stats_map, _disabled = _build_connectors(db)

        if not connectors:
            await active_broker.publish(
                channel,
                "done",
                json.dumps({"total_results": 0, "sources": 0, "elapsed_seconds": 0}),
            )
            return

        # Build vendor score lookup for scoring raw results
        from .models import VendorCard

        vendor_cards = db.query(VendorCard.normalized_name, VendorCard.vendor_score).all()
        vendor_score_map = {vc.normalized_name: vc.vendor_score for vc in vendor_cards}

        # Create a task per connector, tagging with source_name
        task_map: dict[asyncio.Task, str] = {}
        for conn in connectors:
            source_name = getattr(conn, "source_name", _CONNECTOR_SOURCE_MAP.get(conn.__class__.__name__, "unknown"))

            async def _run(c=conn, pn=mpn):
                t0 = time.time()
                hits = await c.search(pn)
                elapsed = int((time.time() - t0) * 1000)
                return hits, elapsed

            task = asyncio.create_task(_run())
            task_map[task] = source_name

        pending = set(task_map.keys())

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

            for task in done:
                source_name = task_map[task]
                sources_completed += 1

                try:
                    hits, elapsed_ms = task.result()
                    hit_count = len(hits)

                    # Score and normalize each hit
                    scored_hits = []
                    for r in hits:
                        r.setdefault("mpn_matched", mpn)
                        scored_hits.append(_score_raw_hit(r, vendor_score_map))

                    # Incremental dedup against accumulated results
                    new_cards, updated_cards = _incremental_dedup(scored_hits, accumulated)

                    # Publish source status
                    await active_broker.publish(
                        channel,
                        "source-status",
                        json.dumps(
                            {
                                "source": source_name,
                                "status": "ok",
                                "results": hit_count,
                                "ms": elapsed_ms,
                            },
                            default=str,
                        ),
                    )

                    # Publish new result cards (HTML for sse-swap="results" — not JSON)
                    if new_cards:
                        start_idx = len(accumulated) - len(new_cards)
                        cards_html = _render_search_vendor_cards_html(
                            new_cards,
                            search_id=search_id,
                            start_index=start_idx,
                            swap_oob=False,
                        )
                        await active_broker.publish(channel, "results", cards_html)

                    # Publish updated cards as OOB HTML so existing vendor-card nodes refresh
                    if updated_cards:
                        update_html = "".join(
                            _render_search_vendor_cards_html(
                                [card],
                                search_id=search_id,
                                start_index=0,
                                swap_oob=True,
                            )
                            for card in updated_cards
                        )
                        await active_broker.publish(channel, "card-update", update_html)

                    total_results += hit_count

                except Exception as e:
                    logger.warning(f"Streaming search connector {source_name} failed: {e}")
                    await active_broker.publish(
                        channel,
                        "source-status",
                        json.dumps(
                            {
                                "source": source_name,
                                "status": "error",
                                "error": str(e)[:500],
                                "results": 0,
                                "ms": 0,
                            },
                            default=str,
                        ),
                    )

        # Cache results for filter endpoint (15-min TTL)
        try:
            rc = _get_search_redis()
            if rc:
                cache_key = f"search:{search_id}:results"
                rc.setex(cache_key, 900, json.dumps(accumulated, default=str))
        except Exception:
            logger.warning("Failed to cache search results for filtering")

        # All connectors done
        elapsed_total = round(time.time() - t_start, 1)
        await active_broker.publish(
            channel,
            "done",
            json.dumps(
                {
                    "total_results": total_results,
                    "sources": sources_completed,
                    "elapsed_seconds": elapsed_total,
                },
                default=str,
            ),
        )
    finally:
        db.close()
```

Rationale notes for reviewers:
- `SessionLocal` is imported inside the function (not at module top) so the `monkeypatch.setattr(search_service, "SessionLocal", ...)` test seam works — the test replaces the module-level name; the in-function `from .database import SessionLocal` shadows that. To preserve test mocking, fetch `SessionLocal` via the module: see Step 3.

- [ ] **Step 3: Make `SessionLocal` mockable at module level**

The test in Task 1 monkeypatches `search_service.SessionLocal`. For the patch to take effect, `stream_search_mpn` must look up `SessionLocal` on the module, not import it locally. Adjust the body so:

- At the **top of the file**, alongside other module-level imports near the existing `from .database import ...` line (find it via `grep -n "from .database" app/search_service.py | head`), ensure `SessionLocal` is imported:
  ```python
  from .database import SessionLocal
  ```
  If `SessionLocal` is already imported at module scope, no change needed; if not, add it to the existing import line.
- Inside `stream_search_mpn`, **remove** the local `from .database import SessionLocal` line shown in Step 2. Replace `db = SessionLocal()` with:
  ```python
  db = _self_mod.SessionLocal() if hasattr(_self_mod, "SessionLocal") else SessionLocal()
  ```
  …or simpler, just call `SessionLocal()` directly at module scope and rely on the module-level import being patchable. The test uses `monkeypatch.setattr(search_service, "SessionLocal", ...)` which replaces the module attribute, and an unqualified `SessionLocal()` reference inside the function resolves through the module's globals. So:
  ```python
  db = SessionLocal()
  ```
  …with the module-level import is sufficient.

Verify by running: `grep -n "SessionLocal" app/search_service.py` — should show one module-level import and the in-function `db = SessionLocal()` call.

- [ ] **Step 4: Run the failing test from Task 1**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py::test_stream_search_mpn_opens_and_closes_its_own_session -v --override-ini="addopts="`

Expected: **PASS** — the worker now opens and closes its own session.

If the test fails with `AttributeError: <module> has no attribute 'SessionLocal'`, the module-level import in Step 3 is missing — add it.

- [ ] **Step 5: Run the full search_service test module**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py -v`

Expected: all tests pass. If any unrelated test fails because of the signature change, the fix is incomplete — Task 3 catches the caller, but there may be additional callers; grep `stream_search_mpn(` to enumerate.

- [ ] **Step 6: Find every caller of `stream_search_mpn`**

Run: `grep -rn "stream_search_mpn" app/ tests/ scripts/ 2>/dev/null`

Expected callers: `app/routers/htmx_views.py:3023` (fixed in Task 3) and possibly tests. Note any others — they all must be updated to the new signature in this same commit to keep the tree green.

- [ ] **Step 7: Commit (do not split worker fix from caller fix)**

Hold this commit until Task 3 is also ready. The worker change and the caller change must land together or `mypy`/runtime breaks on the next request.

---

## Task 3: Update the caller in `htmx_views.py`

**Files:**
- Modify: `app/routers/htmx_views.py:3023`

- [ ] **Step 1: Read the current caller**

Read `app/routers/htmx_views.py:3017-3024`. The relevant lines today:

```python
    # Generate a unique search ID and launch streaming search in background
    search_id = str(uuid4())
    enabled_sources = _get_enabled_sources(db)

    from ..search_service import stream_search_mpn

    await _safe_bg(stream_search_mpn(search_id, search_mpn, db), task_name="stream_search_mpn")
```

- [ ] **Step 2: Drop the `db` argument**

Edit `app/routers/htmx_views.py:3023` so the call becomes:

```python
    await _safe_bg(stream_search_mpn(search_id, search_mpn), task_name="stream_search_mpn")
```

Do not remove the `db` parameter from `search_run` itself — it is still used at line 3019 (`_get_enabled_sources(db)`) and the inline-MPN-from-requirement lookup at lines 3006-3008.

- [ ] **Step 3: Verify no other caller passes `db`**

Run: `grep -n "stream_search_mpn(" app/ -r`

Expected: only the one updated caller in `htmx_views.py`. If any other call site passes `db`, update it the same way.

- [ ] **Step 4: Type-check**

Run: `mypy app/search_service.py app/routers/htmx_views.py`

Expected: no errors related to `stream_search_mpn`. Pre-existing mypy noise unrelated to this fix is acceptable.

- [ ] **Step 5: Lint**

Run: `ruff check app/search_service.py app/routers/htmx_views.py`

Expected: clean. Auto-fix with `ruff check --fix` only if findings are within the changed lines.

- [ ] **Step 6: Run the focused test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py tests/test_routers.py -v`

Expected: all pass.

- [ ] **Step 7: Run the full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`

Expected: all pass. If any test mocks `stream_search_mpn` with the old 3-arg signature, update those mocks.

- [ ] **Step 8: Commit worker + caller together**

```bash
git add app/search_service.py app/routers/htmx_views.py tests/test_search_service.py
git commit -m "fix(search): give streaming worker its own DB session

stream_search_mpn was being passed the request-scoped Session from
search_run. FastAPI's get_db finalizer closed that session as soon as
the HTML shell was returned, so the fire-and-forget worker crashed on
its first db.query(...) and _safe_bg silently swallowed the error.
The SSE channel never received a single event and the spinner hung
indefinitely.

Fix: drop the db parameter from stream_search_mpn and open a fresh
SessionLocal() inside it, with try/finally close. Mirrors the pattern
already used by _enrich_cards at search_service.py:1775.

Tests: regression test in test_search_service.py asserts the worker
opens, uses, and closes its own session even when the caller's session
is gone. Fails today (TypeError on missing arg), passes after fix."
```

---

## Task 4: Verify in production (post-deploy smoke)

**Files:** none (operational)

- [ ] **Step 1: Deploy**

Run: `cd /root/availai && ./deploy.sh`

`deploy.sh` enforces `--no-cache` build + `--force-recreate` per project rules.

- [ ] **Step 2: Tail logs while triggering a search**

In one terminal: `docker compose logs -f app | grep -E "search/run|search/stream|stream_search|broker"`

In a browser, navigate to the part-search page, enter `LM317`, submit. Expected log sequence:
1. `POST /v2/partials/search/run → 200`
2. `GET /v2/partials/search/stream?search_id=...` (SSE long-poll, status 200)
3. Connector logs: `element14: LM317 -> N results`, `OEMSecrets: LM317 -> N results`, `DigiKey: LM317 -> N results` (these connectors have working creds today).
4. **No `Streaming search connector ... failed: This Session's transaction has been rolled back ...`** or similar SQLAlchemy errors. That was the smoking gun.
5. UI populates with vendor cards as connectors complete; spinner clears at `done` event.

- [ ] **Step 3: Confirm degraded-but-functional behavior**

The UI must:
- Show results from element14, OEMSecrets, DigiKey (working connectors).
- Show error chips for BrokerBin, Mouser, Nexar, Apollo (still-broken creds — out of scope for this PR, tracked as follow-up).
- Render the final result count and stop spinning.

If the UI still hangs, capture the browser network tab (was `/v2/partials/search/stream` opened? did it receive `event: done`?) and the relevant log lines, then return to systematic-debugging Phase 1.

- [ ] **Step 4: Update the relevant APP_MAP doc**

Per project rule: after any code change, update the relevant `docs/APP_MAP_*.md`. The streaming-search worker's session ownership is documented in `docs/APP_MAP_INTERACTIONS.md` (or the architecture doc). Run `grep -ln "stream_search_mpn" docs/` to find references and amend.

```bash
git add docs/APP_MAP_*.md
git commit -m "docs: note that stream_search_mpn owns its own session"
```

- [ ] **Step 5: Open follow-up tickets (or just record them in the PR description)**

Track separately:
1. SSE replay buffer in `app/services/sse_broker.py` (publish-before-subscribe race — small window, low impact, but worth fixing).
2. Connector credential rotation: BrokerBin protocol/auth, Mouser key, Nexar key + quota, Apollo enrichment 401.
3. Duplicate-POST de-dup on `search_id` channel.

---

## Self-Review

**Spec coverage:**
- Root cause (request-scoped session passed to fire-and-forget task) → Tasks 2 & 3.
- TDD requirement (failing test before fix) → Task 1 + Task 2 Step 4.
- Pattern precedent (`_enrich_cards` at line 1775) → cited inline in Task 2.
- Caller updates → Task 3 Step 3 enumerates all callers via grep.
- Verification in prod → Task 4.
- Out-of-scope items (SSE buffer, creds, dedup) → recorded as follow-ups.

**Placeholder scan:** No `TODO`, no "TBD", no "fill in details", no "similar to Task N" without inline content. Test code, fix code, and commit messages are written in full.

**Type consistency:** `stream_search_mpn(search_id: str, mpn: str)` is the single new signature — used identically in test (Task 1), implementation (Task 2), and caller (Task 3).

**Risks called out by sequencing review (encoded in tasks):**
- Session must be opened at top of function and closed in `finally` covering the entire body, not just the prelude (Task 2 Step 1 explicitly states this).
- `getattr(_self_mod, "broker", _broker)` test seam at line 1974 preserved verbatim in Task 2 Step 2.
- `monkeypatch.setattr(search_service, "SessionLocal", ...)` test seam — Task 2 Step 3 ensures `SessionLocal` is module-scope so the patch resolves.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-04-search-bg-session-fix.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
