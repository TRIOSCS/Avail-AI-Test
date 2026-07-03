# Prepayment-on-PO Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a buyer request a prepayment on a specific PO, route it to a manager for approval in Avail, and notify the accounting/AP Outlook groups by email + Teams channel card on request and approval.

**Architecture:** Extend the existing `Prepayment` engine (model, `create_prepayment`, PREPAYMENT `ApprovalRequest` routing, decide route) to link a specific `BuyPlanLine`, add the missing request UI, add an accounting/AP notification module (email via a logged-in admin's delegated Graph token + a Teams channel card), enrich the manager-facing Prepayment tab, link PO rows to their plan/SO, and auto-cancel a dangling prepayment approval when its PO is cancelled/re-sourced.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (sync), PostgreSQL 16, Alembic, HTMX + Alpine + Jinja2, Microsoft Graph (delegated), pytest (xdist, in-memory SQLite).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-03-prepayment-po-workflow-design.md` (authoritative).
- Status values from `app/constants.py` StrEnums only; `db.get(Model, id)`, not `.query().get()`.
- Money is `Decimal` end-to-end (`total_incl_fees` is `Numeric(12,2)`); never float.
- All schema changes via Alembic; migration + code deploy in the same batch. Migration revision id ≤ 32 chars. Round-trip on a THROWAWAY Postgres 16, never staging. Verify single `alembic heads`.
- Loguru only; every new file gets a header comment (what/calls/depends).
- Run tests with `TESTING=1 PYTHONPATH=/root/availai`. Run the FULL suite with `SENTRY_DSN=""` (its shutdown flush corrupts xdist teardown and manufactures false failures).
- `pre-commit run --files <changed>` after each task (twice if docformatter mutates); `pre-commit run --all-files` before the final deploy.
- After code changes, update the relevant `docs/APP_MAP_*.md`.
- Do NOT deploy until every task is done, the full suite is green, and go/no-go on staging data passes.

---

### Task 1: Link `Prepayment` to a PO line (model + migration 178)

**Files:**
- Modify: `app/models/quality_plan.py:143-182` (`Prepayment`)
- Create: `alembic/versions/178_prepayment_line_link.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt` (append 178 claim line per its header protocol)
- Test: `tests/test_prepayment_line_link.py`

**Interfaces:**
- Produces: `Prepayment.buy_plan_line_id: int | None` (FK `buy_plan_lines.id`, `ondelete=SET NULL`), `Prepayment.buy_plan_line` relationship.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prepayment_line_link.py
"""Prepayment gains a nullable FK to the specific PO line it prepays (migration 178)."""
from app.models.quality_plan import Prepayment

def test_prepayment_has_buy_plan_line_id_column():
    cols = Prepayment.__table__.columns
    assert "buy_plan_line_id" in cols
    fk = list(cols["buy_plan_line_id"].foreign_keys)[0]
    assert fk.column.table.name == "buy_plan_lines"
    assert cols["buy_plan_line_id"].nullable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_prepayment_line_link.py -v --override-ini="addopts="`
Expected: FAIL (KeyError `buy_plan_line_id`).

- [ ] **Step 3: Add the column + relationship + index to `Prepayment`**

In `app/models/quality_plan.py`, inside `Prepayment` after the `buy_plan_id` line (:158):

```python
    buy_plan_line_id = Column(
        Integer, ForeignKey("buy_plan_lines.id", ondelete="SET NULL"), nullable=True
    )
```

After the `buy_plan` relationship (:175):

```python
    buy_plan_line = relationship("BuyPlanLine", foreign_keys=[buy_plan_line_id])
```

In `__table_args__` (:178) add:

```python
        Index("ix_prepayment_buy_plan_line", "buy_plan_line_id"),
```

- [ ] **Step 4: Write migration 178**

Read `alembic/versions/177_qp_section_reviewed_cols.py` for the revision-id string and style. Create `alembic/versions/178_prepayment_line_link.py`:

```python
"""Prepayment: link to the specific PO line (buy_plan_line_id).

Revision ID: 178_prepayment_line_link
Revises: 177_qp_section_reviewed_cols
"""
from alembic import op
import sqlalchemy as sa

revision = "178_prepayment_line_link"
down_revision = "177_qp_section_reviewed_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prepayments",
        sa.Column("buy_plan_line_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_prepayment_buy_plan_line", "prepayments", "buy_plan_lines",
        ["buy_plan_line_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_prepayment_buy_plan_line", "prepayments", ["buy_plan_line_id"])


def downgrade() -> None:
    op.drop_index("ix_prepayment_buy_plan_line", table_name="prepayments")
    op.drop_constraint("fk_prepayment_buy_plan_line", "prepayments", type_="foreignkey")
    op.drop_column("prepayments", "buy_plan_line_id")
```

Append to `MIGRATION_NUMBERS_IN_FLIGHT.txt`: `178 feat/prepayment-po-workflow  Prepayment.buy_plan_line_id FK...` (follow the file's format).

- [ ] **Step 5: Round-trip on a throwaway Postgres + verify single head**

```bash
docker run -d --name migr178-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=t -p 55488:5432 postgres:16
until docker exec migr178-test pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
DATABASE_URL=postgresql://postgres:test@localhost:55488/t TESTING=1 PYTHONPATH=/root/availai alembic upgrade head
DATABASE_URL=postgresql://postgres:test@localhost:55488/t TESTING=1 PYTHONPATH=/root/availai alembic downgrade -1
DATABASE_URL=postgresql://postgres:test@localhost:55488/t TESTING=1 PYTHONPATH=/root/availai alembic upgrade head
TESTING=1 PYTHONPATH=/root/availai alembic heads   # expect single head 178_prepayment_line_link
docker rm -f migr178-test
```
Expected: upgrade→downgrade→upgrade clean; single head.

- [ ] **Step 6: Run the model test (passes) + commit**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_prepayment_line_link.py -v --override-ini="addopts="` → PASS.
`pre-commit run --files app/models/quality_plan.py alembic/versions/178_prepayment_line_link.py MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_prepayment_line_link.py`

```bash
git add app/models/quality_plan.py alembic/versions/178_prepayment_line_link.py MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_prepayment_line_link.py
git commit -m "feat(prepayment): link Prepayment to its PO line (migration 178)"
```

---

### Task 2: `create_prepayment` takes the line, validates it, race-safe duplicate guard

**Files:**
- Modify: `app/services/prepayment_service.py:28-` (`create_prepayment`)
- Test: `tests/test_prepayment_service_line.py`

**Interfaces:**
- Consumes: `Prepayment.buy_plan_line_id` (Task 1).
- Produces: `create_prepayment(db, *, buy_plan_id, buy_plan_line_id, vendor_card_id, payment_method, total_incl_fees, test_report_sent, buyer_remarks, created_by) -> tuple[Prepayment, ApprovalRequest]`. Raises `ValueError` on line-not-on-plan / no cut PO / duplicate pending.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prepayment_service_line.py
"""create_prepayment links the specific line, validates it, and blocks a 2nd pending."""
import pytest
from decimal import Decimal
from app.services.prepayment_service import create_prepayment
# reuse the plan/line/user fixtures pattern from tests/test_po_line_signoff.py
from tests.test_po_line_signoff import _make_user, _make_plan, _make_line

def test_create_prepayment_sets_line(db_session):
    u = _make_user(db_session, can_approve_prepayments=True)  # approver so routing succeeds
    plan = _make_plan(db_session, u)
    line = _make_line(db_session, plan)  # PENDING_VERIFY, po_number set
    db_session.commit()
    pp, req = create_prepayment(
        db_session, buy_plan_id=plan.id, buy_plan_line_id=line.id, vendor_card_id=None,
        payment_method="wire", total_incl_fees=Decimal("20002.38"), test_report_sent=False,
        buyer_remarks="x", created_by=u,
    )
    assert pp.buy_plan_line_id == line.id

def test_create_prepayment_rejects_line_not_on_plan(db_session):
    u = _make_user(db_session, can_approve_prepayments=True)
    plan = _make_plan(db_session, u)
    other = _make_plan(db_session, u)
    stray = _make_line(db_session, other)
    db_session.commit()
    with pytest.raises(ValueError):
        create_prepayment(db_session, buy_plan_id=plan.id, buy_plan_line_id=stray.id,
                          vendor_card_id=None, payment_method="wire",
                          total_incl_fees=Decimal("1"), test_report_sent=False,
                          buyer_remarks=None, created_by=u)

def test_create_prepayment_blocks_second_pending_on_same_line(db_session):
    u = _make_user(db_session, can_approve_prepayments=True)
    plan = _make_plan(db_session, u)
    line = _make_line(db_session, plan)
    db_session.commit()
    create_prepayment(db_session, buy_plan_id=plan.id, buy_plan_line_id=line.id,
                      vendor_card_id=None, payment_method="wire",
                      total_incl_fees=Decimal("5"), test_report_sent=False,
                      buyer_remarks=None, created_by=u)
    with pytest.raises(ValueError):
        create_prepayment(db_session, buy_plan_id=plan.id, buy_plan_line_id=line.id,
                          vendor_card_id=None, payment_method="wire",
                          total_incl_fees=Decimal("5"), test_report_sent=False,
                          buyer_remarks=None, created_by=u)
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_prepayment_service_line.py -v --override-ini="addopts="` → FAIL (unexpected kwarg / no guard).

- [ ] **Step 3: Implement in `create_prepayment`**

Read the current function first. Add `buy_plan_line_id: int` to the signature (after `buy_plan_id`). After the existing ownership gate, before persisting, insert (imports: `from ..models.buy_plan import BuyPlanLine`; `from ..models.approvals import ApprovalRequest`; `from ..constants import ApprovalGateType, ApprovalRequestStatus, ApprovalSubjectType, BuyPlanLineStatus`):

```python
    # Lock the line to serialize concurrent prepayment requests on the same PO.
    line = (
        db.query(BuyPlanLine)
        .filter(BuyPlanLine.id == buy_plan_line_id)
        .with_for_update()
        .one_or_none()
    )
    if line is None or line.buy_plan_id != buy_plan_id:
        raise ValueError("Line does not belong to this buy plan.")
    if not line.po_number or line.status not in (
        BuyPlanLineStatus.PENDING_VERIFY.value, BuyPlanLineStatus.VERIFIED.value
    ):
        raise ValueError("This PO is not ready for a prepayment request.")
    # One in-flight prepayment per PO: block a second REQUESTED prepayment on this line.
    existing = (
        db.query(ApprovalRequest.id)
        .join(Prepayment, Prepayment.id == ApprovalRequest.subject_id)
        .filter(
            ApprovalRequest.subject_type == ApprovalSubjectType.PREPAYMENT.value,
            ApprovalRequest.gate_type == ApprovalGateType.PREPAYMENT.value,
            ApprovalRequest.status == ApprovalRequestStatus.REQUESTED.value,
            Prepayment.buy_plan_line_id == buy_plan_line_id,
        )
        .first()
    )
    if existing:
        raise ValueError("A prepayment for this PO is already awaiting approval.")
```

Set `buy_plan_line_id=buy_plan_line_id` on the `Prepayment(...)` constructor. (Verify the exact enum names — `ApprovalRequestStatus.REQUESTED`, `ApprovalSubjectType.PREPAYMENT`, `ApprovalGateType.PREPAYMENT` — against `app/constants.py` and adjust `.value` usage to match how `queue.py` compares them.)

- [ ] **Step 4: Run tests (pass)** — `pytest tests/test_prepayment_service_line.py -v --override-ini="addopts="` → PASS. Also run `tests/test_po_line_signoff.py` to confirm the fixture import didn't regress.

- [ ] **Step 5: Commit**

```bash
git add app/services/prepayment_service.py tests/test_prepayment_service_line.py
git commit -m "feat(prepayment): validate PO line + race-safe duplicate-pending guard"
```

---

### Task 3: Request entry point — HTMX modal + create route + trigger button

**Files:**
- Modify: `app/routers/prepayments.py` (add HTMX GET modal + HTMX POST create; keep the JSON route, add `buy_plan_line_id` to `PrepaymentCreate` schema in `app/schemas/`)
- Create: `app/templates/htmx/partials/prepayments/request_modal.html`
- Modify: `app/templates/htmx/partials/buy_plans/_macros.html` (add `request_prepayment_button(line, plan)` macro)
- Modify: `app/templates/htmx/partials/buy_plans/_detail_lines.html`, `app/templates/htmx/partials/approvals/_tab_po_approval.html` (render the button)
- Modify: `app/dependencies.py` + `app/template_env.py` (add `can_request_prepayment(user, line)` Jinja global)
- Test: `tests/test_prepayment_request_ui.py`

**Interfaces:**
- Consumes: `create_prepayment(... buy_plan_line_id ...)` (Task 2); `buyplan_workflow._line_amount` (existing).
- Produces: `GET /v2/partials/prepayments/new?line_id={id}` (modal), `POST /v2/partials/prepayments` (HTMX create → toast + re-render), `can_request_prepayment(user, line) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prepayment_request_ui.py
"""The prepayment request modal renders prefilled from a PO; HTMX create makes the record."""
from tests.conftest import ...  # use the project's authenticated-client fixture pattern
# (mirror tests/test_approvals_hub_tabs.py for _client_as/admin_user + plan/line setup)

def test_request_modal_prefills_amount_from_line(client_as_owner, plan_with_line):
    line = plan_with_line.lines[0]
    r = client_as_owner.get(f"/v2/partials/prepayments/new?line_id={line.id}",
                            headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Request prepayment" in r.text
    assert "20" in r.text  # the line amount prefilled into total_incl_fees

def test_htmx_create_makes_prepayment_linked_to_line(client_as_owner, plan_with_line, db_session):
    line = plan_with_line.lines[0]
    r = client_as_owner.post("/v2/partials/prepayments",
        data={"buy_plan_id": plan_with_line.id, "buy_plan_line_id": line.id,
              "payment_method": "wire", "total_incl_fees": "20002.38",
              "test_report_sent": "false", "buyer_remarks": "ok"},
        headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "HX-Trigger" in r.headers  # success toast
```

- [ ] **Step 2: Run to verify failure** — 404 (routes absent).

- [ ] **Step 3: Add `can_request_prepayment` predicate + Jinja global**

In `app/dependencies.py`, near `can_verify_po_line` (added in Phase 3), add:

```python
def can_request_prepayment(user, line) -> bool:
    """A user may request a prepayment on a PO line they can act on (plan ownership)."""
    if user is None or line is None:
        return False
    from .constants import BuyPlanLineStatus
    if not line.po_number or line.status not in (
        BuyPlanLineStatus.PENDING_VERIFY.value, BuyPlanLineStatus.VERIFIED.value
    ):
        return False
    # Reuse the same ownership rule create_prepayment enforces (get_buyplan_for_user).
    from .services.buyplan_access import user_can_access_plan  # find the real helper name
    return user_can_access_plan(user, line.buy_plan)
```

Register it in `app/template_env.py` alongside `can_verify_po_line` (copy that registration line). NOTE: confirm the real ownership-helper name used inside `create_prepayment` (`get_buyplan_for_user`) and call the same one so button-visibility matches the service gate exactly.

- [ ] **Step 4: Add the modal + create routes**

In `app/routers/prepayments.py` add an HTMX GET that renders the modal prefilled from the line, and an HTMX POST that calls `create_prepayment`, commits, fires `notify_prepayment_requested` (Task 5 — leave a `# TODO(task5)` import-guarded call OR land Task 5 first; see sequencing note), and returns `_avatar_response`-style HTMLResponse with an `HX-Trigger` success toast + re-render of the origin surface. Prefill amount = `float(_line_amount(line))` shown, but POST parses `Decimal(total_incl_fees)`. Map `ValueError` from the service to a 400 error-toast HTMLResponse (mirror the prospecting `_prospect_error_toast` pattern). Add `buy_plan_line_id: int` to `PrepaymentCreate` in `app/schemas/…` (find it) so the JSON route also accepts it and passes it through.

Create `request_modal.html` — a modal form (house pattern: see `quotes/edit_form.html`) with fields: vendor (read-only, from `line.offer.vendor_card.display_name`), `total_incl_fees` (number, prefilled, editable), `payment_method` (select: wire/cc/paypal), `test_report_sent` (checkbox), `buyer_remarks` (textarea); posts to `/v2/partials/prepayments` with hidden `buy_plan_id`, `buy_plan_line_id`.

Add `request_prepayment_button(line, plan)` to `buy_plans/_macros.html` (a button that `hx-get`s the modal into `#modal-content` via `$dispatch('open-modal', …)`), gated by `{% if can_request_prepayment(user, line) %}`. Render it in `_detail_lines.html` (pending_verify + verified lines) and in `_tab_po_approval.html`'s action rail (pending rows).

- [ ] **Step 5: Run tests (pass) + commit**

Run: `pytest tests/test_prepayment_request_ui.py -v --override-ini="addopts="` → PASS.

```bash
git add app/routers/prepayments.py app/schemas app/templates/htmx/partials/prepayments/request_modal.html app/templates/htmx/partials/buy_plans/_macros.html app/templates/htmx/partials/buy_plans/_detail_lines.html app/templates/htmx/partials/approvals/_tab_po_approval.html app/dependencies.py app/template_env.py tests/test_prepayment_request_ui.py
git commit -m "feat(prepayment): request modal + HTMX create route + trigger button on POs"
```

---

### Task 4: Notification config keys (Settings)

**Files:**
- Modify: `app/routers/admin/system.py:47` (`SYSTEM_SETTINGS_META`)
- Modify: `app/templates/htmx/partials/settings/system.html`
- Modify: `app/startup.py` (seed empty defaults if the app seeds system_config defaults there)
- Test: `tests/test_prepayment_config_keys.py`

**Interfaces:**
- Produces: config keys `accounting_group_email`, `ap_group_email`, `prepayment_teams_webhook` readable via `admin_service.get_config_value(db, key)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prepayment_config_keys.py
from app.routers.admin.system import SYSTEM_SETTINGS_META

def test_prepayment_notification_keys_registered():
    for k in ("accounting_group_email", "ap_group_email", "prepayment_teams_webhook"):
        assert k in SYSTEM_SETTINGS_META
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Register the keys.** Read `SYSTEM_SETTINGS_META` entries for the exact shape (type/label/description/default), add the three keys with `type: "string"`, empty default, admin-only. Add matching fields to `settings/system.html` (copy an existing text-input row). If defaults are seeded in `startup.py`, add empty seeds.

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/routers/admin/system.py app/templates/htmx/partials/settings/system.html app/startup.py tests/test_prepayment_config_keys.py
git commit -m "feat(prepayment): accounting/AP email + Teams webhook config keys in Settings"
```

---

### Task 5: `prepayment_notifications` module (email + Teams channel card)

**Files:**
- Create: `app/services/prepayment_notifications.py`
- Modify: `app/services/teams_notifications.py:52` (`post_teams_channel_card` gains optional `webhook_url: str | None = None`)
- Test: `tests/test_prepayment_notifications.py`

**Interfaces:**
- Consumes: `Prepayment` (Task 1), config keys (Task 4), the delegated-admin email pattern in `buyplan_notifications.py:589-614` (`notify_stock_sale_approved`), `post_teams_channel_card`.
- Produces: `async notify_prepayment_requested(prepayment_id: int)`, `async notify_prepayment_approved(prepayment_id: int)`, and a `prepayment_id`-keyed `run_prepayment_notify_bg(coro_fn, prepayment_id)` fire-and-forget runner.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prepayment_notifications.py
"""Prepayment notifications email the group DLs + post a Teams channel card, both events."""
from unittest.mock import AsyncMock, patch
import pytest
from decimal import Decimal
from app.services import prepayment_notifications as pn
# build a Prepayment via create_prepayment (Task 2 fixtures), set config via set_config_value

@pytest.mark.asyncio
async def test_requested_emails_groups_and_posts_card(db_session, prepayment_row, set_group_config):
    with patch.object(pn, "_send_group_email", new=AsyncMock()) as email, \
         patch("app.services.prepayment_notifications.post_teams_channel_card", new=AsyncMock()) as card:
        await pn.notify_prepayment_requested(prepayment_row.id)
    to_addrs = email.call_args.kwargs.get("to") or email.call_args.args[1]
    assert "accounting@trio.test" in to_addrs and "ap@trio.test" in to_addrs
    assert card.called

@pytest.mark.asyncio
async def test_unset_config_skips_channel_no_raise(db_session, prepayment_row):
    # no config set → both channels skip, no exception
    with patch("app.services.prepayment_notifications.post_teams_channel_card", new=AsyncMock()) as card:
        await pn.notify_prepayment_requested(prepayment_row.id)
    assert not card.called
```

- [ ] **Step 2: Run → FAIL (module absent).**

- [ ] **Step 3: Generalize `post_teams_channel_card`**

```python
async def post_teams_channel_card(card: dict, webhook_url: str | None = None) -> None:
    webhook_url = webhook_url or get_credential_cached("teams_notifications", "TEAMS_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("Teams webhook not configured — skipping channel card post")
        return
    ...  # unchanged body
```

- [ ] **Step 4: Write `prepayment_notifications.py`**

Header comment + the two notify functions + the bg runner + `_send_group_email` (copy the delegated-admin-token mail send from `buyplan_notifications.py:589-614`: find an admin with a valid Graph token, send to the group addresses; if none, log + skip). Build a `_card(prepayment, event)` Adaptive Card FactSet (vendor, PO#/plan#/SO#, amount incl fees, method, test-report, requester, and for approved: approver + time). Each channel wrapped in its own try/except (best-effort). `notify_prepayment_requested`/`_approved` read config via `admin_service.get_config_values(db, [...])`, gather the group addresses (skip empty), call `_send_group_email` + `post_teams_channel_card(card, webhook)`. The bg runner opens its own `SessionLocal`, `bg_db.get(Prepayment, prepayment_id)`, runs the coro, closes (copy the shape of `run_v3_notify_bg` at `buyplan_notifications.py:37-53` but keyed on `Prepayment`).

- [ ] **Step 5: Run → PASS. Commit.**

```bash
git add app/services/prepayment_notifications.py app/services/teams_notifications.py tests/test_prepayment_notifications.py
git commit -m "feat(prepayment): accounting/AP notifications — group email + Teams channel card"
```

---

### Task 6: Wire notifications into create + approve

**Files:**
- Modify: `app/routers/prepayments.py` (fire `run_prepayment_notify_bg(notify_prepayment_requested, pp.id)` after commit in the HTMX create)
- Modify: `app/routers/htmx/buy_plans.py:290` (`prepay_request_decide`: on the approve branch, fire `..._approved`)
- Test: `tests/test_prepayment_notify_wiring.py`

**Interfaces:**
- Consumes: Task 5 functions/runner; the create route (Task 3); `prepay_request_decide` (existing).

- [ ] **Step 1: Write the failing tests** — patch `run_prepayment_notify_bg`; assert it's dispatched with `notify_prepayment_requested` on create and `notify_prepayment_approved` on approve, and NOT on reject.

```python
# tests/test_prepayment_notify_wiring.py
from unittest.mock import patch
def test_create_dispatches_requested(client_as_owner, plan_with_line):
    line = plan_with_line.lines[0]
    with patch("app.routers.prepayments.run_prepayment_notify_bg") as bg:
        client_as_owner.post("/v2/partials/prepayments", data={...}, headers={"HX-Request":"true"})
    assert bg.called and bg.call_args.args[0].__name__ == "notify_prepayment_requested"

def test_approve_dispatches_approved_reject_does_not(client_as_manager, pending_prepay_request):
    with patch("app.routers.htmx.buy_plans.run_prepayment_notify_bg") as bg:
        client_as_manager.post(f"/v2/partials/approvals/prepay-requests/{pending_prepay_request.id}/decide",
                               data={"action":"approve"}, headers={"HX-Request":"true"})
    assert any(c.args[0].__name__ == "notify_prepayment_approved" for c in bg.call_args_list)
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Wire both call sites** (import the runner + functions; dispatch after the successful commit; approve-branch only for `..._approved`). Resolve the `# TODO(task5)` left in Task 3.
- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/routers/prepayments.py app/routers/htmx/buy_plans.py tests/test_prepayment_notify_wiring.py
git commit -m "feat(prepayment): fire accounting/AP notifications on request + approval"
```

---

### Task 7: Enrich the manager-facing Prepayment tab

**Files:**
- Modify: `app/services/approvals/queue.py` (the prepayment `RowVM` builder — add vendor, method, amount, test_report_sent, PO#, plan_id, so_number)
- Modify: `app/templates/htmx/partials/approvals/_tab_prepayment.html`
- Test: extend `tests/test_approvals_hub_tabs.py`

**Interfaces:**
- Consumes: `Prepayment.buy_plan_line_id`/`buy_plan` (Task 1).

- [ ] **Step 1: Failing test** — a pending prepayment row on the tab shows vendor, amount, the connected PO#, and a link to the plan (`/v2/partials/buy-plans/{plan_id}`) + SO#.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3:** Read how the prepayment `RowVM` is built in `queue.py` (subject resolution @ `:387`); add the fields (from the `Prepayment` + its `buy_plan`/`buy_plan_line`). Render them in `_tab_prepayment.html`'s `prepay_pending_row`/`prepay_resolved_row` (vendor, method, amount already shown — add test-report badge + PO#/SO# + plan link).
- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/services/approvals/queue.py app/templates/htmx/partials/approvals/_tab_prepayment.html tests/test_approvals_hub_tabs.py
git commit -m "feat(prepayment): enrich the manager Prepayment tab (vendor/PO/SO/test-report)"
```

---

### Task 8: PO Approval tab → parent plan/SO link

**Files:**
- Modify: `app/templates/htmx/partials/approvals/_tab_po_approval.html`
- Test: extend `tests/test_approvals_hub_tabs.py`

- [ ] **Step 1: Failing test** — a pending PO row links to `/v2/partials/buy-plans/{plan_id}` and shows the SO# (`row.plan.sales_order_number`). No view-model change (the row already carries `row.plan`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3:** Wrap the identity cell in an `hx-get` link to the plan detail (push `/v2/buy-plans/{plan_id}`) and add `· SO {{ row.plan.sales_order_number }}` to the sub-line when present.
- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/templates/htmx/partials/approvals/_tab_po_approval.html tests/test_approvals_hub_tabs.py
git commit -m "feat(approvals): PO Approval rows link to their plan + show SO#"
```

---

### Task 9: Cancel/re-source auto-cancels a dangling prepayment approval

**Files:**
- Modify: `app/services/buyplan_workflow.py` (`resource_line` cancel/re-source path)
- Test: `tests/test_prepayment_dangling_cancel.py`

**Interfaces:**
- Consumes: `Prepayment.buy_plan_line_id` (Task 1); `ApprovalRequest` PREPAYMENT rows.

- [ ] **Step 1: Failing test** — a line with a pending prepayment `ApprovalRequest`, when `resource_line` cancels/re-sources it, the prepayment's `ApprovalRequest.status` becomes `cancelled` (with a resolution note).

```python
# tests/test_prepayment_dangling_cancel.py
def test_resource_cancels_pending_prepayment_approval(db_session):
    # create plan+line, create_prepayment (pending), then resource_line(cancel)
    # assert the PREPAYMENT ApprovalRequest for that line's prepayment is CANCELLED
    ...
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3:** In `resource_line`, after a line is cancelled/re-sourced, find any REQUESTED PREPAYMENT `ApprovalRequest` whose `Prepayment.buy_plan_line_id == line.id` and set `status=CANCELLED`, `resolved_at=now`, `resolution_note="PO cancelled/re-sourced — prepayment voided"`. Use the same cancel helper the workflow already uses for engine requests if one exists (grep `_cancel_open_engine_requests_for_plan`).
- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add app/services/buyplan_workflow.py tests/test_prepayment_dangling_cancel.py
git commit -m "fix(prepayment): void a pending prepayment approval when its PO is cancelled/re-sourced"
```

---

### Task 10: Docs, full suite, deploy

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md`, `docs/APP_MAP_DATABASE.md`

- [ ] **Step 1:** Update APP_MAP: DATABASE (`prepayments.buy_plan_line_id`), INTERACTIONS (the request→approve→notify flow, config keys, dangling-cancel).
- [ ] **Step 2:** `pre-commit run --all-files` → green.
- [ ] **Step 3:** Full suite: `SENTRY_DSN="" TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q -rf` → 0 failed (investigate any failure serially; the suite has known xdist flakes only when Sentry is on).
- [ ] **Step 4:** Deploy dress-rehearsal: full 175→…→178 chain on a throwaway PG reaches head cleanly.
- [ ] **Step 5:** Go/no-go on staging: count existing `prepayments` rows (178 only adds a nullable column — safe). Then `./deploy.sh --no-commit` from main.
- [ ] **Step 6:** Live-verify: log in, open a plan with a cut PO, "Request prepayment", confirm the manager row shows vendor/PO/SO/amount, approve, confirm health 200. (Notifications need the 3 config keys set + a valid admin Graph token; email/Teams degrade quietly if unset.)
- [ ] **Step 7:** Commit any doc changes.

```bash
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_DATABASE.md
git commit -m "docs: APP_MAP for prepayment-on-PO workflow"
```

---

## Self-Review Notes

- **Spec coverage:** model+migration (T1), line validation+guard (T2), request entry point (T3), config (T4), notifications email+Teams-card (T5) wired both events (T6), enriched manager tab (T7), PO→SO link (T8), dangling-approval cancel (T9), docs+deploy (T10). All spec sections mapped.
- **Sequencing:** T5 (notifications) is referenced by T3/T6; land T5 before wiring in T6, and in T3 guard the notify call so T3's tests pass before T5 exists (or reorder T5 before T3 — executor's choice, noted in T3 Step 4).
- **Type consistency:** `create_prepayment` keyword `buy_plan_line_id` used identically in T2/T3; `notify_prepayment_requested`/`_approved` + `run_prepayment_notify_bg` names consistent T5/T6; `can_request_prepayment` T3 only.
- **Verify-before-code reminders:** confirm real names for the ownership helper (`get_buyplan_for_user`), the prepayment `RowVM` builder, `PrepaymentCreate` schema location, and the `ApprovalRequestStatus`/`ApprovalGateType`/`ApprovalSubjectType` `.value` comparison convention before writing each task.
