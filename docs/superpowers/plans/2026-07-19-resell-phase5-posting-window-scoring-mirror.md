# Resell Rework — Phase 5: Posting Window + Scoring + Mirror — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Re-verify every symbol against the CURRENT worktree — Phases 1-4 moved line numbers; work by symbol, not by the numbers copied here.

**Goal:** Give the posting-deadline subsystem a real entry point and stop labeling resolved lists red "Overdue" (D1/#8); make the Sighting mirror line-identity-correct so duplicate-part lines and partial awards stop hiding live supply (#18); add the missing nightly BuyerScore backstop (#17 core); and stop list/company deletion from stranding mirror rows (P2 teardown).

**Architecture:** Thin routers (`app/routers/resell.py`) → services (`app/services/excess_service.py`, `excess_mirror.py`, `buyer_affinity_service.py`) → jobs (`app/jobs/resell_jobs.py`). One additive migration (199) for the Sighting FK. Reuse established patterns: the close/draft guards, the `_job_*` wrapper shape, the chip macro (gated at the resell-template level, never edited in the shared macro).

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (2.0-style only — ratchet is down-only), HTMX/Alpine/Jinja2, Alembic, pytest.

## Global Constraints
- Status/enum values ALWAYS from `app/constants.py`. Use `db.get(Model, id)`, 2.0-style selects, `HTTPException` guards.
- **Migration id ≤ 32 chars** (staging `alembic_version` is VARCHAR(32)). Claim the number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` in the same commit; verify `alembic heads` == single head.
- **Never edit the shared `time_text` macro** (`_macros.html`) — it's used by requisitions too. Gate the chip at the resell template level.
- No new UI *conventions* — reuse the existing `create_modal.html` `<select name="company_id">` pattern for the two net-new inputs: the optional "Offers close by" date on the create modal (D1, Task 1) and the optional "Buyer" selector on the offer form (Task 5). No other UI additions.
- After changes, update `docs/APP_MAP_INTERACTIONS.md` (posting-deadline flow + mirror line-identity + teardown + nightly buyer-score job + UI-submit offer attribution) and `docs/APP_MAP_DATABASE.md` (new `sightings.excess_line_item_id`).

---

### Task 1: Posting-window entry point + "Overdue"-on-resolved chip fix (finding #8, decision D1)

**Files:** `app/services/excess_service.py` (`create_excess_list`, `update_excess_list`), `app/services/excess_mirror.py` (`publish_list`), `app/routers/resell.py` (`resell_create_list`, `_list_cards`/`_detail_context` context, a small `_close_at_display` helper), `app/schemas/excess.py` (`ExcessListUpdate`), `app/templates/htmx/partials/resell/create_modal.html`, `app/templates/htmx/partials/resell/_header_chips.html`, `app/templates/htmx/partials/resell/_lists.html`; Tests: `tests/test_resell_list_lifecycle.py`, `tests/test_resell_mirror.py` (update `test_publish_clears_stale_close_at`), new `tests/test_resell_posting_window.py`.

**Interfaces:**
- Produces: `create_excess_list(..., close_at: datetime | None = None)` stores a **future** tz-aware deadline on the draft (reject a naive or past datetime with `HTTPException(400)`). `resell_create_list` accepts an optional `close_at` Form field (HTML `datetime-local` → parse to tz-aware UTC). `publish_list` **preserves** a future `close_at` and only nulls a **stale/past** one (was: unconditional `close_at=None`). `update_excess_list` accepts `close_at` (draft-scope, same 400 validation, allows clearing). The card/detail context exposes `is_live` (status in open/collecting) and `close_at_display` (formatted date) so the chip renders the countdown ONLY while live and `closed {date}` / nothing once resolved.
- Consumes: existing `expire_overdue_lists` (unchanged — it already fires on `status in (open,collecting) AND close_at < now`; it now finally has live rows to act on).

- [ ] Failing tests first (`tests/test_resell_posting_window.py`): (a) create with a future `close_at` persists it; create with a past/naive `close_at` → 400; (b) publish preserves a future `close_at` (rewrite the mirror test that asserted it's cleared → assert future preserved, stale nulled); (c) the nightly `expire_overdue_lists` flips an open list whose real (create-set, publish-preserved) `close_at` is now past → expired + mirror retired (replace the hand-set `el.close_at` in `test_resell_list_lifecycle.py`); (d) chip: `is_live`/`close_at_display` context correct — a bid_out/closed/awarded list yields `is_live=False` and NO "Overdue". → red → implement → green → commit.
- [ ] Template: `create_modal.html` gains an optional `<input type="datetime-local" name="close_at">` labeled "Offers close by (optional)". `_header_chips.html` + `_lists.html` gate the `closes {{ time_text(...) }}` chip on `is_live`; for resolved lists render `closed {{ close_at_display }}` (muted, not red) or nothing. Verify rendered HTML headless (no red "Overdue" on a closed list).

---

### Task 2: Sighting-mirror line identity — migration 199 + line-keyed upsert/retire (finding #18)

**Files:** `alembic/versions/199_sighting_excess_line_fk.py` (NEW), `MIGRATION_NUMBERS_IN_FLIGHT.txt`, `app/models/sourcing.py` (`Sighting` — new column + index), `app/services/excess_mirror.py` (`_find_mirror`, `mirror_line`, `retire_line`); Tests: `tests/test_resell_mirror.py`.

**Interfaces:**
- Migration `199_sighting_excess_line_fk` (27 chars): `revision="199_sighting_excess_line_fk"`, `down_revision="198_sighting_req_id_nullable"`. `upgrade()`: `add_column('sightings', Column('excess_line_item_id', Integer, nullable=True))`; `create_foreign_key('fk_sightings_excess_line_item','sightings','excess_line_items',['excess_line_item_id'],['id'],ondelete='SET NULL')`; `create_index('ix_sightings_excess_line_item','sightings',['excess_line_item_id'])`; then a clean-sweep of legacy shadows: `op.execute("DELETE FROM sightings WHERE source_type='customer_excess'")` (they're disposable, rebuilt line-keyed on next publish/sync; the old key is ambiguous exactly for duplicate-part lines so match-and-backfill is rejected). `downgrade()`: drop index → drop FK → drop column. Use `op.batch_alter_table` guard only if the round-trip runs under SQLite; PG16 deploy target takes plain ops. Round-trip upgrade→downgrade→upgrade on a THROWAWAY PG (not staging).
- Model: `Sighting.excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="SET NULL"), nullable=True)` after `source_company_id`; add `Index("ix_sightings_excess_line_item", "excess_line_item_id")` to `__table_args__`. (Drift gate requires both model index + migration index.)
- `_find_mirror` gains an `excess_line_item_id` param; the DISAMBIGUATOR becomes `Sighting.excess_line_item_id == line.id` (keep `source_type='customer_excess'` scope). `mirror_line` sets `sighting.excess_line_item_id = line.id` and passes `line.id` to `_find_mirror`. `retire_line` looks up by `line.id` so retiring one duplicate-part line deletes ONLY its own Sighting.

- [ ] Failing tests first (`tests/test_resell_mirror.py`): within-list two lines same part/material_card, distinct qty/condition → **two** distinct Sightings, independent qty (was: one collapsed row); award/retire one twin → the other twin's Sighting **survives** (was: shared-row deletion). Keep the existing cross-list no-collapse test green. → red → model+migration+mirror edits → green → round-trip the migration on throwaway PG → commit.

---

### Task 3: `teardown_list_mirror` + wire into deletion paths (P2 list-delete-strands-mirror)

**Files:** `app/services/excess_mirror.py` (NEW `teardown_list_mirror`), `app/services/company_merge_service.py` (`delete_companies`), `app/management/seed_resell_demo.py` (`_reset`), `app/services/excess_service.py` (`delete_excess_list` — defence-in-depth); Tests: `tests/test_resell_mirror.py`, `tests/test_company_merge_service.py`.

**Interfaces:**
- Produces: `teardown_list_mirror(db, excess_list)` — look up the virtual Requisition/Requirement via `_virtual_req_name(excess_list)`; bulk-delete every `Sighting` with `source_type='customer_excess'` hanging on that virtual requirement's id (robust to `material_card_id`/`source_company_id` NULLs, unlike `retire_line`); then delete the virtual Requirement + Requisition so no orphan scratch req survives. Leaf→root delete order (SQLite FKs enforced in tests). Flush; caller commits. Must run BEFORE the `ExcessList`/company rows are deleted (needs `excess_list.id`). Scope strictly to the given list's virtual req (a sibling list for the same company owns a DISTINCT virtual req — never wipe by company).
- `delete_companies`: enumerate each company's `ExcessList`s and call `teardown_list_mirror(db, el)` BEFORE the existing generic `Sighting.source_company_id` NULL-detach + `ExcessList` bulk-purge, so mirror rows are DELETED (not left advertising live supply with a NULL company). Keep the generic NULL-detach for genuine non-mirror sightings.
- `seed_resell_demo._reset`: call `teardown_list_mirror(db, el)` per demo list before `db.delete(el)`.
- `delete_excess_list`: call `teardown_list_mirror` (no-op today — draft-only, never mirrored — but makes the guarantee explicit and survives any future loosening).
- **Do NOT** call teardown on close/expire (a closed list can be reopened via unaward → re-mirrors; its virtual req must survive). Teardown is strictly for list/company DELETION.

- [ ] Failing tests first: after `delete_companies` on a company whose list was published/mirrored → zero `customer_excess` Sightings for that company AND the virtual `Customer Excess (list %)` Requisition/Requirement gone (reuse `_customer_excess_sightings` helper); same for `seed_resell_demo --reset`; `delete_excess_list` teardown idempotent no-op. → red → implement → green → commit.

---

### Task 4: Nightly BuyerScore backstop job + drift test (finding #17 core)

**Files:** `app/jobs/resell_jobs.py` (NEW `_job_recompute_buyer_scores` + a 3rd `scheduler.add_job`), `app/services/buyer_affinity_service.py` (docstring on `recompute_buyer_score` denominator — see note); Tests: new `tests/test_resell_buyer_score_backstop.py` + extend `tests/test_buyer_affinity_service.py`.

**Interfaces:**
- Produces: `_job_recompute_buyer_scores` — async `_traced_job` wrapper mirroring `_job_expire_resell_lists` (SessionLocal + `try / except SQLAlchemyError (rollback) / except Exception (rollback) / finally close`), delegating to `buyer_affinity_service.recompute_all_buyer_scores(db)`; registered in `register_resell_jobs` as a 3rd `scheduler.add_job(CronTrigger(hour=2, minute=35), id="recompute_buyer_scores", name="Recompute resell BuyerScores")` (a distinct minute so the three nightly jobs don't collide).
- **Denominator decision (resolved in-plan, no user needed):** finding #17 asked to exclude `sent_at`-NULL rows from `response_rate`. Ground-truth: a manual-log phone/teams/marketplace touch is a *genuine* contact written `status=SENT, sent_at=None`; excluding it would wrongly drop real touches from the rate. **Resolution: keep counting genuine manual touches** (current behavior is correct); the finding's `sent_at`-NULL exclusion is superseded. Add a docstring note on `recompute_buyer_score` recording this + a test asserting a manual-log SENT touch DOES count while a FAILED/SENDING/INTERRUPTED row does NOT.

- [ ] Failing tests first (`tests/test_resell_buyer_score_backstop.py`): (a) RESELL-TEST-4 drift — after a prior compute, mutate offer/outreach history to stale a `BuyerScore` row, call `recompute_all_buyer_scores`, assert it reconciles to truth and returns the walked-card count; (b) nightly-job coverage mirroring the expiry-job test — success path plus the SQLAlchemyError and generic-Exception branches (patch the service to raise, assert rollback + no crash). Extend `test_buyer_affinity_service.py` with the manual-log-counts / FAILED-excluded denominator assertion. → red → implement job + docstring → green → commit.

---

### Task 5: UI-submit offer attribution → BuyerScore fires on manual offers (finding #17, UI half)

**Files:** `app/templates/htmx/partials/resell/offer_form.html` (new optional buyer `<select>`), `app/routers/resell.py` (`resell_submit_offer` — accept `buyer_company_id`; the view rendering the offer form must pass a `companies` list, reusing the create-modal context source), `app/services/excess_service.py` (`submit_offer` — accept + resolve the buyer), `app/services/resell_outreach_service.py` (reuse `counterparty_card`); Tests: `tests/test_resell_award.py` (new end-to-end submit→award→score case), `tests/test_resell_offers.py`.

**Interfaces:**
- Produces: `offer_form.html` gains an optional `<select name="buyer_company_id" class="input">` — mirrors the `create_modal.html` `company_id` select exactly (default `<option value="">Buyer (optional)…</option>` + the same company options), labeled "Buyer (optional)". The context that renders the offer form passes `companies` (reuse whatever `create_modal`'s render context uses — do not invent a new query if one exists).
- `resell_submit_offer` accepts `buyer_company_id: int | None = Form(None)` and threads it into `submit_offer`.
- `submit_offer(..., buyer_company_id: int | None = None)`: when set, resolve `offer.offerer_vendor_card_id = counterparty_card(db, company_id=buyer_company_id).id` (the existing canonicalizer that get-or-creates a VendorCard from a company); when unset, leave it `None` (no regression — award still works, just no score, exactly as today). This makes `recompute_buyer_score_on_win` (already wired at award) FIRE for manual offers attributed to a buyer.
- Anonymization: the buyer selector is on the OWNER-facing / submitter offer form only; it exposes the CRM company list (companies the trader already sees), not competitor-offer data — no `can_see_customer` leak. Confirm the offer form is not rendered to a non-owner competitor in a way that would leak the list.

- [ ] Failing tests first: (a) `excess_service.submit_offer(..., buyer_company_id=<company>)` sets `offerer_vendor_card_id` to the resolved card; award that offer → a `BuyerScore` row is written for that card (the gap the whole finding is about — previously NULL card → no score); (b) `submit_offer` with no `buyer_company_id` leaves `offerer_vendor_card_id=None` and award still succeeds (no regression); (c) the offer form renders the buyer `<select>` with the company options (headless assert). → red → implement → green → commit.

---

## Self-Review
- **Coverage:** #8 (T1), #18 (T2), P2 teardown (T3), #17 nightly backstop (T4), #17 UI-submit attribution (T5). ✓ — finding #17 now fully closed.
- **Migration:** exactly one (199, additive Sighting FK), id 27 chars ≤ 32, claimed in-flight, single head. ✓
- **UI:** two net-new inputs, both reusing the existing `create_modal` `<select>` pattern (no new convention). ✓
- **Anonymization/guards preserved:** no change to `can_see_customer`, owner/draft, or terminal-status guards from Phases 1-4; the buyer selector exposes only the CRM company list the trader already sees. ✓
- **Shared macro untouched:** chip gated at the resell-template level. ✓
