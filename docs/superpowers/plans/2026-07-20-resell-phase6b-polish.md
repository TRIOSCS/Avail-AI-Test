# Resell Rework — Phase 6b: Polish + Dead-Column Disposition — Implementation Plan

> Built AFTER 6a merges (off the new main). Migration 201 chains off `200_resell_hot_indexes` — set `down_revision="200_resell_hot_indexes"` (re-verify it's the single head at build time). Re-ground every symbol.

**Goal:** Close the Resell silent-failure polish items (500-hardening + lost user feedback) and execute the D6 dead-column drop.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, pytest. One additive-reverse migration (201, drop `valid_until`).

## Global Constraints
- StrEnum statuses; `HTTPException` guards; 2.0-style new code; Query-API ratchet down-only.
- Migration id ≤32; `201_drop_offer_valid_until` (26 chars); claim in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; single head; round-trip on throwaway PG.
- After changes update `docs/APP_MAP_*` docs.

---

### Task 1: convert-to-offer non-positive qty → 400 (silent-failure a)

**Files:** `app/routers/resell.py` (`resell_outreach_convert_offer` guard ~2159), `app/services/resell_outreach_service.py` (`_link_inbound_offer` ExcessOfferLine build ~1196); Tests: `tests/test_resell_outreach_*.py`.
- Router guard ~2159: change `if not mpn_raw.strip() or qty is None:` → `... or qty is None or qty <= 0:` with a 400 "needs a part number and a positive quantity" (mirror the two DONE siblings at resell.py:1434 / 2258).
- Root-cause the coercion at `_link_inbound_offer` ~1196: `quantity=row.get('quantity') or 1` silently promotes 0→1 and lets -5 reach the `@validates` 500 (also on the emailed-bid path via `record_response`). Harden to use the validated int, not `or 1`; reject non-positive before the model.
- [ ] Failing tests: convert with qty=0 → 400 (not 500, not silent 1); qty=-5 → 400; the emailed-bid `record_response` path rejects/handles non-positive without a 500. → red → fix both sites → green → commit.

### Task 2: confirm_import surfaces skipped-row count (silent-failure b)

**Files:** `app/routers/resell.py` (`resell_import_confirm` ~1348), reuse the `_toast`/HX-Trigger helper (~1462); Tests: `tests/test_resell_*import*.py`.
- Capture `result = excess_service.confirm_import(...)` (currently discarded); when `result['skipped'] > 0` attach an `HX-Trigger` `showToast` (warning) — "N row(s) skipped (invalid quantity or blank part number)" — on the returned `template_response` (mutable Response; set `resp.headers['HX-Trigger']` like `_toast`).
- [ ] Failing test: import a batch with some invalid rows → response carries the HX-Trigger showToast with the skipped count; a clean import carries none. → red → implement → green → commit.

### Task 3: assemble-bid per-element dict guard → 400 (silent-failure c)

**Files:** `app/routers/resell.py` (`resell_assemble_bid` ~890); Tests: `tests/test_resell_bid_lifecycle.py`.
- After the existing `if not isinstance(raw, list) or not raw:` add `if not all(isinstance(s, dict) for s in raw): raise HTTPException(400, "Invalid bid payload")` BEFORE the `s.get(...)` comprehension (~893) so `[1,2]`/`["x"]` → 400, not an AttributeError 500.
- [ ] Failing test: POST assemble-bid with a JSON list of non-dicts → 400 (not 500). → red → implement → green → commit.

### Task 4: corrupt-upload distinct error (silent-failure e)

**Files:** `app/file_utils.py` (`parse_tabular_file` ~68-91), callers `app/routers/resell.py` (~1314), `app/main.py` (upload_requirements/import_stock_list), `app/scheduler.py` (_parse_stock_list_file); Tests: `tests/test_file_utils.py` (update `test_corrupt_file_returns_empty` ~100).
- Root-cause: `parse_tabular_file`'s blanket `except Exception → return []` makes a corrupt file indistinguishable from a genuinely-empty one (both → "No data rows found"). Signal a hard parse failure distinctly (raise a typed `ParseError` on the caught exception instead of returning `[]`). Update ALL callers: resell.py renders "We couldn't read this file — it may be corrupt or not a valid spreadsheet" vs the genuine-empty "No data rows found"; main.py + scheduler.py handle the typed error. Update the test that locked in return-`[]`.
- **CAUTION — shared util:** `parse_tabular_file` is used by 3 modules; a contract change must update every caller. Verify each caller path.
- [ ] Failing tests: a corrupt/unreadable upload → distinct "couldn't read this file" message (resell preview); a genuinely empty file → "No data rows found"; main.py + scheduler.py callers handle the typed error. → red → implement → green → commit.

### Task 5: D6 — drop `excess_offers.valid_until` + stale comment (migration 201)

**Files:** `alembic/versions/201_drop_offer_valid_until.py` (NEW), `MIGRATION_NUMBERS_IN_FLIGHT.txt`, `app/models/excess.py` (remove `valid_until` ~156; fix the stale `status` comment ~157 — remove the non-existent "expired"), `app/services/excess_service.py` (`submit_offer` — remove the `valid_until` param ~510 + assignment ~563), `app/schemas/excess.py` (remove `valid_until` from `ExcessOfferCreate` ~184 / `ExcessOfferResponse` ~201 — both unused); Tests: `tests/test_models_excess.py` etc. (drop any `valid_until` assertion).
- Staging has 0 non-null `valid_until` rows → no data loss. Model removal MUST ship WITH the migration or the fresh-DB drift gate emits a `remove_column` diff and fails.
- Migration `201_drop_offer_valid_until`: `upgrade()` drop the column; `downgrade()` re-add it (`Column(UTCDateTime, nullable=True)`) — additive-reverse, no data to restore. `down_revision="200_resell_hot_indexes"`. Round-trip on throwaway PG; single head.
- **Leave DORMANT (no action):** `excess_lists.version` (D4 = dispose later), `excess_lists.total_line_items` (maintained, cosmetic). **DEFERRED (documented, not done):** dropping the orphan `excess_line_items.market_price`/`demand_score` — they exist on legacy staging but not a fresh DB (drift gate green); dropping is an irreversible data-column op not explicitly decided, and the autogenerate-against-staging trap is already mitigated by the throwaway-DB rule. Note in the PR body.
- [ ] Failing test: model no longer has `valid_until`; migration round-trips; drift gate green. → red → implement → green → commit.

---

## Self-Review
- **Coverage:** silent-failure a (T1), b (T2), c (T3), e (T4); D6 valid_until drop (T5). ✓
- **Already-done (skip):** nightly-expiry per-list isolation (Phase 5); qty guards on submit_offer + log_bid (only convert was missing). ✓
- **Deferred (documented):** market_price/demand_score orphan drop; version/total_line_items stay dormant. ✓
- **Migration:** one (201, drop valid_until), id 26 chars ≤32, claimed, single head, 0 data loss. ✓
