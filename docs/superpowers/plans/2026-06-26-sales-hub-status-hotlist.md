# Sales Hub Status Pipeline + Hotlist Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Requisition (Sales Hub) status pipeline with **OPEN · RFQs Sent · Offers · Quoted · Won · Lost**, drop the `archive` *status* in favour of an `is_archived` boolean (hidden-but-retrievable), and add a **HOTLIST** monitor status that wires into the existing Proactive matcher so an incoming offer matching a hotlisted part+customer surfaces a match with one-click send.

**Architecture:** The status lifecycle lives in `RequisitionStatus` (StrEnum) + the `requisition_state.transition()` state machine. The board/filter/badge surfaces read those values. The Proactive matcher (`proactive_matching._find_matches`) is CPH-gated; we add a parallel HOTLIST seeding path keyed on `(material_card_id, customer_site_id)` of active HOTLIST requisitions so it produces a `ProactiveMatch` even with no purchase history — reusing the existing surface + one-click-send pipeline unchanged. Archive becomes an orthogonal boolean, not a stage.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Alembic + HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind. Tests: pytest (xdist, in-memory SQLite via conftest).

## Global Constraints

- New pipeline values (exact): `open`, `rfqs_sent`, `offers`, `quoted`, `won`, `lost`. Plus monitor status `hotlist`. Plus retained `draft` (pre-open) and `cancelled` (kept; existing rows). **No `active`, `sourcing`, `quoting`, `reopened`, `archived` as live pipeline stages.**
- "open automatically means sourcing" → merge `sourcing` AND legacy `active` into `open` (the new entry stage).
- REMOVE the `archive` *status*. Archived reqs become `is_archived = true` (hidden-but-retrievable). Data migration: `sourcing`→`open`, `active`→`open`, `quoting`→`quoted`, `reopened`→`open`; `archived`→ set `is_archived=true` AND status→`lost` fallback (see Task 6 note — **archive-mapping = (B), flagged for user**).
- Status values: always use `RequisitionStatus` StrEnum constants (`app/constants.py`), never raw strings. Migrations: Alembic only, include downgrade. Single alembic head after (`alembic heads`). Base new migration on `down_revision = "156_user_avatar"`.
- Thin router / fat service. Authz unchanged. Reuse existing `status_badge`/`req_status_badge` + action-rail patterns. Accent/density conformance (use `var(--accent)`, `.badge`, `.btn-primary` primitives — no new color conventions).
- Tests with every change. `pre-commit run --all-files` before commit (twice if docformatter rewraps). Migration round-trip tested on a THROWAWAY Postgres only — NEVER the live/compose/.env DATABASE_URL.
- Commit message: `feat(sales-hub): Open/RFQs/Offers/Quoted/Won/Lost pipeline + Hotlist monitoring; drop Archive`. DO NOT push.

---

### Task 1: RequisitionStatus enum — new pipeline + HOTLIST

**Files:**
- Modify: `app/constants.py:104-124` (RequisitionStatus)
- Test: `tests/test_requisition_status_enum.py` (Create)

**Interfaces:**
- Produces: `RequisitionStatus` with members `DRAFT, OPEN, RFQS_SENT, OFFERS, QUOTED, WON, LOST, HOTLIST, CANCELLED`; nonmembers `TERMINAL = frozenset({"won","lost","cancelled"})`, `OPEN_PIPELINE = frozenset({"open","rfqs_sent","offers","quoted"})`, `MONITOR = frozenset({"hotlist"})`.

- [ ] **Step 1: Write failing test** — `tests/test_requisition_status_enum.py`
```python
"""Locks the reworked Requisition pipeline enum. Called by: pytest. Depends on: app.constants."""
from app.constants import RequisitionStatus


def test_pipeline_members_exact():
    vals = {e.value for e in RequisitionStatus}
    assert vals == {"draft", "open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist", "cancelled"}


def test_archived_and_sourcing_removed():
    assert not hasattr(RequisitionStatus, "ARCHIVED")
    assert not hasattr(RequisitionStatus, "SOURCING")
    assert not hasattr(RequisitionStatus, "ACTIVE")


def test_terminal_and_open_pipeline_sets():
    assert RequisitionStatus.TERMINAL == frozenset({"won", "lost", "cancelled"})
    assert RequisitionStatus.OPEN_PIPELINE == frozenset({"open", "rfqs_sent", "offers", "quoted"})
    assert RequisitionStatus.MONITOR == frozenset({"hotlist"})
```

- [ ] **Step 2: Run, verify fail** — `TESTING=1 PYTHONPATH=$PWD pytest tests/test_requisition_status_enum.py -v` → FAIL.

- [ ] **Step 3: Implement** — replace `app/constants.py:104-124` class body:
```python
class RequisitionStatus(StrEnum):
    """Status lifecycle for Requisition records.

    Pipeline (Sales Hub): OPEN -> RFQS_SENT -> OFFERS -> QUOTED -> WON/LOST.
    DRAFT precedes OPEN. HOTLIST is an off-pipeline *monitor* state: the
    salesperson watches a part/customer and the Proactive matcher surfaces an
    offer when stock appears. CANCELLED retained for existing rows. Archive is
    NOT a status — see Requisition.is_archived (hidden-but-retrievable).
    """

    DRAFT = "draft"
    OPEN = "open"  # entry stage; "open" automatically means sourcing
    RFQS_SENT = "rfqs_sent"
    OFFERS = "offers"
    QUOTED = "quoted"
    WON = "won"
    LOST = "lost"
    HOTLIST = "hotlist"  # monitor-only; surfaced by Proactive on a matching offer
    CANCELLED = "cancelled"

    # Terminal (done) — excluded from the default open list. Single source of truth.
    # `nonmember` keeps these off the member list (they're constants, not statuses).
    TERMINAL = nonmember(frozenset({"won", "lost", "cancelled"}))
    # Active pipeline stages shown by default in the Sales Hub list.
    OPEN_PIPELINE = nonmember(frozenset({"open", "rfqs_sent", "offers", "quoted"}))
    # Off-pipeline monitor states (Hotlist).
    MONITOR = nonmember(frozenset({"hotlist"}))
```

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit** — `git add app/constants.py tests/test_requisition_status_enum.py && git commit -m "feat(sales-hub): rework RequisitionStatus to Open/RFQs/Offers/Quoted/Won/Lost + Hotlist"`

---

### Task 2: Requisition model — is_archived column + default OPEN

**Files:**
- Modify: `app/models/sourcing.py:30-54` (status default; add `is_archived`, index)
- Test: `tests/test_requisition_model_archive.py` (Create)

**Interfaces:**
- Consumes: `RequisitionStatus` (Task 1).
- Produces: `Requisition.is_archived` (Boolean, not null, default False, server_default "false", indexed `ix_requisitions_is_archived`); `Requisition.status` Python default now `"open"`.

- [ ] **Step 1: Write failing test** — `tests/test_requisition_model_archive.py`
```python
"""Requisition.is_archived + default status. Called by: pytest. Depends on: app.models, conftest db."""
from app.models import Requisition


def test_default_status_is_open(db):
    r = Requisition(name="R1")
    db.add(r)
    db.commit()
    assert r.status == "open"
    assert r.is_archived is False


def test_is_archived_settable(db):
    r = Requisition(name="R2", is_archived=True)
    db.add(r)
    db.commit()
    assert r.is_archived is True
```

- [ ] **Step 2: Run, verify fail** (`is_archived` not a column).

- [ ] **Step 3: Implement** — in `app/models/sourcing.py`:
  - line 49: `status = Column(String(50), default="open")`
  - after line 54 (`is_scratch` column) add:
```python
    # Archive is orthogonal to the status pipeline: hidden-but-retrievable.
    # Replaces the removed ARCHIVED status (migration 157). The Sales Hub list
    # hides archived reqs by default; an "Archived" filter retrieves them.
    is_archived = Column(Boolean, nullable=False, default=False, server_default="false")
```
  - in `__table_args__` (after line 42) add: `Index("ix_requisitions_is_archived", "is_archived"),`

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit** — `git add app/models/sourcing.py tests/test_requisition_model_archive.py && git commit -m "feat(sales-hub): add Requisition.is_archived; default status open"`

---

### Task 3: State machine — new transitions + archive/hotlist actions

**Files:**
- Modify: `app/services/requisition_state.py:16-29` (ALLOWED_TRANSITIONS), add `set_archived`, `set_hotlist` helpers
- Test: `tests/test_requisition_state.py` (Modify — add cases)

**Interfaces:**
- Consumes: `RequisitionStatus`, `Requisition.is_archived` (Tasks 1-2).
- Produces:
  - `ALLOWED_TRANSITIONS` covering the new pipeline.
  - `transition(req, new_status, actor, db)` (existing signature unchanged).
  - `set_archived(req, archived: bool, actor, db) -> None` — sets `req.is_archived`, logs ActivityLog (REQ_ARCHIVED / REQ_UNARCHIVED). On un-archive, status is left as-is (already a valid pipeline/terminal value).
  - `set_hotlist(req, actor, db) -> None` — transitions status to `hotlist` from any non-terminal/non-hotlist state, logs STATUS_CHANGED.

- [ ] **Step 1: Add failing tests** to `tests/test_requisition_state.py`:
```python
def test_transition_open_to_rfqs_sent(db, sample_req):
    from app.services.requisition_state import transition
    sample_req.status = "open"
    transition(sample_req, "rfqs_sent", None, db)
    assert sample_req.status == "rfqs_sent"


def test_legacy_sourcing_origin_allows_open(db, sample_req):
    # rows still on a legacy value can always move to open
    from app.services.requisition_state import transition
    sample_req.status = "sourcing"
    transition(sample_req, "open", None, db)
    assert sample_req.status == "open"


def test_set_hotlist_and_back(db, sample_req):
    from app.services.requisition_state import set_hotlist, transition
    sample_req.status = "open"
    set_hotlist(sample_req, None, db)
    assert sample_req.status == "hotlist"
    transition(sample_req, "open", None, db)
    assert sample_req.status == "open"


def test_set_archived_toggle(db, sample_req):
    from app.services.requisition_state import set_archived
    set_archived(sample_req, True, None, db)
    assert sample_req.is_archived is True
    set_archived(sample_req, False, None, db)
    assert sample_req.is_archived is False
```
(If `sample_req` fixture absent, add one creating a committed `Requisition(name="T", status="open")`.)

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** — replace `ALLOWED_TRANSITIONS` (lines 16-29):
```python
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"open", "hotlist"},
    "open": {"rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "rfqs_sent": {"open", "offers", "quoted", "won", "lost", "hotlist"},
    "offers": {"open", "quoted", "won", "lost", "hotlist"},
    "quoted": {"open", "offers", "won", "lost", "hotlist"},
    "won": {"open"},
    "lost": {"open", "hotlist"},
    "hotlist": {"open", "rfqs_sent", "offers", "quoted", "won", "lost"},
    "cancelled": {"open"},
    # Legacy origins (pre-157 rows / in-flight sessions) — always allow normalising to open.
    "active": {"open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "sourcing": {"open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "quoting": {"open", "quoted", "won", "lost", "hotlist"},
    "reopened": {"open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "archived": {"open"},
}
```
  Change `old_status = req.status or "active"` → `or "open"` (line 37).
  Append two helpers (reuse the ActivityLog pattern already in `transition`):
```python
def set_hotlist(req, actor, db: Session) -> None:
    """Put a requisition on the Hotlist monitor (Proactive surfaces matches)."""
    transition(req, RequisitionStatus.HOTLIST, actor, db)


def set_archived(req, archived: bool, actor, db: Session) -> None:
    """Archive/unarchive a requisition (hidden-but-retrievable; orthogonal to status)."""
    from ..constants import ActivityType
    if req.is_archived == archived:
        return
    req.is_archived = archived
    try:
        actor_id = actor.id if actor else None
        db.add(
            ActivityLog(
                user_id=actor_id,
                activity_type=ActivityType.REQ_ARCHIVED if archived else ActivityType.REQ_UNARCHIVED,
                channel=Channel.SYSTEM,
                requisition_id=req.id,
                subject="Archived" if archived else "Unarchived",
            )
        )
    except Exception as e:  # pragma: no cover - logging best-effort
        logger.error("Failed to log archive change: {}", e, exc_info=True)
```

- [ ] **Step 4: Run, verify pass** — `TESTING=1 PYTHONPATH=$PWD pytest tests/test_requisition_state.py -v`.

- [ ] **Step 5: Commit** — `git add app/services/requisition_state.py tests/test_requisition_state.py && git commit -m "feat(sales-hub): new pipeline transitions + set_archived/set_hotlist"`

---

### Task 4: List service + legacy core filter — open list excludes archived, archived/hotlist filters

**Files:**
- Modify: `app/services/requisition_list_service.py:461-476`
- Modify: `app/routers/requisitions/core.py:72-84` (counts), `:359-368` (filter), `:558-585` (archive toggle → use is_archived)
- Modify: `app/schemas/requisitions2.py:16-24` (ReqStatus enum), `:54` (default)
- Test: `tests/test_requisition_list_service.py` (Modify), `tests/test_requisitions_core_coverage.py` (Modify)

**Interfaces:**
- Consumes: `RequisitionStatus.OPEN_PIPELINE`, `Requisition.is_archived`, `set_archived` (Tasks 1-3).
- Produces: list service default (`status=open` filter meaning "active pipeline, not archived"); `ReqStatus` values `all, open, rfqs_sent, offers, quoted, won, lost, hotlist, archived`.

- [ ] **Step 1: Add failing tests** to `tests/test_requisition_list_service.py`:
```python
def test_archived_hidden_by_default(db, make_req):
    make_req(status="open")
    make_req(status="open", is_archived=True)
    from app.schemas.requisitions2 import ReqListFilters, ReqStatus
    from app.services.requisition_list_service import list_requisitions
    res = list_requisitions(db, ReqListFilters(status=ReqStatus.all), user_id=None, user_role="manager")
    assert all(not r["is_archived"] for r in res["requisitions"])


def test_archived_filter_shows_archived(db, make_req):
    make_req(status="lost", is_archived=True)
    from app.schemas.requisitions2 import ReqListFilters, ReqStatus
    from app.services.requisition_list_service import list_requisitions
    res = list_requisitions(db, ReqListFilters(status=ReqStatus.archived), user_id=None, user_role="manager")
    assert len(res["requisitions"]) == 1 and res["requisitions"][0]["is_archived"]


def test_hotlist_filter(db, make_req):
    make_req(status="hotlist")
    make_req(status="open")
    from app.schemas.requisitions2 import ReqListFilters, ReqStatus
    from app.services.requisition_list_service import list_requisitions
    res = list_requisitions(db, ReqListFilters(status=ReqStatus.hotlist), user_id=None, user_role="manager")
    assert len(res["requisitions"]) == 1 and res["requisitions"][0]["status"] == "hotlist"
```
(Add a `make_req` fixture that commits a `Requisition(name=..., status=..., is_archived=...)` if absent; and ensure the row dict at service line ~558 includes `"is_archived": r.is_archived` — add it.)

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3a: ReqStatus enum** — replace `app/schemas/requisitions2.py:16-24`:
```python
class ReqStatus(str, Enum):
    all = "all"
    open = "open"
    rfqs_sent = "rfqs_sent"
    offers = "offers"
    quoted = "quoted"
    won = "won"
    lost = "lost"
    hotlist = "hotlist"
    archived = "archived"
```
  And `ReqListFilters.status` default (line 54): `status: ReqStatus = ReqStatus.open`.

- [ ] **Step 3b: list service filter** — replace `requisition_list_service.py:461-476`. Always exclude archived unless explicitly asked:
```python
    # ── Archived gate (orthogonal to status pipeline) ────────────────
    elif filters.status.value == "archived":
        query = query.filter(Requisition.is_archived.is_(True))
    else:
        query = query.filter(Requisition.is_archived.is_(False))
        if filters.status.value == "all":
            pass
        elif filters.status.value == "open":
            query = query.filter(Requisition.status.in_(list(RequisitionStatus.OPEN_PIPELINE)))
        else:
            query = query.filter(Requisition.status == filters.status.value)
```
  Add `"is_archived": r.is_archived,` to the row dict (near line 558).

- [ ] **Step 3c: legacy core filter** — `core.py:359-368`: change `status == "archive"` branch to `query.filter(Requisition.is_archived.is_(True))`; the default `else` to `query.filter(Requisition.is_archived.is_(False), Requisition.status.in_(list(RequisitionStatus.OPEN_PIPELINE)))`. Counts (72-84): `open_cnt` → `status.in_(OPEN_PIPELINE)` AND `is_archived.is_(False)`; `archive_cnt` → `is_archived.is_(True)`.

- [ ] **Step 3d: archive toggle** — `core.py:558-585`: replace body to call `set_archived(req, not req.is_archived, user, db)` from `requisition_state`; return `{"ok": True, "is_archived": req.is_archived}`.

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_requisition_list_service.py tests/test_requisitions_core_coverage.py -v`.

- [ ] **Step 5: Commit** — `git commit -am "feat(sales-hub): list/counts/filters use is_archived + OPEN_PIPELINE; hotlist filter"`

---

### Task 5: UI surfaces — filters, badges, action rail, inline edit, detail panel, Hotlist action

**Files:**
- Modify: `app/templates/requisitions2/_filters.html:13-21` (status dropdown)
- Modify: `app/templates/htmx/partials/shared/_macros.html` — `req_status_badge` (113-126), `status_dot`/`opp_status_cell` label+bucket maps (202-214 region), `opp_row_action_rail` (670+) add Hotlist button + use is_archived for archive/restore gating
- Modify: `app/templates/requisitions2/_inline_cell.html:23` (status options)
- Modify: `app/templates/requisitions2/_detail_panel.html:8-16,49,59` (status_styles dict, archive/won/hotlist gating)
- Modify: `app/schemas/requisitions2.py` RowActionName/BulkActionName — add `hotlist`, `unhotlist`; replace archive semantics keep slug but route to is_archived
- Modify: `app/routers/requisitions2.py:380-454` row_action (map hotlist→set_hotlist, archive→set_archived(True), activate/restore→set_archived(False)); `:300-374` inline status (allow hotlist; transition() handles validation)
- Test: `tests/test_requisitions2_templates.py` (Modify), `tests/test_requisitions2_routes.py` (Modify)

**Interfaces:**
- Consumes: `set_hotlist`, `set_archived`, new `RequisitionStatus` (Tasks 1-4).
- Produces: status filter dropdown values `all, open, rfqs_sent, offers, quoted, won, lost, hotlist, archived`; a Hotlist row/detail action posting to `/requisitions2/{id}/action/hotlist`.

- [ ] **Step 1: Add failing template/route tests** to `tests/test_requisitions2_templates.py` / `_routes.py`:
```python
def test_filter_has_new_statuses(client):
    html = client.get("/requisitions2").text
    for v in ("rfqs_sent", "offers", "quoted", "won", "lost", "hotlist", "archived"):
        assert f'value="{v}"' in html
    assert 'value="sourcing"' not in html and 'value="active"' not in html


def test_hotlist_action_sets_status(client, make_req, login_buyer):
    r = make_req(status="open")
    resp = client.post(f"/requisitions2/{r.id}/action/hotlist")
    assert resp.status_code == 200
    # reload
    assert client.get(f"/requisitions2/{r.id}/detail").status_code == 200
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3a: filters** — `_filters.html:13-21`:
```html
  <select name="status" class="px-2 py-1.5 text-sm border border-gray-300 rounded-lg">
    <option value="all">All statuses</option>
    <option value="open">Open</option>
    <option value="rfqs_sent">RFQs Sent</option>
    <option value="offers">Offers</option>
    <option value="quoted">Quoted</option>
    <option value="hotlist">Hotlist</option>
    <option value="won">Won</option>
    <option value="lost">Lost</option>
    <option value="archived">Archived</option>
  </select>
```

- [ ] **Step 3b: req_status_badge** — `_macros.html:113-126` req_map:
```jinja2
{%- set req_map = {
  "draft": "bg-slate-50 text-slate-500 border-slate-200",
  "open": "bg-sky-50 text-sky-600 border-sky-200",
  "rfqs_sent": "bg-amber-50 text-amber-600 border-amber-200",
  "offers": "bg-emerald-50 text-emerald-600 border-emerald-200",
  "quoted": "bg-violet-50 text-violet-600 border-violet-200",
  "hotlist": "bg-orange-50 text-orange-600 border-orange-200",
  "won": "bg-emerald-50 text-emerald-700 border-emerald-300",
  "lost": "bg-rose-50 text-rose-500 border-rose-200",
  "cancelled": "bg-gray-50 text-gray-400 border-gray-200"
} -%}
```
  And `status_dot`/`opp_status_cell` maps (~202-214): bucket_map `{'open':'open','rfqs_sent':'sourcing','offers':'offered','quoted':'quoted','hotlist':'neutral'}`; label_map `{'open':'Open','rfqs_sent':'RFQs Sent','offers':'Offers','quoted':'Quoted','hotlist':'Hotlist'}`. (Verify the `.opp-status-dot--*` tokens used exist in styles.css; reuse existing bucket names only — do not invent new ones.)

- [ ] **Step 3c: inline_cell** — `_inline_cell.html:23` list: `['draft', 'open', 'rfqs_sent', 'offers', 'quoted', 'hotlist', 'won', 'lost']`.

- [ ] **Step 3d: action rail + detail** — `opp_row_action_rail` (670+): the archive button currently keys on `req.status != 'archived'`; change to `not req.is_archived` (archive) / else restore via `/action/activate`. Add a Hotlist toggle button (post `/requisitions2/{id}/action/hotlist`, icon = star/eye; aria-label "Add {{req.name}} to Hotlist"), shown when `req.status not in ('hotlist','won','lost','cancelled')`. `_detail_panel.html`: `status_styles` dict → add open/rfqs_sent/offers/quoted/hotlist/won/lost; archive action gating uses `req.is_archived`; Won gating set `req.status in ('open','rfqs_sent','offers','quoted','hotlist')`; add a "Hotlist" action_btn when not already hotlist/terminal.

- [ ] **Step 3e: schema + router** — `requisitions2.py` `RowActionName` add `hotlist = "hotlist"`. In `row_action` (380-454): map `hotlist` → `set_hotlist(req, user, db)`; `archive` → `set_archived(req, True, user, db)`; `activate` → `set_archived(req, False, user, db)` (un-archive; status untouched if already a valid stage, else `transition` to open). Keep won/lost via `transition`. Inline status save (300-374) already calls `transition` — no change beyond enum.

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_requisitions2_templates.py tests/test_requisitions2_routes.py -v`.

- [ ] **Step 5: Commit** — `git commit -am "feat(sales-hub): UI surfaces for new pipeline + Hotlist action; archive via is_archived"`

---

### Task 6: Alembic data migration 157 (schema + data) + THROWAWAY-PG round-trip

**Files:**
- Create: `alembic/versions/157_req_pipeline_hotlist.py`
- Test: round-trip on throwaway PG (manual, scripted in step 4)

**Interfaces:**
- Consumes: `down_revision = "156_user_avatar"`.
- Produces: column `requisitions.is_archived BOOLEAN NOT NULL DEFAULT false`, index `ix_requisitions_is_archived`; data remap of `status`.

**ARCHIVE-MAPPING DECISION — (B), FLAGGED:** archived rows → `is_archived = true` AND `status = 'lost'` (a terminal placeholder so the pipeline value is valid; the row stays hidden-but-retrievable via the Archived filter regardless of status). **User to confirm B vs A(`archived→lost`, no boolean) vs C.** If A is chosen later, a follow-up migration drops the column; the boolean is additive and reversible.

- [ ] **Step 1: Write migration** — `alembic/versions/157_req_pipeline_hotlist.py`:
```python
"""Rework requisition pipeline + add is_archived; drop archive status.

Revision ID: 157_req_pipeline_hotlist
Revises: 156_user_avatar
Create Date: 2026-06-26

Data remap: sourcing/active/reopened -> open, quoting -> quoted,
archived -> is_archived=true + status lost. Adds is_archived column.
"""

import sqlalchemy as sa
from alembic import op

revision = "157_req_pipeline_hotlist"
down_revision = "156_user_avatar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "requisitions",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_requisitions_is_archived", "requisitions", ["is_archived"])
    bind = op.get_bind()
    # Archived -> hidden boolean + terminal placeholder status.
    bind.execute(sa.text("UPDATE requisitions SET is_archived = true, status = 'lost' WHERE status = 'archived'"))
    # Merge legacy active stages into the new single entry stage.
    bind.execute(sa.text("UPDATE requisitions SET status = 'open' WHERE status IN ('active', 'sourcing', 'reopened')"))
    bind.execute(sa.text("UPDATE requisitions SET status = 'quoted' WHERE status = 'quoting'"))
    # Default for any null.
    bind.execute(sa.text("UPDATE requisitions SET status = 'open' WHERE status IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    # Best-effort reverse: archived rows recover their archived status.
    bind.execute(sa.text("UPDATE requisitions SET status = 'archived' WHERE is_archived = true"))
    op.drop_index("ix_requisitions_is_archived", table_name="requisitions")
    op.drop_column("requisitions", "is_archived")
```

- [ ] **Step 2: Confirm single head (file scan)** — verify nothing else has `down_revision = "156_user_avatar"`:
```bash
grep -rl 'down_revision = "156_user_avatar"' alembic/versions/ | grep -v 157_req_pipeline_hotlist
```
Expected: empty.

- [ ] **Step 3: Spin up THROWAWAY Postgres** (NEVER the live/.env DB):
```bash
docker run --rm -d --name avail_throwaway_pg -p 55433:5432 \
  -e POSTGRES_PASSWORD=x -e POSTGRES_USER=x -e POSTGRES_DB=x postgres:16-alpine
sleep 4  # via Monitor/until-loop in practice
```

- [ ] **Step 4: Round-trip** (upgrade to base→head, seed an archived+sourcing row pre-157, re-run, verify; downgrade; upgrade again):
```bash
export DATABASE_URL="postgresql+psycopg2://x:x@localhost:55433/x"
TESTING=0 PYTHONPATH=$PWD alembic upgrade 156_user_avatar
# seed legacy rows BEFORE 157 so the data UPDATEs have something to remap
docker exec -i avail_throwaway_pg psql -U x -d x -c \
  "INSERT INTO requisitions (name, status) VALUES ('legacy-arch','archived'),('legacy-src','sourcing'),('legacy-quoting','quoting');"
TESTING=0 PYTHONPATH=$PWD alembic upgrade head
TESTING=0 PYTHONPATH=$PWD alembic heads          # expect single head 157_req_pipeline_hotlist
docker exec -i avail_throwaway_pg psql -U x -d x -c \
  "SELECT name,status,is_archived FROM requisitions ORDER BY name;"
# expect: legacy-arch -> lost/true, legacy-quoting -> quoted/false, legacy-src -> open/false
TESTING=0 PYTHONPATH=$PWD alembic downgrade -1   # back to 156
TESTING=0 PYTHONPATH=$PWD alembic upgrade head   # forward again, must succeed
```

- [ ] **Step 5: Tear down THROWAWAY PG** (always, even on failure):
```bash
docker rm -f avail_throwaway_pg
unset DATABASE_URL
```

- [ ] **Step 6: Commit** — `git add alembic/versions/157_req_pipeline_hotlist.py && git commit -m "feat(sales-hub): migration 157 — is_archived + status remap"`

---

### Task 7: Hotlist → Proactive matcher wiring

**Files:**
- Modify: `app/services/proactive_matching.py` — add `_find_hotlist_matches(...)` and call it from `_find_matches` (or compose in `find_matches_for_offer`)
- Test: `tests/test_proactive_hotlist.py` (Create)

**Interfaces:**
- Consumes: `RequisitionStatus.HOTLIST`, `ProactiveMatch`, `Requisition`, `CustomerSite`, `Company`, existing scoring/suppression helpers.
- Produces: `find_matches_for_offer(offer_id, db)` ALSO returns matches seeded from active HOTLIST requisitions whose `material_card_id == offer.material_card_id`, even with no CPH history. Match carries `requisition_id` of the hotlist req, `customer_site_id`/`company_id`/`salesperson_id` from it, `match_score` (use CPH score if history exists else a baseline e.g. 60), reusing the same `ProactiveMatch` row → surface → one-click-send path.

**Wiring rationale (load-bearing):** Current `_find_matches` returns `[]` when `cph_rows` is empty (`proactive_matching.py:203`). A hotlisted part the customer never bought has no CPH, so it would never surface. We add a second seeding source: active HOTLIST reqs for the same material_card. Dedup stays `(material_card_id, company_id)` so a customer with BOTH history and a hotlist req gets one match (hotlist req_id preferred for surfacing). Suppression (do-not-offer/throttle) and the existing send pipeline are reused verbatim.

- [ ] **Step 1: Write failing test** — `tests/test_proactive_hotlist.py`:
```python
"""Hotlist reqs seed Proactive matches even with no purchase history.
Called by: pytest. Depends on: proactive_matching, models, conftest db."""
from app.constants import ProactiveMatchStatus, RequisitionStatus
from app.models import Company, CustomerSite, Offer, ProactiveMatch, Requirement, Requisition
from app.services.proactive_matching import find_matches_for_offer


def _setup(db, mcid=1):
    co = Company(name="Acme", account_owner_id=1)
    db.add(co); db.flush()
    site = CustomerSite(company_id=co.id, is_active=True)
    db.add(site); db.flush()
    req = Requisition(name="watch", status=RequisitionStatus.HOTLIST.value,
                      customer_site_id=site.id, company_id=co.id, created_by=1)
    db.add(req); db.flush()
    db.add(Requirement(requisition_id=req.id, material_card_id=mcid, primary_mpn="ABC123"))
    offer = Offer(material_card_id=mcid, mpn="ABC123", unit_price=10, status="active")
    db.add(offer); db.commit()
    return co, site, req, offer


def test_hotlist_seeds_match_without_cph(db):
    co, site, req, offer = _setup(db)
    matches = find_matches_for_offer(offer.id, db)
    assert any(m.requisition_id == req.id and m.company_id == co.id for m in matches)
    db.commit()
    assert db.query(ProactiveMatch).filter_by(requisition_id=req.id).count() == 1


def test_hotlist_match_surfaces_status_new(db):
    _co, _site, req, offer = _setup(db, mcid=2)
    find_matches_for_offer(offer.id, db); db.commit()
    m = db.query(ProactiveMatch).filter_by(requisition_id=req.id).first()
    assert m.status == ProactiveMatchStatus.NEW
```
(Adjust model kwargs to match real columns; the conftest `db` fixture + a user id=1 may need seeding — mirror `tests/test_proactive_matching.py` setup.)

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** — in `proactive_matching.py`, add a hotlist seeding pass invoked from `find_matches_for_offer` after the CPH `_find_matches`:
```python
def _find_hotlist_matches(db: Session, *, material_card_id: int, mpn: str, our_cost, source_offer) -> list:
    """Seed ProactiveMatch rows from active HOTLIST requisitions for this part.

    Unlike the CPH path this does NOT require purchase history — a hotlist is an
    explicit salesperson request to monitor a part for a customer. Reuses the
    same suppression + dedup + surface pipeline.
    """
    from ..constants import RequisitionStatus
    mpn_upper = normalize_mpn(mpn) or mpn.upper().strip()
    fallback_offer_id = source_offer.id if source_offer else None
    if not fallback_offer_id:
        return []
    rows = (
        db.query(Requisition, CustomerSite, Company)
        .join(Requirement, Requirement.requisition_id == Requisition.id)
        .join(CustomerSite, CustomerSite.id == Requisition.customer_site_id)
        .join(Company, Company.id == Requisition.company_id)
        .filter(
            Requisition.status == RequisitionStatus.HOTLIST.value,
            Requisition.is_archived.is_(False),
            Requirement.material_card_id == material_card_id,
            Company.account_owner_id.isnot(None),
            CustomerSite.is_active.is_(True),
        )
        .all()
    )
    if not rows:
        return []
    existing = {
        r[0] for r in db.query(ProactiveMatch.company_id).filter(
            ProactiveMatch.material_card_id == material_card_id,
            ProactiveMatch.status.in_([ProactiveMatchStatus.NEW, ProactiveMatchStatus.SENT]),
        ).all()
    }
    dno = build_batch_dno_set(db, mpn_upper, {c.id for _, _, c in rows})
    out = []
    for req, site, company in rows:
        if company.id in existing or company.id in dno:
            continue
        score = 60  # baseline — explicit monitor request, no history to weight
        match = ProactiveMatch(
            offer_id=fallback_offer_id, requirement_id=None, requisition_id=req.id,
            customer_site_id=site.id, salesperson_id=company.account_owner_id,
            mpn=mpn_upper, material_card_id=material_card_id, company_id=company.id,
            match_score=score, margin_pct=None, customer_purchase_count=0,
            our_cost=our_cost,
        )
        db.add(match); out.append(match); existing.add(company.id)
        db.add(ActivityLog(
            user_id=company.account_owner_id, activity_type="proactive_match", channel="system",
            requisition_id=req.id, company_id=company.id, contact_name=company.name,
            subject=f"Hotlist match: {mpn_upper} — {company.name}",
        ))
    return out
```
  And in `find_matches_for_offer`:
```python
    cph_matches = _find_matches(db, material_card_id=offer.material_card_id,
                               mpn=offer.mpn or "", our_cost=cost, source_offer=offer)
    hot_matches = _find_hotlist_matches(db, material_card_id=offer.material_card_id,
                               mpn=offer.mpn or "", our_cost=cost, source_offer=offer)
    return cph_matches + hot_matches
```
  (Dedup across the two sources: `_find_matches` already records `existing_match_company_ids` from the DB at its start; since CPH runs first and `db.add`s rows, the hotlist pass's `existing` query won't see uncommitted adds — so also filter hotlist company_ids against the set of `m.company_id for m in cph_matches`.)

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_proactive_hotlist.py tests/test_proactive_matching.py -v`.

- [ ] **Step 5: Commit** — `git commit -am "feat(sales-hub): wire Hotlist reqs into Proactive matcher (seed matches without CPH)"`

---

### Task 8: Static analysis, full suite, docs, pre-commit

**Files:**
- Modify: `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md` (status pipeline + is_archived + hotlist→proactive flow)
- Verify: `tests/test_static_analysis.py` + full requisition/proactive suites

- [ ] **Step 1: Grep for stragglers** — any remaining `"sourcing"`/`"archived"`/`"active"` requisition-status literals in templates/routers/services (NOT requirement `sourcing_status`, which is a separate enum and unchanged). Fix each to the new values.
```bash
grep -rn "RequisitionStatus.ARCHIVED\|RequisitionStatus.SOURCING\|RequisitionStatus.ACTIVE\|RequisitionStatus.QUOTING\|RequisitionStatus.REOPENED" app/
```
Expected: empty after fixes (and no import of removed members anywhere — those would be ImportErrors).

- [ ] **Step 2: Static analysis test** — `TESTING=1 PYTHONPATH=$PWD pytest tests/test_static_analysis.py -v` → PASS.

- [ ] **Step 3: Full targeted suite**:
```bash
TESTING=1 PYTHONPATH=$PWD pytest tests/test_requisition_status_enum.py tests/test_requisition_model_archive.py \
  tests/test_requisition_state.py tests/test_requisition_list_service.py tests/test_requisitions2_routes.py \
  tests/test_requisitions2_templates.py tests/test_requisitions_core_coverage.py tests/test_proactive_hotlist.py \
  tests/test_proactive_matching.py tests/test_routers_proactive.py tests/test_schemas_requisitions.py \
  tests/test_integration_requisitions.py tests/test_static_analysis.py -v
```
Then the WHOLE suite once: `TESTING=1 PYTHONPATH=$PWD pytest tests/ -q` → all green (fix any req-status fallout).

- [ ] **Step 4: Docs** — update APP_MAP_DATABASE (Requisition.status values + is_archived) and APP_MAP_INTERACTIONS (Hotlist → Proactive seeding path).

- [ ] **Step 5: pre-commit** — `pre-commit run --all-files` (run twice if docformatter rewraps). All hooks pass.

- [ ] **Step 6: Final commit** — single squash-free commit is fine; ensure the canonical message exists at least once: amend the last to `feat(sales-hub): Open/RFQs/Offers/Quoted/Won/Lost pipeline + Hotlist monitoring; drop Archive` OR add an empty-tree umbrella commit. DO NOT push.

---

## Self-Review

- **Spec coverage:** Part 2 pipeline → Tasks 1,3,4,5,6. Drop archive → Tasks 2,4,5,6 (is_archived). Data migration sourcing→open + archive-decision flagged → Task 6. Throwaway PG round-trip + staging-untouched → Task 6 (explicit `DATABASE_URL=...55433`, never `.env`). Part 3 Hotlist status → Tasks 1,5; salesperson action → Task 5; Proactive wiring → Task 7; deeper-automation flag → reported (manual send shipped, auto-send flagged). Tests → every task + Task 8. pre-commit/commit/no-push → Task 8.
- **Placeholder scan:** none — all code shown.
- **Type consistency:** `set_hotlist`/`set_archived` signatures consistent across Tasks 3,5,7; `is_archived` consistent across 2,4,5,6,7; `OPEN_PIPELINE` used in 4; ReqStatus values consistent 4,5.
- **Deeper-automation flag:** auto-send (send the offer to the customer with zero clicks on a hotlist hit) is intentionally OUT — we surface the match + one-click send. Flagged for follow-up.
