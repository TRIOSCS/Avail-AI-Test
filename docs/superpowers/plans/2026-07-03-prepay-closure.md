# Prepay Closure (Payment Lifecycle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the prepayment loop — a prepayment moves `requested → approved → paid` (or `void`), accounting confirms the wire via a tokenized link in the approval email, and the paid event notifies the buyer, salesperson, and all managers.

**Architecture:** Add an explicit `status` lifecycle + paid/approved/void fields to `Prepayment` (migration 179). Stamp `approved` and mint a single-use `pay_token` at approval; a public token route (no login, CSRF-exempt, rate-limited) lets non-Avail accounting mark it `paid`; a manager in-app fallback + undo covers mistakes. Extend the teardown sweep to `void` approved-but-unwired prepayments with a "do not wire" stand-down. Two new notifications (`_paid` in-app fan-out, `_voided` stand-down) reuse the existing module.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (sync), PostgreSQL 16, Alembic, HTMX + Alpine + Jinja2, `secrets` token, Microsoft Graph (delegated) notifications, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-03-prepay-closure-design.md` (authoritative).
- Status values from a new `PrepaymentStatus` StrEnum in `app/constants.py`; never raw strings. `db.get(Model, id)`.
- Money is `Decimal` (`paid_amount`, `total_incl_fees` are `Numeric(12,2)`); never float.
- Migration 179 via Alembic; migration + code deploy same batch; revision id ≤ 32 chars; round-trip on a THROWAWAY Postgres 16 (never staging); verify single `alembic heads`.
- The token route is PUBLIC (no auth) — the token IS the authorization: add its path to `CSRF_EXEMPT_URLS` (app/main.py) and rate-limit it. Token = `secrets.token_urlsafe(32)`, single-use (cleared on paid/void), idempotent route.
- Notifications are best-effort/fire-and-forget (existing `prepayment_notifications` pattern) — never block a transition or the DB commit. In-app alerts use `ActivityLog(channel="system", activity_type=ActivityType.NOTE)` (the pattern already in `prepayment_notifications.py:405`).
- Confirm-link base URL = `settings.app_url` (app/config.py).
- Run tests `TESTING=1 PYTHONPATH=/root/availai`; full suite with `SENTRY_DSN=""`. `pre-commit run --files <changed>` after each task (twice if docformatter mutates). Update `docs/APP_MAP_*` after code changes. Do NOT deploy until the whole plan is green + go/no-go.
- Leave the approver / separation-of-duties model UNCHANGED.

---

### Task 1: `PrepaymentStatus` + lifecycle columns (migration 179)

**Files:**
- Modify: `app/constants.py` (new `PrepaymentStatus` StrEnum)
- Modify: `app/models/quality_plan.py` (`Prepayment`)
- Create: `alembic/versions/179_prepayment_lifecycle.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`
- Test: `tests/test_prepayment_lifecycle_model.py`

**Interfaces:**
- Produces: `PrepaymentStatus` (`REQUESTED="requested"`, `APPROVED="approved"`, `PAID="paid"`, `VOID="void"`); `Prepayment.status` (default `requested`), `.approved_by_id/.approved_at`, `.pay_token`, `.paid_at/.paid_by_id/.paid_by_label/.paid_via/.wire_reference/.paid_amount`, `.voided_at/.voided_by_id/.void_reason`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prepayment_lifecycle_model.py
"""Prepayment gains a status lifecycle + approved/paid/void columns (migration 179)."""
from app.constants import PrepaymentStatus
from app.models.quality_plan import Prepayment

def test_prepayment_status_enum():
    assert PrepaymentStatus.REQUESTED.value == "requested"
    assert {s.value for s in PrepaymentStatus} == {"requested", "approved", "paid", "void"}

def test_prepayment_lifecycle_columns_exist():
    cols = Prepayment.__table__.columns
    for name in ("status", "approved_by_id", "approved_at", "pay_token",
                 "paid_at", "paid_by_id", "paid_by_label", "paid_via",
                 "wire_reference", "paid_amount",
                 "voided_at", "voided_by_id", "void_reason"):
        assert name in cols, name
    assert cols["status"].default.arg == PrepaymentStatus.REQUESTED.value
```

- [ ] **Step 2: Run → FAIL** — `pytest tests/test_prepayment_lifecycle_model.py -v --override-ini="addopts="`.

- [ ] **Step 3: Add the enum + columns**

In `app/constants.py` near the other StrEnums:

```python
class PrepaymentStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PAID = "paid"
    VOID = "void"
```

In `app/models/quality_plan.py` `Prepayment`, after `buyer_remarks`:

```python
    status = Column(String(20), nullable=False, default=PrepaymentStatus.REQUESTED.value)
    approved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(UTCDateTime, nullable=True)
    pay_token = Column(String(64), nullable=True, unique=True)
    paid_at = Column(UTCDateTime, nullable=True)
    paid_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    paid_by_label = Column(String(120), nullable=True)
    paid_via = Column(String(20), nullable=True)  # accounting_email | in_app
    wire_reference = Column(String(120), nullable=True)
    paid_amount = Column(Numeric(12, 2), nullable=True)
    voided_at = Column(UTCDateTime, nullable=True)
    voided_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    void_reason = Column(String(255), nullable=True)
```

Import `PrepaymentStatus` at the top of the model module. Add `Index("ix_prepayment_status", "status")` to `__table_args__`. (String/ForeignKey/Numeric/Index already imported.)

- [ ] **Step 4: Write migration 179**

Read `alembic/versions/178_prepayment_line_link.py` for the revision-id/style. Create `alembic/versions/179_prepayment_lifecycle.py`, `down_revision = "178_prepayment_line_link"`. `upgrade()` adds all 13 columns (nullable; `status` with `server_default="requested"` then keep the app-level default), the `ix_prepayment_status` index, and the unique `pay_token` (add the column then `op.create_index("ix_prepayment_pay_token", "prepayments", ["pay_token"], unique=True)`). Then **backfill status** from each prepayment's PREPAYMENT `ApprovalRequest` via `op.get_bind()` + `text()`:

```python
    conn = op.get_bind()
    # approved
    conn.execute(sa.text(
        "UPDATE prepayments p SET status='approved', approved_at=ar.resolved_at, approved_by_id=ar.resolved_by_id "
        "FROM approval_requests ar WHERE ar.subject_type='prepayment' AND ar.subject_id=p.id AND ar.status='approved'"))
    # rejected -> void
    conn.execute(sa.text(
        "UPDATE prepayments p SET status='void', voided_at=ar.resolved_at, void_reason='rejected by approver' "
        "FROM approval_requests ar WHERE ar.subject_type='prepayment' AND ar.subject_id=p.id AND ar.status='rejected'"))
```
(Confirm the `approval_requests` resolver column name — likely `resolved_by_id`; adjust to the real column. Everything else stays `requested`.) `downgrade()` drops the two indexes then the 13 columns.

Append the 179 claim line to `MIGRATION_NUMBERS_IN_FLIGHT.txt`.

- [ ] **Step 5: Round-trip + single head**

```bash
docker run -d --name m179 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=t -p 55466:5432 postgres:16
until docker exec m179 pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
DATABASE_URL=postgresql://postgres:test@localhost:55466/t TESTING=1 PYTHONPATH=/root/availai alembic upgrade head
DATABASE_URL=postgresql://postgres:test@localhost:55466/t TESTING=1 PYTHONPATH=/root/availai alembic downgrade -1
DATABASE_URL=postgresql://postgres:test@localhost:55466/t TESTING=1 PYTHONPATH=/root/availai alembic upgrade head
TESTING=1 PYTHONPATH=/root/availai alembic heads   # single head 179
docker rm -f m179
```

- [ ] **Step 6: Run test (pass) + commit**

```bash
git add app/constants.py app/models/quality_plan.py alembic/versions/179_prepayment_lifecycle.py MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_prepayment_lifecycle_model.py
git commit -m "feat(prepayment): status lifecycle + approved/paid/void columns (migration 179)"
```

---

### Task 2: Approve stamps + mints token; reject → void + stand-down

**Files:**
- Modify: `app/routers/htmx/buy_plans.py` (`prepay_request_decide`)
- Test: `tests/test_prepayment_lifecycle_transitions.py`

**Interfaces:**
- Consumes: `PrepaymentStatus`, `Prepayment.status/approved_*/pay_token/void_*` (Task 1); `notify_prepayment_voided` (Task 4 — leave a guarded seam if built before Task 4).
- Produces: on approve, `prepayment.status=approved`, `approved_by_id`, `approved_at`, `pay_token` set; on reject, `status=void`, `void_reason`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_prepayment_lifecycle_transitions.py
"""Approving a prepayment stamps + mints its pay_token; rejecting voids it."""
from app.constants import PrepaymentStatus
# reuse the prepay fixtures from tests/test_prepayment_service_line.py + an authed manager client

def test_approve_stamps_and_mints_token(db_session, approved_manager_client, pending_prepay):
    pp, req = pending_prepay
    approved_manager_client.post(f"/v2/partials/approvals/prepay-requests/{req.id}/decide",
                                 data={"action": "approve"}, headers={"HX-Request": "true"})
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.APPROVED.value
    assert pp.approved_by_id is not None and pp.approved_at is not None
    assert pp.pay_token and len(pp.pay_token) >= 32

def test_reject_voids(db_session, approved_manager_client, pending_prepay):
    pp, req = pending_prepay
    approved_manager_client.post(f"/v2/partials/approvals/prepay-requests/{req.id}/decide",
                                 data={"action": "reject", "comment": "no"}, headers={"HX-Request": "true"})
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.VOID.value
    assert pp.void_reason
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement in `prepay_request_decide`** (read it — it already loads the `ApprovalRequest` `ar` and has the `gate_type==PREPAYMENT` guard from the QA fix, and resolves the Prepayment for the approve-notify). In the APPROVE branch, after the engine approve succeeds, load the Prepayment and:

```python
    import secrets
    from ...constants import PrepaymentStatus
    pp.status = PrepaymentStatus.APPROVED.value
    pp.approved_by_id = user.id
    pp.approved_at = datetime.now(timezone.utc)
    pp.pay_token = secrets.token_urlsafe(32)
    db.commit()
```
In the REJECT branch, load the Prepayment and set `pp.status = PrepaymentStatus.VOID.value`, `pp.void_reason = "rejected by approver"`, `db.commit()`, then fire `run_prepayment_notify_bg(notify_prepayment_voided, pp.id)` (Task 4). Keep the existing `notify_prepayment_approved` dispatch on approve.

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/routers/htmx/buy_plans.py tests/test_prepayment_lifecycle_transitions.py
git commit -m "feat(prepayment): approve stamps + mints pay_token; reject voids"
```

---

### Task 3: `mark_prepayment_paid` service transition

**Files:**
- Modify: `app/services/prepayment_service.py` (add `mark_prepayment_paid`)
- Test: `tests/test_prepayment_mark_paid.py`

**Interfaces:**
- Consumes: Task 1 columns; `notify_prepayment_paid` (Task 4).
- Produces: `mark_prepayment_paid(db, prepayment, *, wire_reference, paid_amount, paid_via, paid_by_id=None, paid_by_label=None) -> Prepayment`. Raises `ValueError` unless `status == approved`. Sets `paid` + fields, clears `pay_token`, fires the paid fan-out.

- [ ] **Step 1: Failing tests** — marking an approved prepayment sets `paid` + fields + clears token; marking a non-approved raises `ValueError`.

```python
# tests/test_prepayment_mark_paid.py
import pytest
from decimal import Decimal
from app.constants import PrepaymentStatus
from app.services.prepayment_service import mark_prepayment_paid

def test_mark_paid_sets_fields_and_clears_token(db_session, approved_prepay):
    pp = approved_prepay  # status=approved, pay_token set
    mark_prepayment_paid(db_session, pp, wire_reference="WIRE-1",
                         paid_amount=Decimal("20002.38"), paid_via="in_app",
                         paid_by_id=pp.created_by_id, paid_by_label="MK")
    assert pp.status == PrepaymentStatus.PAID.value
    assert pp.wire_reference == "WIRE-1" and pp.paid_at and pp.pay_token is None

def test_mark_paid_requires_approved(db_session, requested_prepay):
    with pytest.raises(ValueError):
        mark_prepayment_paid(db_session, requested_prepay, wire_reference="x",
                             paid_amount=Decimal("1"), paid_via="in_app")
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
def mark_prepayment_paid(db, prepayment, *, wire_reference, paid_amount, paid_via,
                         paid_by_id=None, paid_by_label=None):
    from ..constants import PrepaymentStatus
    from .prepayment_notifications import run_prepayment_notify_bg, notify_prepayment_paid
    if prepayment.status != PrepaymentStatus.APPROVED.value:
        raise ValueError("Only an approved prepayment can be marked paid.")
    prepayment.status = PrepaymentStatus.PAID.value
    prepayment.paid_at = datetime.now(timezone.utc)
    prepayment.wire_reference = wire_reference
    prepayment.paid_amount = paid_amount
    prepayment.paid_via = paid_via
    prepayment.paid_by_id = paid_by_id
    prepayment.paid_by_label = paid_by_label
    prepayment.pay_token = None
    db.commit()
    run_prepayment_notify_bg(notify_prepayment_paid, prepayment.id)
    return prepayment
```

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/services/prepayment_service.py tests/test_prepayment_mark_paid.py
git commit -m "feat(prepayment): mark_prepayment_paid transition (guarded, clears token, fans out)"
```

---

### Task 4: `notify_prepayment_paid` (fan-out) + `notify_prepayment_voided` (stand-down)

**Files:**
- Modify: `app/services/prepayment_notifications.py`
- Test: `tests/test_prepayment_notifications.py` (extend)

**Interfaces:**
- Produces: `async notify_prepayment_paid(prepayment_id, db=None)`, `async notify_prepayment_voided(prepayment_id, db=None, reason=None)`.

- [ ] **Step 1: Failing tests** — `_paid` writes in-app `ActivityLog(channel="system")` rows to the buyer (`created_by_id`), the salesperson (`buy_plan.submitted_by_id`), and every `role=='manager'` user (deduped); `_voided` emails the accounting/AP group addresses + posts the Teams card with a "DO NOT WIRE" heading.

```python
@pytest.mark.asyncio
async def test_paid_alerts_buyer_salesperson_managers(db_session, paid_prepay, users):
    await pn.notify_prepayment_paid(paid_prepay.id, db=db_session)
    alerts = db_session.query(ActivityLog).filter_by(channel="system").all()
    recips = {a.user_id for a in alerts}
    assert paid_prepay.created_by_id in recips
    assert paid_prepay.buy_plan.submitted_by_id in recips
    assert all(m.id in recips for m in users["managers"])

@pytest.mark.asyncio
async def test_voided_emails_stand_down(db_session, approved_prepay, set_group_config):
    with patch.object(pn, "_send_group_email", new=AsyncMock()) as email, \
         patch("app.services.prepayment_notifications.post_teams_channel_card", new=AsyncMock()):
        await pn.notify_prepayment_voided(approved_prepay.id, db=db_session, reason="plan cancelled")
    body = email.call_args.kwargs.get("html") or email.call_args.args[-1]
    assert "DO NOT WIRE" in body
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** both, mirroring the module's existing `notify_prepayment_approved`/`_send_group_email`/`_card` and the `channel="system"` alert writer at `:400-410`. `_paid` collects recipients: `pp.created_by_id`, `pp.buy_plan.submitted_by_id` (fallback `pp.buy_plan.requisition.created_by`), and `db.query(User).filter(User.role==UserRole.MANAGER.value, User.is_active).all()`; dedupe by id; write one `ActivityLog(user_id=r, activity_type=ActivityType.NOTE, channel="system", buy_plan_id=pp.buy_plan_id, subject="Prepayment paid", notes=f"{beneficiary} {currency} {amount} wired for PO {po} (plan #{id})")` each; commit. `_voided` reuses `_send_group_email` + `post_teams_channel_card` with a `_card(pp, "voided")`/`_email_html(pp, "voided")` whose heading is "DO NOT WIRE — prepayment voided: {reason}". Extend `_card`/`_email_html` to accept the `voided`/`paid` events.

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/services/prepayment_notifications.py tests/test_prepayment_notifications.py
git commit -m "feat(prepayment): paid fan-out + voided stand-down notifications"
```

---

### Task 5: Public tokenized confirm-paid route + email link

**Files:**
- Create: `app/routers/prepayment_confirm.py`
- Create: `app/templates/htmx/partials/prepayments/confirm_page.html` (public, minimal, own `<html>` shell — NOT the app base)
- Modify: `app/main.py` (register router + add the path to `CSRF_EXEMPT_URLS`)
- Modify: `app/services/prepayment_notifications.py` (`notify_prepayment_approved` email/card includes the confirm URL)
- Test: `tests/test_prepayment_confirm_route.py`

**Interfaces:**
- Consumes: `Prepayment.pay_token`/`status` (Task 1), `mark_prepayment_paid` (Task 3), `settings.app_url`.
- Produces: `GET /p/confirm/{token}` (page), `POST /p/confirm/{token}` (marks paid).

- [ ] **Step 1: Failing tests**

```python
# tests/test_prepayment_confirm_route.py
"""Public tokenized confirm-paid route: no login, idempotent, void-safe."""
from app.constants import PrepaymentStatus

def test_confirm_marks_paid_no_login(client, approved_prepay, db_session):
    token = approved_prepay.pay_token
    r = client.post(f"/p/confirm/{token}", data={"wire_reference": "W1", "confirmer": "Katy"})
    assert r.status_code == 200
    db_session.refresh(approved_prepay)
    assert approved_prepay.status == PrepaymentStatus.PAID.value
    assert approved_prepay.paid_via == "accounting_email" and approved_prepay.pay_token is None

def test_confirm_unknown_token_404(client):
    assert client.get("/p/confirm/nope").status_code == 404

def test_confirm_voided_token_shows_do_not_wire(client, voided_prepay_with_token):
    r = client.get(f"/p/confirm/{voided_prepay_with_token.pay_token}")
    assert "voided" in r.text.lower() or "do not wire" in r.text.lower()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement the router** (`app/routers/prepayment_confirm.py`, header comment). `router = APIRouter(tags=["prepayment-confirm"])`. `GET /p/confirm/{token}` and `POST /p/confirm/{token}`: look up `db.query(Prepayment).filter_by(pay_token=token).one_or_none()`; `None` → `HTMLResponse(status_code=404, ...)` render of confirm_page in a "not found" mode. If `status != approved` (already paid/void) → render the page in the matching read-only mode (paid → "already marked paid {date}"; void → "voided ({reason}) — do not wire"). GET (approved) → the confirm form (summary + `wire_reference` + `confirmer` + submit). POST (approved) → `mark_prepayment_paid(db, pp, wire_reference=form.wire_reference, paid_amount=pp.total_incl_fees, paid_via="accounting_email", paid_by_label=form.confirmer or "Accounting")` → render the "recorded — thank you" page. Apply `@limiter.limit("10/minute")` to both. The template is a standalone public HTML page (no app nav/JS).

Register in `app/main.py` (`app.include_router(...)`) and add `re.compile(r"/p/confirm/")` to `CSRF_EXEMPT_URLS` (the POST has no session/csrf; the token is the auth).

- [ ] **Step 4: Confirm URL in the approval email.** In `prepayment_notifications.notify_prepayment_approved` (and its `_card`/`_email_html`), when the event is `approved` and `pp.pay_token` is set, include `f"{settings.app_url}/p/confirm/{pp.pay_token}"` as a "Confirm wire sent" button/link. Add a test asserting the approved email body contains `/p/confirm/`.

- [ ] **Step 5: Run → PASS. Commit.**

```bash
git add app/routers/prepayment_confirm.py app/templates/htmx/partials/prepayments/confirm_page.html app/main.py app/services/prepayment_notifications.py tests/test_prepayment_confirm_route.py
git commit -m "feat(prepayment): public tokenized confirm-paid route + email link"
```

---

### Task 6: In-app mark-paid fallback + manager undo

**Files:**
- Modify: `app/routers/prepayments.py` (2 HTMX routes)
- Create: `app/templates/htmx/partials/prepayments/mark_paid_modal.html`
- Modify: `app/templates/htmx/partials/approvals/_tab_prepayment.html` (Mark-paid button on `approved` rows; Undo on `paid` rows)
- Test: `tests/test_prepayment_mark_paid.py` (extend)

**Interfaces:**
- Consumes: `mark_prepayment_paid` (Task 3), `PrepaymentStatus`.
- Produces: `POST /v2/partials/prepayments/{id}/mark-paid` (HTMX; manager/admin or plan owner), `POST /v2/partials/prepayments/{id}/unmark-paid` (manager/admin).

- [ ] **Step 1: Failing tests** — an authed manager posts mark-paid on an `approved` prepayment → `paid`, `paid_via="in_app"`, `paid_by_id` set; a restricted role is 403/404; unmark-paid on a `paid` reverts to `approved`, clears paid fields, re-mints `pay_token`, and is manager-only.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** `mark-paid`: gate to `require_user` + (manager/admin role OR plan owner via `get_buyplan_for_user`); read `wire_reference`/`paid_amount`(default `total_incl_fees`) form fields; call `mark_prepayment_paid(..., paid_via="in_app", paid_by_id=user.id, paid_by_label=user.name)`; return the re-rendered tab body + success toast; `ValueError` → 400 toast. `unmark-paid`: manager/admin only; guard `status==paid`; set `status=approved`, clear paid fields, `pay_token=secrets.token_urlsafe(32)`, write an ActivityLog, commit, re-render. Add the modal + the two buttons (Mark paid on `approved` rows, Undo on `paid` rows) to `_tab_prepayment.html`.

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/routers/prepayments.py app/templates/htmx/partials/prepayments/mark_paid_modal.html app/templates/htmx/partials/approvals/_tab_prepayment.html tests/test_prepayment_mark_paid.py
git commit -m "feat(prepayment): in-app mark-paid fallback + manager undo"
```

---

### Task 7: Void-on-teardown of an APPROVED prepayment

**Files:**
- Modify: `app/services/buyplan_workflow.py` (`_cancel_open_prepayment_requests_for_plan`)
- Test: `tests/test_prepayment_dangling_cancel.py` (extend)

**Interfaces:**
- Consumes: Task 1 status; `notify_prepayment_voided` (Task 4).

- [ ] **Step 1: Failing test** — a plan with an `approved` prepayment, on cancel/halt/complete/resource, flips it to `void` (+ `voided_at/void_reason`, `pay_token` cleared) and fires `notify_prepayment_voided`; a `paid` prepayment is left untouched; the existing REQUESTED behavior still holds.

```python
def test_teardown_voids_approved_prepayment(db_session, approved_prepay_on_plan):
    from app.services.buyplan_workflow import cancel_buy_plan
    pp = approved_prepay_on_plan
    with patch("app.services.buyplan_workflow.run_prepayment_notify_bg") as bg:
        cancel_buy_plan(pp.buy_plan_id, <user>, "done", db_session)
    db_session.refresh(pp)
    assert pp.status == "void" and pp.pay_token is None
    assert any("notify_prepayment_voided" == c.args[0].__name__ for c in bg.call_args_list)
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** In `_cancel_open_prepayment_requests_for_plan` (which already cancels REQUESTED requests + accepts the optional `line_ids` from the QA fix), ALSO select the plan's (or `line_ids`') `Prepayment`s with `status == PrepaymentStatus.APPROVED.value`, set `status=void`, `voided_at=now`, `void_reason=reason`, `pay_token=None`, and dispatch `run_prepayment_notify_bg(notify_prepayment_voided, pp.id)` for each. Leave `paid` untouched. Keep the REQUESTED-request cancellation as-is.

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/services/buyplan_workflow.py tests/test_prepayment_dangling_cancel.py
git commit -m "fix(prepayment): void an approved-but-unwired prepayment on plan teardown + stand-down"
```

---

### Task 8: Paid/Void badges

**Files:**
- Modify: `app/services/prepayment_service.py` (`prepayment_state_for_lines`)
- Modify: `app/templates/htmx/partials/buy_plans/_macros.html` (badge macro), `_tab_prepayment.html`
- Test: `tests/test_approvals_hub_tabs.py` (extend)

- [ ] **Step 1: Failing test** — `prepayment_state_for_lines` returns `paid`/`void` for those lines; the tab/PO badge renders "Paid" (emerald) with the wire reference on a paid row and "Void" with the reason on a void row.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3:** Extend `prepayment_state_for_lines` to read `Prepayment.status` (map approved→'approved', paid→'paid', void→(omit, so a new request is allowed again) — decide: a `void` line SHOULD allow a fresh request, so treat void as "no active prepayment"; paid blocks + shows Paid). Extend the badge macro with `paid`/`void` variants; render wire reference/paid-by on paid rows and reason on void rows in `_tab_prepayment.html`.
- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/services/prepayment_service.py app/templates/htmx/partials/buy_plans/_macros.html app/templates/htmx/partials/approvals/_tab_prepayment.html tests/test_approvals_hub_tabs.py
git commit -m "feat(prepayment): Paid/Void lifecycle badges"
```

---

### Task 9: Docs, full suite, deploy

- [ ] **Step 1:** Update `docs/APP_MAP_DATABASE.md` (prepayments lifecycle columns) + `docs/APP_MAP_INTERACTIONS.md` (the closure flow: approve→token→confirm-paid→fan-out; void-on-teardown; the public route).
- [ ] **Step 2:** `pre-commit run --all-files` → green.
- [ ] **Step 3:** Full suite `SENTRY_DSN="" TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q -rf` → 0 failed.
- [ ] **Step 4:** Migration dress-rehearsal: full chain → 179 on a throwaway PG.
- [ ] **Step 5:** Go/no-go on staging (179 adds nullable columns + a small backfill — safe). Ensure `settings.app_url` in staging `.env` points at the reachable staging URL so the confirm link is valid.
- [ ] **Step 6:** `./deploy.sh --no-commit` from main.
- [ ] **Step 7:** Live-verify: approve a prepayment → read its `pay_token` from the DB → `POST /p/confirm/{token}` → assert `paid` + the buyer/salesperson/manager in-app alerts + the Paid badge; teardown an approved prepayment → assert `void` + the stand-down. Commit docs.

```bash
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_DATABASE.md
git commit -m "docs: APP_MAP for prepay closure lifecycle"
```

---

## Self-Review Notes

- **Spec coverage:** lifecycle+columns (T1), approve/reject transitions (T2), mark-paid transition (T3), paid/voided notifications (T4), public token confirm + email link (T5), in-app fallback + undo (T6), void-on-teardown (T7), badges (T8), docs+deploy (T9). All spec sections mapped.
- **Sequencing:** T4 (notifications) is referenced by T2/T3/T7 — build T4 before T5-T7 wire it, or guard the calls; T2/T3 can land their transitions first and the notify dispatch resolves once T4 exists. Note in T2/T3.
- **Type consistency:** `PrepaymentStatus` values, `mark_prepayment_paid(...)` signature, `notify_prepayment_paid/_voided(prepayment_id, db=None)`, `pay_token`, `paid_via` in {`accounting_email`,`in_app`} — consistent across T1–T8.
- **Verify-before-code:** confirm the `approval_requests` resolver column name for the backfill; the exact `prepay_request_decide` reject-branch shape; the `_send_group_email`/`_card`/`_email_html` signatures; and that `void` should re-open the line for a fresh prepayment (T8) — all flagged in the tasks.
