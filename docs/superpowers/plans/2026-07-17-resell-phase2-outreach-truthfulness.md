# Resell Rework — Phase 2: Outreach Send Truthfulness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make Resell outreach tell the truth about sends — distinct `FAILED`/`INTERRUPTED` states with persisted errors, retry with a double-send guard, a stale-`sending` sweeper, commit-after-send bookkeeping, and correct downstream counting — so traders never act on false "contacted, silent" data.

**Architecture:** Additive migration adds outreach status members + a persisted `send_error` column. The send state machine (`resell_outreach_service.py`) stops collapsing failures to `NO_RESPONSE`. Four downstream readers exclude non-sent rows via ONE shared status set. A new sweeper job + retry entry point close the durability gaps.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL 16, APScheduler jobs, pytest.

## Global Constraints
- Status values ALWAYS from `app/constants.py:ExcessOutreachStatus` — never raw strings.
- New members appended after `NO_RESPONSE` (`constants.py:257`): `FAILED = "failed"`, `INTERRUPTED = "interrupted"`. Rewrite the enum docstring (`:240-249`) so `no_response` = genuine buyer silence ONLY. The model validator (`models/excess.py:368-375`) auto-accepts new members — no validator edit.
- Migration claims the next free number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` — **194** (193 is Phase-1's remap). Chain `down_revision` onto Phase-1's `193` if merged, else current `alembic heads`; re-chain at merge, keep the number. Round-trip on a THROWAWAY PG 16.
- Introduce ONE shared constant `_NOT_SENT_STATUSES = {SENDING, FAILED, INTERRUPTED}` (home: `buyer_affinity_service.py` near `_RESPONDED_STATUSES:76-81`) and reuse it in all four downstream fixes — never re-inline the set.
- A "failed" row may have actually delivered: retry and the sweeper MUST re-run the sent-message lookup (`_find_sent_message`) before resending. Never assume not-sent.
- After changes, update `docs/APP_MAP_INTERACTIONS.md` (outreach lifecycle) + `APP_MAP_DATABASE.md` (new column/statuses).

---

### Task 1: New statuses + `send_error` column migration (194)

**Files:** Modify `app/constants.py:240-257`; `app/models/excess.py:339-351` (add `send_error` nullable Text + update status comment `:345`); Create `alembic/versions/194_outreach_failed_states.py`; Modify `MIGRATION_NUMBERS_IN_FLIGHT.txt`; Test `tests/test_resell_outreach_async.py`.

**Interfaces:** Produces `ExcessOutreachStatus.FAILED`/`INTERRUPTED`; `ExcessOutreach.send_error: str | None`.

- [ ] **Step 1:** Failing test — `ExcessOutreachStatus.FAILED == "failed"`; an `ExcessOutreach(status="interrupted")` validates; `send_error` round-trips a string.
- [ ] **Step 2:** Run — FAIL (AttributeError / no column).
- [ ] **Step 3:** Append the two enum members + rewrite docstring; add `send_error = Column(Text, nullable=True)` to the model; write the additive migration (`op.add_column`; downgrade drops it). Claim 194.
- [ ] **Step 4:** Run — PASS; round-trip upgrade→downgrade→upgrade on throwaway PG; `alembic heads` single.
- [ ] **Step 5:** Commit (migration + claim + model + constants + test).

---

### Task 2: Finalize truthfulness — FAILED + persisted error + total-exception + graph-id flag

**Files:** Modify `app/services/resell_outreach_service.py:459-498`; Test `tests/test_resell_outreach_async.py` (rework `:273`, `:315`, reconcile `:227`).

**Interfaces:** Consumes `FAILED`, `send_error`. Produces: per-buyer non-`sent` result → `status=FAILED`, `send_error=result.get("error")`, `sent_at=None`; total send exception (`:459-465`) → all pending rows `FAILED` + exception text (not `NO_RESPONSE`, not stuck `sending`); graph-id-missing (`:493-498`) → flag (reuse `send_error` or a boolean) so tracker can show "delivered, reply-matching degraded".

- [ ] **Step 1:** Failing tests — skip (`{"status":"skipped","error":"do-not-contact"}`) → `FAILED` + `send_error=="do-not-contact"` (rewrite of `test_skipped_recipient_flagged_no_response:273`); total exception → `FAILED` + error text (rewrite of `:315`); genuine per-buyer failure → `FAILED`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** At `:475-479` map non-`sent` → `FAILED` + persist `error`; at `:459-465` flag pending rows `FAILED` with the exception text instead of `send_results=[]`→`NO_RESPONSE`; at `:493-498` set the degraded flag.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit.

---

### Task 3: Commit-after-send + guarded bookkeeping + activity/cadence gating

**Files:** Modify `app/services/resell_outreach_service.py:501-552` (finalize call site + background commit boundary); Test same file.

**Interfaces:** Produces: the status/graph-id advance commits immediately after the send outcome; a later bookkeeping exception cannot roll back a delivered `SENT` (fix the blanket `except → rollback` at `:548-550`); `_log_outreach_activity` is gated at the `:501-508` call site so a FAILED send writes NO "Emailed" ActivityLog and does NOT bump cadence clocks. Do NOT gate inside `_log_outreach_activity` (manual path `:318` legitimately passes `sent=False`).

- [ ] **Step 1:** Failing tests — bookkeeping exception after send does not revert `SENT`/graph ids (regression for `:548-550`); FAILED send writes no ActivityLog and does not advance `last_outbound_at` (finding #6).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Commit the send outcome first; wrap post-send bookkeeping in its own guard; gate the activity/cadence call on `sent_ok` at `:501-508`.
- [ ] **Step 4:** Run — PASS. **Step 5:** Commit.

---

### Task 4: Retry + double-send guard + stale-`sending` sweeper

**Files:** Modify `app/services/resell_outreach_service.py` (new retry fn + reuse `SENDING`-only filter `:425-449`); `app/jobs/resell_jobs.py:20-27` (register sweeper); `app/routers/resell.py` (retry route near `:1611`); `app/templates/htmx/partials/resell/_outreach.html:85-107` (Retry button); Test `tests/test_resell_outreach_async.py` + a job test (model: `tests/test_nightly_resell_coverage.py`).

**Interfaces:** Produces: retry resets a `FAILED` row → `SENDING` and re-enqueues, but ONLY after `_find_sent_message` confirms it wasn't already delivered (double-send guard); sweeper flips `SENDING AND created_at < now-threshold` → `INTERRUPTED` (index `excess.py:380` exists), also doing the sent-lookup before any auto-retry.

- [ ] **Step 1:** Failing tests — retry of a FAILED row re-enqueues + does a sent-lookup first + does not double-send an already-delivered row; sweeper flips an aged SENDING row to INTERRUPTED without assuming not-sent.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement retry fn + route + button; implement + register the sweeper job. **Step 4:** Run — PASS. **Step 5:** Commit.

---

### Task 5: Downstream exclusions + tracker badge/Retry surface

**Files:** Modify `app/routers/resell.py:1344` (offered summary); `app/services/buyer_affinity_service.py:398,405,610-619` (denominator, offered-timestamp, nudge `already`); `app/templates/htmx/partials/resell/_outreach.html:84-95`; Test `tests/test_resell_outreach_async.py` + affinity test.

**Interfaces:** Consumes `_NOT_SENT_STATUSES`. Produces: offered-summary counts only genuinely-offered buyers; response_rate denominator excludes non-sent rows; `last_offered_at` drops the `created_at` fallback (`:405`); nudge `already` set excludes FAILED/INTERRUPTED so they're re-nudgeable; badge map gains red `failed` + amber `interrupted`; a non-sent row shows `—` for "When" (not `created_at`).

- [ ] **Step 1:** Failing tests — offered count, response_rate denominator, and nudge `already` each exclude a FAILED row (finding: buyer not penalized/stranded). **Step 2:** FAIL. **Step 3:** Apply the shared-set filter at all four sites + template badge/When. **Step 4:** PASS. **Step 5:** Commit.

---

### Task 6: Campaign idempotency guard

**Files:** Modify `app/services/resell_outreach_service.py:371-384` (or route `resell.py:1565-1586`); Test `tests/test_resell_outreach_async.py`.

**Interfaces:** Produces: a second identical submit does not create a second live row / second send — skip buyers that already have a live (`SENDING`/`SENT`) row for the same `(list_id, line_id)` within a window (mirror the `SENDING`-only filter `:425-449`).

- [ ] **Step 1:** Failing test — second identical submit creates no second live row / send. **Step 2:** FAIL. **Step 3:** Add the dedup guard. **Step 4:** PASS. **Step 5:** Commit.

---

### Task 7: Nightly-expiry per-list isolation + inbound collecting-flip

**Files:** Modify `app/services/excess_service.py:974-977` (per-list try/except or `begin_nested` SAVEPOINT); `app/services/resell_outreach_service.py:837-852` (inbound collecting-flip); Test `tests/test_nightly_resell_coverage.py` + outreach test.

**Interfaces:** Produces: one list whose mirror-sync raises does not block the others from expiring (finding #6 silent-failures); a reply-linked inbound offer flips `OPEN → COLLECTING` (matching `excess_service.py:559-561`).

- [ ] **Step 1:** Failing tests — a bad list in the batch does not stop the others expiring; an inbound reply on an OPEN list flips it to COLLECTING. **Step 2:** FAIL. **Step 3:** Wrap each list's flip+mirror+commit in isolation; add the collecting-flip after the offer links. **Step 4:** PASS. **Step 5:** Commit. Open PR; pr-review-fleet; live-verify.

---

## Self-Review
- **Coverage:** #5 (T2,T3,T5), #6 (T3,T7), #7 (T2,T3,T4), campaign idempotency (T6), graph-id (T2), nightly isolation + inbound flip (T7), migration/enum (T1). ✓
- **Shared set:** `_NOT_SENT_STATUSES` defined once (T1/T5), reused in all four readers. ✓
- **Delivered-safety:** retry + sweeper both re-run sent-lookup before resend. ✓
- **Migration:** 194, chains onto 193, round-tripped. ✓
