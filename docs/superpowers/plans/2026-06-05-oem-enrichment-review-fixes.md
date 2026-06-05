# OEM Enrichment — Review-Fix Plan (round 2)

> Executes the consolidated findings from the 7-agent PR review of branch `worktree-oem-enrichment`. TDD where a behavior changes. Worktree: `/root/availai/.claude/worktrees/oem-enrichment`; run tests with `TESTING=1 PYTHONPATH=$(pwd) pytest <paths> -q` (use `pytest` directly). Baseline before round 2: full suite 14503 passed.

**Do the tasks in order (RF1 first — the `WebMeter` type everything else uses).** Commit per task with the given message. After all tasks: full suite + `pre-commit run --files <changed>`.

---

## RF1 — `WebMeter` type + reserve-before-await metering fix

**Why:** 4 reviewers — billable web call that bills then raises `ClaudeError` is never counted (`WEB_DAILY_CAP` drifts over); loose `dict` meter has silent-key-typo risk across 3 modules.

**RF1.1 — Add `WebMeter` to `app/services/enrichment_types.py`** (after the imports, add `from dataclasses import dataclass`):
```python
@dataclass
class WebMeter:
    """Mutable per-card budget/health meter threaded through ``enrich_card``.

    ``web_calls`` counts billable web-search-enabled Claude tier attempts; it is
    RESERVED *before* each dispatch so a call that bills then raises is still counted.
    ``claude_ok`` latches True after any Claude call returns without raising. The worker
    uses ``web_calls`` for the daily web budget and ``claude_ok`` to reset its breaker.
    """

    web_calls: int = 0
    claude_ok: bool = False

    def reserve_web_call(self) -> None:
        """Count one billable web-search tier attempt. Call BEFORE the await."""
        self.web_calls += 1

    def mark_claude_ok(self) -> None:
        """Latch that a Claude call returned without raising. Call AFTER the await."""
        self.claude_ok = True
```

**RF1.2 — `app/services/authoritative_enrichment_service.py`**: import `from app.services.enrichment_types import WebMeter`. Change `enrich_card` signature `web_meter: dict | None = None` → `web_meter: WebMeter | None = None`. Replace each of the three metering blocks so the reserve happens BEFORE the await and the ok-latch AFTER:

Distributor web tier:
```python
    if web_enabled:
        if web_meter is not None:
            web_meter.reserve_web_call()
        web = await extract_part_from_web(card.display_mpn, card.normalized_mpn)
        if web_meter is not None:
            web_meter.mark_claude_ok()
        if web.status == "web_sourced":
            apply_web_sourced(card, web)
            return MaterialEnrichmentStatus.WEB_SOURCED
```
Cross-ref tier:
```python
        if web_meter is not None:
            web_meter.reserve_web_call()
        xr = await cross_reference_mpn(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter.mark_claude_ok()
        if xr.status == "resolved" and xr.resolved_mpn:
            ...
```
OEM-description tier:
```python
        if web_meter is not None:
            web_meter.reserve_web_call()
        oem = await extract_oem_description(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter.mark_claude_ok()
        if oem.status == "oem_sourced":
            ...
```
infer path (no web reserve, only latch):
```python
    inf = await infer_part(card.display_mpn)
    if web_meter is not None:
        web_meter.mark_claude_ok()
```
Update the `web_meter` docstring paragraph: `web_calls` "counts each web-search-enabled Claude tier attempt (distributor / cross-ref / OEM-description), reserved before dispatch."

**RF1.3 — `app/services/enrichment_worker/worker.py` `run_one_batch`**: import `WebMeter`. Replace `card_meter = {"web_calls": 0, "claude_ok": False}` → `card_meter = WebMeter()`. Pass `web_meter=card_meter`. Change `card_meter["claude_ok"]` → `card_meter.claude_ok`, `card_meter["web_calls"]` → `card_meter.web_calls`. **Move the budget flush into a `finally`** on the per-card try so calls that fired before a later-tier raise are still billed:
```python
        card_meter = WebMeter()
        try:
            status = await enrich_card(card, db, connectors=conns, disabled=disabled, cooldown=cooldown, web_meter=card_meter)
            card.enriched_at = now
            counts[status] = counts.get(status, 0) + 1
            if card_meter.claude_ok:
                breaker.record_claude_success()
        except ClaudeError as e:
            # (unchanged body — record_claude_error, log)
            ...
        except Exception as e:
            # (unchanged body — quarantine not_found)
            ...
        finally:
            if card_meter.web_calls > 0:
                web_calls_today += card_meter.web_calls
                intel_cache.set_cached(web_cache_key, {"count": web_calls_today}, ttl_days=1.0)
```
Soften the `run_one_batch` docstring line claiming the cap "cannot overshoot ... by up to batch_size" → "a single card may fire up to 3 billable web calls, so the per-card gate can overshoot the cap by at most 2; the meter is flushed in a finally so every dispatched call is billed even when a later tier raises."

**RF1.4 — `scripts/backfill_oem_enrichment.py`**: `from app.services.enrichment_types import WebMeter`; `meter = WebMeter()`; `web_total += meter.web_calls`. (Per-card try already runs after the await in-loop, so flush is fine.)

**RF1.5 — Update existing meter tests** in `tests/test_enrichment_worker.py` and `tests/test_backfill_oem_enrichment.py`: any `fake_enrich_card(..., web_meter=...)` that did `web_meter["web_calls"] += N` / `web_meter["claude_ok"] = True` becomes `web_meter.reserve_web_call()`×N (or `web_meter.web_calls += N`) / `web_meter.mark_claude_ok()`; any literal `{"web_calls": 0, "claude_ok": False}` → `WebMeter()`; any `meter["web_calls"]`/`meter["claude_ok"]` read → attribute.

Run: `pytest tests/test_enrichment_worker.py tests/test_backfill_oem_enrichment.py tests/test_authoritative_enrichment.py -q`. Commit: `fix(enrichment): WebMeter type + reserve-before-dispatch web budget (no undercount on ClaudeError)`.

---

## RF2 — `enrich_card` correctness fixes

**RF2.1 — OEM_SOURCED re-enrichment guard** (`authoritative_enrichment_service.py:307`): a direct `enrich_cards` call on an already-`oem_sourced` card would re-run the OEM tiers (2 web calls). Extend the early-exit:
```python
    if card.enrichment_status in (MaterialEnrichmentStatus.VERIFIED, MaterialEnrichmentStatus.OEM_SOURCED) and not refresh:
        return card.enrichment_status
```

**RF2.2 — Dell low-precision → `not_found`, not `not_catalogued`** (terminal block ~384). The Dell 5-char pattern is broad; a genuine 5-char MPN that misses earlier tiers must not be parked 30 days. In `app/services/enrichment_worker/oem_classifier.py` add:
```python
# Vendors whose patterns are precise enough that an OEM-tier miss means "genuinely an
# uncatalogued OEM service part" (-> not_catalogued, 30-day backoff). The broad Dell
# 5-char pattern is excluded: a miss there is more likely a generic part, so it stays
# not_found (22h retry) instead of being parked for a month.
HIGH_PRECISION_VENDORS: frozenset[str] = frozenset({"lenovo", "ibm", "hpe", "acer", "asus"})
```
Import it in `authoritative_enrichment_service.py` and change the terminal:
```python
    card.enrichment_status = (
        MaterialEnrichmentStatus.NOT_CATALOGUED
        if (vendor in HIGH_PRECISION_VENDORS and oem_attempted)
        else MaterialEnrichmentStatus.NOT_FOUND
    )
```
(`vendor in HIGH_PRECISION_VENDORS` is False when `vendor is None`, so the `vendor` truthiness check is subsumed.)

**RF2.3 — `enrich_cards` counts dict** (~401): add the two new tiers so callers reading `counts["oem_sourced"]`/`["not_catalogued"]` don't `KeyError`:
```python
    counts: dict[str, int] = {
        MaterialEnrichmentStatus.VERIFIED: 0,
        MaterialEnrichmentStatus.WEB_SOURCED: 0,
        MaterialEnrichmentStatus.OEM_SOURCED: 0,
        MaterialEnrichmentStatus.AI_INFERRED: 0,
        MaterialEnrichmentStatus.NOT_FOUND: 0,
        MaterialEnrichmentStatus.NOT_CATALOGUED: 0,
    }
```

**RF2.4 — `apply_cross_ref_verified` confirmer dedupe** (~235): compute once:
```python
    confirmer = contributors[0] if contributors else None
    prov["cross_ref"] = {... "confirmed_by": confirmer ...}
    card.enrichment_source = confirmer or "cross_ref"
```

**RF2.5 — Docstring**: `enrich_card` one-line summary → `"authoritative -> web -> OEM cross-ref/description -> flagged AI inference -> not_catalogued/not_found."`

Run: `pytest tests/test_authoritative_enrichment.py -q`. Commit: `fix(enrichment): oem_sourced re-enrich guard, Dell->not_found, enrich_cards counts, confirmer dedupe`.

---

## RF3 — Worker per-tier observability + migration

**Why:** the worker's per-tier heartbeat/daily-summary silently omit `oem_sourced`/`not_catalogued`.

**RF3.1 — Model** `app/models/enrichment_worker_status.py`: add after `not_found_today`:
```python
    oem_sourced_today = Column(Integer, default=0, server_default="0", nullable=False)
    not_catalogued_today = Column(Integer, default=0, server_default="0", nullable=False)
```

**RF3.2 — Migration** `alembic/versions/089_oem_enrichment_status_columns.py` (down_revision = `088_enrichment_worker_status`):
- `upgrade`: `op.add_column("enrichment_worker_status", sa.Column("oem_sourced_today", sa.Integer(), nullable=False, server_default="0"))` and same for `not_catalogued_today`; then `op.execute("COMMENT ON COLUMN material_cards.enrichment_status IS 'unenriched|verified|web_sourced|oem_sourced|ai_inferred|not_found|not_catalogued (see MaterialEnrichmentStatus)'")`.
- `downgrade`: `op.execute("COMMENT ON COLUMN material_cards.enrichment_status IS NULL")`; `op.drop_column(...)` both.
- Include the standard file-header docstring. Verify single head: `alembic heads` → one. Test upgrade→downgrade→upgrade on the dev DB if available; otherwise rely on the model+migration parity (tests use `Base.metadata.create_all`, so new columns appear automatically).

**RF3.3 — Worker loop** `worker.py main()`: add `oem_sourced_today = 0` and `not_catalogued_today = 0` alongside the other accumulators; in the daily-reset block reset them and include them in the `daily_stats_json` dict + the daily-summary log; accumulate `oem_sourced_today += batch_counts.get(MaterialEnrichmentStatus.OEM_SOURCED, 0)` and `not_catalogued_today += batch_counts.get(MaterialEnrichmentStatus.NOT_CATALOGUED, 0)`; pass both to `update_enrichment_worker_status(...)` in the heartbeat.

**RF3.4 — `select_batch` docstring**: add a bullet — `not_catalogued: eligible when enriched_at IS NULL OR older than not_catalogued_retry_days (long backoff).`

Run: `pytest tests/test_enrichment_worker.py -q`. Commit: `feat(enrichment): worker per-tier counters for oem_sourced/not_catalogued + migration 089`.

---

## RF4 — Backfill robustness

**RF4.1 — Session lifecycle (root-cause, no band-aid)** `scripts/backfill_oem_enrichment.py`: give `run()` an injectable session and close only an owned one:
```python
async def run(*, commit, limit, max_web_calls, csv_path, db=None) -> dict:
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        ...  # existing body
    except Exception:
        db.rollback()
        raise
    finally:
        if owns_session:
            db.close()
```
Keep the existing `except Exception: db.rollback(); raise` (now nested inside, or fold into the same try with both except+finally).

**RF4.2 — `ClaudeError` early-abort**: import `from app.utils.claude_errors import ClaudeError`. In the loop, catch `ClaudeError` separately, count `counts["claude_error"]`, track consecutive count, and `break` after 5 consecutive (an outage — stop burning budget); reset the consecutive counter on any non-Claude outcome:
```python
            try:
                status = await enrich_card(card, db, connectors=conns, disabled=disabled, cooldown=cooldown, web_meter=meter)
                consecutive_claude_errors = 0
            except ClaudeError:
                consecutive_claude_errors += 1
                counts["claude_error"] = counts.get("claude_error", 0) + 1
                status = "claude_error"
                logger.warning("BACKFILL: {} Claude error ({} consecutive)", card.display_mpn, consecutive_claude_errors)
                web_total += meter.web_calls
                if consecutive_claude_errors >= 5:
                    logger.error("BACKFILL: 5 consecutive Claude errors — aborting (backend outage)")
                    rows.append({...this card...}); break
            except Exception as e:
                logger.warning("BACKFILL: {} failed: {}", card.display_mpn, type(e).__name__)
                status = "error"
```
(Initialize `consecutive_claude_errors = 0` before the loop. Keep `web_total += meter.web_calls` once per card on the success/other path as today.)

**RF4.3 — `--dry-run`/`--commit` mutual exclusion** (make `--dry-run` meaningful): use a mutually-exclusive argparse group so passing both errors, and `--dry-run` is an explicit opt-in:
```python
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Preview only; roll back (default).")
    g.add_argument("--commit", action="store_true", help="Persist results.")
```

**RF4.4 — Tests** (`tests/test_backfill_oem_enrichment.py`): switch to injecting `db=db_session` (drop the `SessionLocal` patch); tighten `test_budget_cap_halts` to assert `counts["processed"] == 2` and `counts["web_calls"] == 4`; add `test_select_includes_not_catalogued` (a `not_catalogued` seed is picked up); add `test_bad_card_does_not_abort_run` (a card whose `enrich_card` raises a non-Claude `Exception` → row recorded `status="error"`, run continues); add `test_consecutive_claude_errors_abort` (≥5 `ClaudeError` → loop breaks early).

Run: `pytest tests/test_backfill_oem_enrichment.py -q`. Commit: `fix(scripts): backfill session lifecycle + ClaudeError early-abort + dry-run/commit exclusivity`.

---

## RF5 — `oem_extractor` hardening + comments

**RF5.1 — `frozen=True` + `Literal` status** on the result dataclasses (kills the shared-mutable-singleton footgun; moves the closed set into the type). In `app/services/enrichment_worker/oem_extractor.py` add `from typing import Literal`, and:
```python
@dataclass(frozen=True)
class CrossRefResult:
    status: Literal["resolved", "failed"]
    ...
@dataclass(frozen=True)
class OemExtractResult:
    status: Literal["oem_sourced", "failed"]
    ...
```
The producers already build the full object in one `return` (no post-construction mutation), and consumers only read — so `frozen=True` is safe. Keep the `_XR_FAILED`/`_OEM_FAILED` module singletons (now immutable). Do NOT touch the pre-existing `web_extractor.py` (out of scope).

**RF5.2 — Linkage single-attestation comment**: above the gate-2 block in `cross_reference_mpn`, add a comment documenting the accepted residual:
```python
    # Linkage gate: the FRU<->MPN association is single-attestation (the model's quoted
    # text), checked as a normalized-substring containment of BOTH codes. This is the one
    # place an LLM claim is load-bearing for the *linkage*; it is defended in depth — the
    # source domain must be allowlisted (gate 1), the resolved MPN is INDEPENDENTLY
    # re-verified against a distributor by the caller, and confidence must clear 0.90.
    # A short code embedded in a longer token is an accepted residual (see review notes).
```

**RF5.3 — Module header `Depends on:`** add `app.utils.claude_errors`.

Run: `pytest tests/test_oem_extractor.py tests/test_authoritative_enrichment.py -q`. Commit: `refactor(enrichment): frozen+Literal oem_extractor results + linkage residual comment`.

---

## RF6 — New tests (lock the contracts the review found untested)

In `tests/test_authoritative_enrichment.py`:
- `test_web_meter_exact_counts_per_tier`: real `enrich_card` (mocks for the extractors/infer) asserting `meter.web_calls == 1` (web_sourced path), `== 2` (cross-ref verified), `== 3` (oem_sourced and not_catalogued paths).
- `test_web_meter_counts_billed_call_on_claude_error`: cross-ref raises `ClaudeError` after the distributor-web reserve → `meter.web_calls >= 2` and the exception propagates (reserve-before guarantees the billed call is counted).
- `test_ai_inferred_sets_claude_ok_without_web_call`: web disabled, vendor None, infer→ai_inferred → `meter.web_calls == 0 and meter.claude_ok is True`.
- `test_dell_miss_is_not_found_not_catalogued`: `classify_oem_vendor` returns `"dell"`, OEM tiers attempted and fail, infer declines → terminal `NOT_FOUND` (not `not_catalogued`).

In `tests/test_enrichment_worker.py`:
- `test_breaker_resets_on_claude_ok_without_web`: spy/stub breaker; `fake_enrich_card` sets `meter.mark_claude_ok()` and `web_calls==0` → `record_claude_success` called, budget unchanged.
- `test_verified_only_does_not_reset_breaker`: `fake_enrich_card` returns VERIFIED with `claude_ok False`, `web_calls 0` → `record_claude_success` NOT called.

In `tests/test_oem_domains.py`:
- `test_ibm_and_uppercase_host`: `is_oem_domain("https://www.ibm.com/support/x")` True; `is_oem_domain("HTTPS://SUPPORT.LENOVO.COM/x")` True (host lowercased); `is_oem_domain("https://notlenovo.com/x")` False.

In `tests/test_oem_classifier.py`:
- `test_dell_pattern_is_broad_known_tradeoff`: document that a 5-char alnum-with-letter (e.g. `"LM317"`) classifies `"dell"` (accepted false-positive) — pins the breadth so a future change is deliberate.

In `tests/test_oem_badges.py`: remove the dead `tmpl.module if False else` expression → just `tmpl.render(...)`.

Run the full new+touched set, then the whole suite. Commit: `test(enrichment): real-meter, breaker-reset, backfill, domain + classifier edge coverage`.

---

## Final verification (after RF1–RF6)
- `TESTING=1 PYTHONPATH=$(pwd) pytest tests/ -q` — green (≥ 14503 + new).
- `ruff check app/ scripts/ && ruff format --check app/ scripts/`.
- `pre-commit run mypy --files <all changed .py>` — passes.
- `alembic heads` — single head (089).
- `npm run build` unaffected (no JS changed in round 2).

## Deliberately NOT changed (documented decisions — surface to user)
- **Linkage gate substring logic** — kept (token-exact matching would false-reject separator-bearing OEM codes like `60NB0690-MB1820`/`NB.MBC11.003`); residual is accepted + commented + defended in depth (RF5.2). Offer stricter mode if the user wants it.
- **`web_extractor.py` / `WebExtractResult`** — pre-existing; not frozen/Literal-ized here (drift-bundling discipline).
- **`fetch_authoritative` broad-except escalation** — pre-existing; out of scope.
