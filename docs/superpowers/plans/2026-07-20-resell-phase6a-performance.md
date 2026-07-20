# Resell Rework — Phase 6a: Performance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Re-verify every symbol against the CURRENT worktree — work by symbol, not the numbers here.

**Goal:** Retire the Resell hot-path performance findings: the `rank_buyers_for` candidate over-fetch (#19), the outreach-submit per-(buyer×line) query storm (#20), three verified N+1/joinedload gaps, the three missing hot indexes (migration 200), and the untested M9 award-race lock (RESELL-TEST-3).

**Architecture:** Services (`buyer_affinity_service.py`, `resell_outreach_service.py`) + routers (`resell.py`) + models (`excess.py`, `offers.py`) + one additive index migration (200). Reuse the CSV-export "twin" endpoints as the joinedload patterns to copy. Postgres-specific query paths MUST have a SQLite fallback (tests run on in-memory SQLite).

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16 (JSONB `?|`, GIN), pytest (xdist, `@requires_postgres` infra in conftest).

## Global Constraints
- SQLAlchemy 2.0 style; StrEnum statuses; `HTTPException` guards. Query-API ratchet is down-only — the existing legacy `db.query()` sites being edited may stay `db.query()` (do not increase the count; do not convert a collection-`joinedload` query to `select()` without `.unique()`).
- **Migration id ≤ 32 chars**; `200_resell_hot_indexes` (22 chars), `down_revision="199_sighting_excess_line_fk"` (verified single head). Claim 200 in `MIGRATION_NUMBERS_IN_FLIGHT.txt` same commit. Model `__table_args__` index names MUST equal the migration's created names (drift gate) — round-trip on throwaway PG.
- **No behavior change** — these are pure perf refactors. Preserve every return shape, especially `ExcessOutreach.parts_included` JSON keys (`part_number`, `quantity`, `line_item_id`) — persisted + read back by retry/reply paths.
- Any Postgres-only operator (`?|`) MUST be `dialect`-branched with a Python fallback so the SQLite test path and any non-PG env still work.
- After changes update `docs/APP_MAP_INTERACTIONS.md` (perf notes) + `docs/APP_MAP_DATABASE.md` (3 new indexes).

---

### Task 1: `rank_buyers_for` genuine candidate narrowing + SQL tier push-down (#19)

**Files:** `app/services/buyer_affinity_service.py` (`rank_buyers_for` ~195, `candidate_filters`/candidates query ~262-272, `_reachable_card_ids` ~156, `_target_commodities` ~147); Test: `tests/test_buyer_affinity_service.py`.

**Problem:** `commodity_tags.isnot(None)` matches the default `[]` so the candidate set is still ~all cards; the ~99.7% discard just moved into the Python loop. Full VendorCard ORM entities are hydrated though only 4 columns are read.

**Interfaces:**
- Replace the ineffective `commodity_tags.isnot(None)` predicate with a genuine tag-overlap candidate branch:
  - **Postgres:** `VendorCard.commodity_tags.op("?|")(cast(target_array, ARRAY(Text)))` using the existing GIN index `ix_vendor_cards_commodity_tags_gin` (migration 082 — **no new index**). Reconcile the case-sensitivity mismatch: `?|` is exact/case-sensitive but the Python tier loop compares lowercased both sides — build the `target_array` to match the ACTUAL stored tag casing (or normalize); document the choice.
  - **SQLite (tests/fallback):** keep the current Python set-intersection path (dialect-branch on `db.bind.dialect.name`).
- Bound the engagement (Tier-3) branch with `ORDER BY engagement_score DESC NULLS LAST LIMIT (limit*3)` in SQL instead of loading every engagement-scored card (whole result is capped at `limit`).
- Project only the 4 read columns: `db.query(VendorCard.id, VendorCard.display_name, VendorCard.engagement_score, VendorCard.commodity_tags)` (not full ORM rows).
- Run `_reachable_card_ids` over the REDUCED union (history_ids ∪ tag-overlap ∪ top-N engagement), not the near-all list.

- [ ] Failing tests first: (a) a card with a genuinely OVERLAPPING commodity tag is a candidate; a card with only `[]` tags and no engagement/history is NOT (proves the predicate narrows); (b) the result is still capped at `limit` and the panel is never starved (engagement tiebreak still fills); (c) the SQLite fallback path returns the same ranked ids as before for the existing fixtures (no regression). → red → dialect-branched implement → green → commit. **Verify the PG `?|` path on real PostgreSQL** (the throwaway PG used for the migration round-trip, or note it for live-verify) since SQLite can't exercise `?|`.

---

### Task 2: Outreach-submit query batching + single flush (#20)

**Files:** `app/services/resell_outreach_service.py` (`_parts_snapshot` ~191, `_make_outreach_rows` ~224, `submit_outreach` ~313, `enqueue_outreach_email` ~385, `_target_line_ids` ~167); Test: `tests/test_resell_outreach_*.py` (+ a query-count assertion).

**Interfaces:**
- Precompute ONE campaign snapshot: a single `select(ExcessLineItem).where(excess_list_id==el.id)` → `whole_list_snapshot = [{"part_number","quantity","line_item_id"} …]` + `by_line = {li.id: [that line's dict]}`. Fuse/reuse `_target_line_ids`'s existing per_line query (it already fetches all lines but keeps only `li.id`); for whole_list scope it returns `[None]` WITHOUT querying, so the snapshot query must run unconditionally.
- Convert `_parts_snapshot` to a pure in-memory lookup: `by_line.get(line_id, []) if line_id is not None else whole_list_snapshot` — **preserve the exact dict keys** (persisted to `parts_included`).
- Thread the snapshot map into `_make_outreach_rows` (new param) and `enqueue_outreach_email`'s email-body parts comprehension so both read the map instead of querying.
- Move `db.flush()` out of `_make_outreach_rows` (per-buyer) to a SINGLE flush after the per-buyer loop. CAVEAT: `enqueue_outreach_email` reads `r.id` to build the plan row_ids — restructure to collect row objects, flush once, THEN capture ids.

- [ ] Failing tests first: a query-count test around a multi-buyer × multi-line campaign asserts the `ExcessLineItem` snapshot SELECT runs ONCE (not B×L); the persisted `parts_included` payload is byte-identical to before (same keys/values); retry still matches. → red → implement → green → commit. (Note: `_has_live_recent_outreach`'s separate dedup query is out of scope — do not claim it's eliminated.)

---

### Task 3: Three N+1 / joinedload fixes

**Files:** `app/routers/resell.py` (`_offers_context` ~565, `_award_response_context` ~657, `_detail_context` ~335, `_outreach_tracker_context` ~1686, `resell_outreach_reply` ~2105, `_replies_context` ~1727); Tests: `tests/test_resell_offers.py`, `tests/test_resell_reply_routes.py`.

**Interfaces:**
- **Offers-tab (finding 1):** add the export twin's joinedloads (`joinedload(ExcessOffer.offerer_company)`, `joinedload(ExcessOffer.offerer_vendor_card)`, `joinedload(ExcessOffer.lines).joinedload(ExcessOfferLine.excess_line_item)`) to BOTH owner-path queries (`take_all_offers`, `per_line_offers`). Keep them `db.query()` (legacy auto-uniques the collection joinedload — do NOT switch to `select()` without `.unique()`).
- **Doubled line-items query:** `_award_response_context` merges `_detail_context` + `_offers_context`, each running the identical `ExcessLineItem` SELECT. Load the line-items once and thread into both (optional preloaded-items param) so the award-response render runs it once.
- **Tracker (finding 2):** add the export twin's joinedloads (`target_vendor_card`, `excess_line_item`, `submitted_by_user` — all many-to-one, no `.unique()`) to the `_outreach_tracker_context` rows query (this runs inside the 3s poll).
- **Reply (finding 3):** `resell_outreach_reply` builds the whole-list conversation map then uses one conversation. Replace `_replies_context` with a narrow `_conversation_replies(db, conversation_id)` that queries only `VendorResponse.graph_conversation_id == outreach.graph_conversation_id` ordered newest-first. `_replies_context` has exactly one caller → replace it (don't orphan it — dead-code gate) and retarget `tests/test_resell_reply_routes.py::test_joins_and_orders_newest_first`.

- [ ] Failing tests first: query-count/N+1 regression tests for the owner offers render and the tracker render (bounded SELECT count across the joinedloaded relations); a test asserting the award-response path issues the line-items SELECT once; a reply test asserting only the target conversation's `VendorResponse` rows load. → red → implement → green → commit.

---

### Task 4: Three missing hot indexes — migration 200

**Files:** `alembic/versions/200_resell_hot_indexes.py` (NEW), `MIGRATION_NUMBERS_IN_FLIGHT.txt`, `app/models/excess.py` (`ExcessOutreach.__table_args__`, `ExcessOffer.__table_args__`), `app/models/offers.py` (`VendorResponse.__table_args__`); Test: covered by the drift gate + the Task-3 reply query.

**Interfaces:**
- `ix_excess_outreach_message` on `excess_outreach.graph_message_id` (note: `graph_conversation_id` is ALREADY indexed — the missing one is `graph_message_id`).
- `ix_excess_offers_vendor_card` on `excess_offers.offerer_vendor_card_id`.
- `ix_vr_conversation` on `vendor_responses.graph_conversation_id` (serves the Task-3 single-conversation query).
- Migration `200_resell_hot_indexes` (`down_revision="199_sighting_excess_line_fk"`): `create_index` ×3 up, `drop_index` ×3 down (reverse). Each index name declared in the model `__table_args__` AND created in the migration (drift-gate parity). Follow the `195_outreach_send_subject_body.py` template.

- [ ] Add the 3 model indexes + write the migration; claim 200 in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; round-trip upgrade→downgrade→upgrade on a THROWAWAY PG16 (not staging); confirm `alembic heads` == single head `200_resell_hot_indexes`; the fresh-DB drift gate stays green. → commit.

---

### Task 5: RESELL-TEST-3 — `@requires_postgres` award-race concurrency test

**Files:** `tests/test_resell_award.py`; (no app change — proves the existing `_lock_list_for_award` M9 lock).

**Interfaces:** A real `@requires_postgres` test that proves `with_for_update` serializes concurrent awards (a no-op on SQLite). Request `pg_engine` directly, open TWO independent sessions via `sessionmaker(bind=pg_engine)`, seed one list + one overlapping line, run two threads each calling `award_offer` on a DIFFERENT open offer touching that line, synchronized with a `threading.Barrier` so both enter `_lock_list_for_award` concurrently. Assert exactly ONE award succeeds (offer→won, line→awarded) and the other raises `HTTPException(409)` (no double-award); buyer-score/mirror hooks fire once. Manually clean up the two extra sessions (the `pg_session` TRUNCATE teardown covers only its own session).

- [ ] Write the test; it SKIPs without `PG_TEST_DSN` and PASSes against real PG. Verify it actually fails if `_lock_list_for_award` is stubbed to a no-op (proves it tests the lock). → commit.

---

## Self-Review
- **Coverage:** #19 (T1), #20 (T2), 3 N+1 (T3), indexes migr 200 (T4), RESELL-TEST-3 (T5). ✓
- **Migration:** one (200, additive indexes), id 22 chars ≤ 32, claimed, single head, model/migration parity. ✓
- **Dialect safety:** the `?|` push-down is PG-branched with a SQLite Python fallback — tests stay green on SQLite. ✓
- **No behavior change:** `parts_included` keys preserved; joinedloads don't alter results; concurrency test is additive. ✓
- **Ratchet:** no new legacy `db.query()`; collection joinedloads keep their auto-uniquing query form. ✓
