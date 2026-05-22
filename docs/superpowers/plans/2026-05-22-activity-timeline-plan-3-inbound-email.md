# Activity Timeline — Plan 3: Inbound-Email Bridge

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** When an inbound vendor email reply lands (a `VendorResponse` row is created in `poll_inbox`), write a matching `email_received` `activity_log` row so the email appears on the requisition Activity tab.

**Architecture:** `poll_inbox()` in `app/email_service.py` is the single `VendorResponse` creation site. It already resolves `matched_req_id` (a 4-tier requisition matcher) and has the inbound message's address/subject/message-id in scope. Route inbound email through the existing `log_email_activity()` writer (`direction="received"`) — it does vendor contact-matching, sets `direction="inbound"`/`event_type="email"`, and dedups on `external_id`. Inbox scans can run with no user (`scanned_by_user_id` may be `None`), so `log_email_activity`'s `user_id` parameter is widened to `int | None` (the `activity_log.user_id` column is already nullable). No schema migration.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest, Loguru.

**Spec:** `docs/superpowers/specs/2026-05-20-activity-timeline-design.md` (build step 3).

**Branch:** Create `feat/activity-timeline-3` off `feat/activity-timeline-2b` (or off `main` if Plans 1/2a/2b have merged).

---

### Task 1: Widen `log_email_activity` `user_id` to optional

`log_email_activity()` (`app/services/activity_service.py`, def ~line 143) types `user_id: int` (required, non-optional). Inbox scans triggered by a scheduled job have no user. The `ActivityLog.user_id` column is already nullable. Widen the parameter so a userless inbound-email log is valid.

**Files:**
- Modify: `app/services/activity_service.py`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py` (reuse its existing imports/helpers):

```python
def test_log_email_activity_accepts_none_user(db_session, test_requisition):
    """log_email_activity tolerates user_id=None (userless inbox scan)."""
    record = log_email_activity(
        user_id=None,
        direction="received",
        email_addr="vendor@example.com",
        subject="RE: RFQ",
        external_id="msg-none-user-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.user_id is None
    assert record.requisition_id == test_requisition.id
```

Verify `log_email_activity` is imported in that test file (Plan 1 added tests calling it — reuse the import).

- [ ] **Step 2: Run the test, confirm it fails**

`TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k none_user -v --override-ini="addopts="`
Expected: it may PASS at runtime (Python does not enforce annotations) — in that case the "failure" is the type contract: confirm `mypy app/services/activity_service.py` is clean BEFORE the change, then after Step 3 confirm the annotation is corrected. If the test genuinely fails (e.g. a runtime guard rejects `None`), confirm that reason. Treat the type-annotation correction as the deliverable; the test locks the runtime behavior.

- [ ] **Step 3: Widen the annotation**

In `app/services/activity_service.py`, in `log_email_activity`'s signature, change `user_id: int` to `user_id: int | None`. (Do NOT change `log_call_activity` — out of scope.) Confirm nothing in the body assumes `user_id` is non-`None` (it is only assigned to `ActivityLog(user_id=...)`, which is nullable — fine).

- [ ] **Step 4: Run tests + mypy**

`TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -v --override-ini="addopts="`
`mypy app/services/activity_service.py`
Expected: tests pass; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_service.py tests/test_activity_write_path.py
git commit -m "feat: allow userless inbound-email activity logging (user_id optional)"
```

---

### Task 2: Log `email_received` when a VendorResponse is created

In `poll_inbox()` (`app/email_service.py`), after the `VendorResponse` is added and flushed, call `log_email_activity(direction="received", ...)` so the inbound email reaches the requisition Activity tab.

**Files:**
- Modify: `app/email_service.py`
- Modify: `docs/APP_MAP_INTERACTIONS.md`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
def test_poll_inbox_logs_email_received(db_session, test_requisition, monkeypatch):
    """An inbound vendor reply matched to a requisition writes an email_received row."""
    import app.email_service as es
    from app.models import ActivityLog

    fake_message = {
        "id": "graph-msg-inbound-001",
        "subject": f"RE: RFQ [AVAIL-{test_requisition.id}]",
        "from": {"emailAddress": {"address": "vendor@example.com", "name": "Vendor Rep"}},
        "body": {"content": "We can supply 500 units."},
        "bodyPreview": "We can supply 500 units.",
        "receivedDateTime": "2026-05-22T10:00:00Z",
        "conversationId": "conv-inbound-001",
    }
    # Drive poll_inbox with one fake inbox message. VERIFY the real mechanism poll_inbox
    # uses to fetch messages and monkeypatch THAT (a graph-client call). See Step-1 note.

    ...  # call poll_inbox, then:
    rows = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == test_requisition.id,
            ActivityLog.activity_type == "email_received",
        )
        .all()
    )
    assert len(rows) == 1
```

**Step-1 note (REQUIRED before finalizing the test):** Read `poll_inbox()` in `app/email_service.py` — determine exactly how it fetches inbox messages (a Graph API call / a helper) and what `token`/`db`/`requisition_id`/`scanned_by_user_id` arguments it needs. `monkeypatch` the message-fetch so `poll_inbox` processes the single `fake_message` above with no network call (mirror how `tests/test_email_service.py` already tests `poll_inbox` — read that file for the established pattern). The `[AVAIL-{id}]` subject token makes the 4-tier matcher resolve `matched_req_id` to `test_requisition.id` (Tier 2). If matching via the token proves unreliable in the test, pass `requisition_id=test_requisition.id` directly to `poll_inbox`. The test MUST drive the real `poll_inbox` and assert the real `ActivityLog` row. If `poll_inbox` cannot be exercised in a unit test, STOP and report NEEDS_CONTEXT.

- [ ] **Step 2: Run the test, confirm it fails**

`TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k poll_inbox -v --override-ini="addopts="`
Expected: FAIL — no `email_received` row.

- [ ] **Step 3: Instrument `poll_inbox`**

In `app/email_service.py`, locate the `VendorResponse(...)` construction in `poll_inbox` (inside the per-message `try` / `db.begin_nested()` block) and its `db.flush()`. Confirm `log_email_activity` is imported in this file (it is defined in `app/services/activity_service.py`; add `from .services.activity_service import log_email_activity` if not already imported — match the file's import style).

Immediately after the `VendorResponse` `db.flush()`, still inside the per-message `try` block (so it shares the savepoint), insert:

```python
            log_email_activity(
                user_id=scanned_by_user_id,
                direction="received",
                email_addr=email_addr,
                subject=subj,
                external_id=msg_id,
                contact_name=sender.get("name"),
                db=db,
                requisition_id=matched_req_id,
            )
```

Use the real local variable names from `poll_inbox` (the dossier indicates `scanned_by_user_id`, `email_addr`, `subj`, `msg_id`, `sender`, `matched_req_id`, `db` — verify each against the live function). `log_email_activity` dedups on `external_id` (`msg_id`), so a re-poll of the same message will not double-log. `log_email_activity` flushes, does not commit — `poll_inbox`'s existing `nested.commit()` / batch `db.commit()` persist it.

Do NOT guard on `matched_req_id` being set — an unmatched inbound email (`requisition_id=None`) is still a valid activity row (it just won't appear on a req tab); `log_email_activity` handles `requisition_id=None`.

- [ ] **Step 4: Run tests, confirm they pass**

`TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py tests/test_email_service.py -v --override-ini="addopts="`
Expected: PASS — new test passes; existing `poll_inbox`/email-service tests still pass. (Pre-existing xdist pollution under broad runs — verify against a `git stash` baseline; report but proceed.)

- [ ] **Step 5: Update the APP_MAP doc**

In `docs/APP_MAP_INTERACTIONS.md`, find the inbound-email flow / activity-logging note and add: inbound vendor email replies (`VendorResponse` created in `poll_inbox`) now also write an `email_received` `activity_log` row via `log_email_activity()`, so they appear on the requisition Activity tab. Match the doc's style.

- [ ] **Step 6: Lint and commit**

```bash
ruff check app/email_service.py
git add app/email_service.py docs/APP_MAP_INTERACTIONS.md tests/test_activity_write_path.py
git commit -m "feat: log email_received activity when inbound vendor reply is recorded"
```

---

## Self-Review

**Spec coverage (build step 3 — "bridge inbound email"):**
- `VendorResponse` create → `email_received` activity row → Task 2 ✓
- Userless inbox scans supported → Task 1 ✓
- Dedup (no double-log on re-poll) → `log_email_activity`'s `external_id` dedup, `external_id=msg_id` ✓

**Design note:** the spec's component diagram showed `log_activity(event_type=email_received)`; this plan instead uses the existing `log_email_activity(direction="received")` because it already does vendor contact-matching, sets `direction`/`event_type`, and dedups on `external_id` — Plan 1 added the `requisition_id` parameter to `log_email_activity` for exactly this purpose. Routing inbound email through `log_email_activity` (not the bare `log_activity`) is consistent with `log_activity`'s own docstring ("Email and call events are written by log_email_activity()").

**No migration:** confirmed — `activity_log.user_id` is already nullable; only a Python type annotation is widened.

**Scope:** Plan 3 is the inbound-email bridge only. AI curation of `email_received` rows (quality scoring) is Plan 4.
