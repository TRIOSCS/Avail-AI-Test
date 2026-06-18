# CRM Cadence Data Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the two-clock cadence engine (last-outbound + last-meaningful-reply) and account tiering to the CRM data layer, so every account/site/contact has honest, sortable "days since contact" signals.

**Architecture:** The `ActivityLog` event table (which already carries `direction` and `is_meaningful`) stays the source of truth. We add **materialized clock columns** (`last_outbound_at`, `last_reply_at`) on Company / CustomerSite / SiteContact / VendorCard / VendorContact, kept fresh two ways: a real-time forward-only "bump" at activity write-time, and a nightly recompute job as the self-healing backstop. A pure `cadence_state()` function maps `(tier, last_outbound_at)` to green/amber/red against per-tier targets inside a universal 30-day ceiling.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 (DeclarativeBase, classic `Column()` style), Alembic, APScheduler, PostgreSQL (prod) / in-memory SQLite (tests), pytest + xdist.

## Global Constraints

- **DB model style:** SQLAlchemy 2.0 classic `Column(...)` (NOT `Mapped[]`). Base = `app.models.base.Base`. Timestamps use `UTCDateTime` from `app.database`.
- **Alembic revision id ≤ 32 chars** (PG `alembic_version.version_num` is `VARCHAR(32)`; SQLite won't catch overflow). Use `108_crm_cadence_clocks` (22 chars).
- **Migration coordination:** claim the number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` in the SAME commit. Lowest free 3-digit ≥ existing is **108** — verify it's still free at execution time (`ls alembic/versions/ | sort`); if a concurrent worktree took it, use the next free number and update the revision id + claim line to match.
- **Tests run on in-memory SQLite** (`pytest`, parallel via `-n auto`). Model columns auto-create from `Base.metadata`, so model edits are testable immediately; the Alembic migration is for real PG and is exercised by the migration-chain test + live-PG verification, not unit tests.
- **Forward-only clocks:** a clock may only advance, never move backward (prevents one writer clobbering another's newer timestamp).
- **Cadence targets (verbatim):** `key=7d`, `core=14d`, `standard=30d`, `prospect=30d`; **universal red ceiling = 30d for every tier**; NULL clock = state `"new"`, sorted as most-overdue.
- **Tier values:** `"key" | "core" | "standard" | "prospect"`; NULL tier is treated as `"standard"`.
- **Non-breaking:** keep the existing `staleness_tier()` and `last_activity_at` behavior intact (the UI swaps over in Plan 3). This plan only ADDS.

---

### Task 1: Migration + model columns (clocks + tier)

**Files:**
- Modify: `app/models/crm.py` (Company, CustomerSite, SiteContact)
- Modify: `app/models/vendor.py` (VendorCard, VendorContact) — confirm path via `grep -rn "class VendorCard" app/models/`
- Create: `alembic/versions/108_crm_cadence_clocks.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`
- Test: `tests/test_crm_cadence_model.py`

**Interfaces:**
- Produces: new attributes on the five models — `last_outbound_at: datetime|None`, `last_reply_at: datetime|None` (all five); `last_activity_at: datetime|None` on SiteContact; `tier: str|None` on Company. Later tasks read/write these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crm_cadence_model.py
from datetime import datetime, timezone

from app.models.crm import Company, CustomerSite, SiteContact


def test_clock_and_tier_columns_persist(db_session):
    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    co = Company(name="Clock Co", tier="key", last_outbound_at=now, last_reply_at=now)
    db_session.add(co)
    db_session.commit()
    site = CustomerSite(company_id=co.id, site_name="HQ", last_outbound_at=now, last_reply_at=now)
    db_session.add(site)
    db_session.commit()
    contact = SiteContact(
        customer_site_id=site.id, full_name="Pat Buyer",
        last_activity_at=now, last_outbound_at=now, last_reply_at=now,
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(co)
    db_session.refresh(contact)
    assert co.tier == "key"
    assert co.last_outbound_at == now and co.last_reply_at == now
    assert contact.last_activity_at == now and contact.last_outbound_at == now
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_crm_cadence_model.py -v`
Expected: FAIL — `TypeError: 'tier' is an invalid keyword argument for Company` (column not yet on model).

- [ ] **Step 3: Add columns to the models**

In `app/models/crm.py`, in `Company` (after the `last_activity_at = Column(UTCDateTime, index=True)` line):

```python
    # CRM cadence — two clocks + tier (see docs/.../2026-06-17-crm-data-foundation.md)
    last_outbound_at = Column(UTCDateTime, index=True)
    last_reply_at = Column(UTCDateTime, index=True)
    tier = Column(String(20), index=True)  # key | core | standard | prospect (NULL => standard)
```

In `CustomerSite` (after its `last_activity_at = Column(UTCDateTime)` line):

```python
    last_outbound_at = Column(UTCDateTime)
    last_reply_at = Column(UTCDateTime)
```

In `SiteContact` (after `contact_status = Column(String(20), default="new")`):

```python
    # CRM cadence — contact-level clocks
    last_activity_at = Column(UTCDateTime)
    last_outbound_at = Column(UTCDateTime)
    last_reply_at = Column(UTCDateTime)
```

In `VendorCard` and `VendorContact` (mirror, for symmetric vendor cadence in later plans) add:

```python
    last_outbound_at = Column(UTCDateTime)
    last_reply_at = Column(UTCDateTime)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_crm_cadence_model.py -v`
Expected: PASS.

- [ ] **Step 5: Write the Alembic migration**

```python
# alembic/versions/108_crm_cadence_clocks.py
"""CRM cadence: two-clock columns (last_outbound_at/last_reply_at) + account tier.

What: adds last_outbound_at + last_reply_at to companies, customer_sites,
      site_contacts, vendor_cards, vendor_contacts; adds last_activity_at to
      site_contacts; adds tier to companies. Indexes the company-level clocks
      and tier (left-list sort/filter).
Downgrade: drops the added columns/indexes (reversible).
"""

import sqlalchemy as sa
from alembic import op

revision = "108_crm_cadence_clocks"
down_revision = "107_is_scratch_requisitions"
branch_labels = None
depends_on = None

_CLOCKS = ("last_outbound_at", "last_reply_at")


def upgrade() -> None:
    for col in _CLOCKS:
        op.add_column("companies", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("customer_sites", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("site_contacts", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("vendor_cards", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
        op.add_column("vendor_contacts", sa.Column(col, sa.DateTime(timezone=True), nullable=True))
    op.add_column("site_contacts", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("tier", sa.String(length=20), nullable=True))
    op.create_index("ix_companies_last_outbound_at", "companies", ["last_outbound_at"])
    op.create_index("ix_companies_last_reply_at", "companies", ["last_reply_at"])
    op.create_index("ix_companies_tier", "companies", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_companies_tier", table_name="companies")
    op.drop_index("ix_companies_last_reply_at", table_name="companies")
    op.drop_index("ix_companies_last_outbound_at", table_name="companies")
    op.drop_column("companies", "tier")
    op.drop_column("site_contacts", "last_activity_at")
    for col in _CLOCKS:
        op.drop_column("vendor_contacts", col)
        op.drop_column("vendor_cards", col)
        op.drop_column("site_contacts", col)
        op.drop_column("customer_sites", col)
        op.drop_column("companies", col)
```

- [ ] **Step 6: Claim the migration number**

Append to `MIGRATION_NUMBERS_IN_FLIGHT.txt` (after the `107` line):

```
108 worktree-crm-redesign-cockpit crm cadence two-clock columns + account tier
```

- [ ] **Step 7: Verify the migration chain + full model test**

Run: `pytest tests/test_crm_cadence_model.py tests/test_migration_chain.py tests/test_migration_numbers_in_flight.py -v`
Expected: PASS (chain links 108→107; claim line present).

- [ ] **Step 8: Commit**

```bash
git add app/models/crm.py app/models/vendor.py alembic/versions/108_crm_cadence_clocks.py MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_crm_cadence_model.py
git commit -m "feat(crm): add two-clock + tier columns (migration 108)"
```

---

### Task 2: `cadence_state()` — pure tier→state logic

**Files:**
- Modify: `app/services/crm_service.py`
- Test: `tests/test_cadence_state.py`

**Interfaces:**
- Produces: `TIER_TARGET_DAYS: dict[str, int]`, `CADENCE_RED_DAYS: int = 30`, and `cadence_state(tier: str | None, last_outbound_at: datetime | None, now: datetime | None = None) -> str` returning one of `"new" | "on_target" | "due" | "overdue"`. Consumed by Tasks 5–6 and Plan 3's UI.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cadence_state.py
from datetime import datetime, timedelta, timezone

from app.services.crm_service import cadence_state

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _ago(days):
    return NOW - timedelta(days=days)


def test_never_contacted_is_new():
    assert cadence_state("key", None, now=NOW) == "new"


def test_key_green_amber_red():
    assert cadence_state("key", _ago(3), now=NOW) == "on_target"   # <=7
    assert cadence_state("key", _ago(10), now=NOW) == "due"        # 8..30
    assert cadence_state("key", _ago(31), now=NOW) == "overdue"    # >30


def test_standard_has_no_amber_band_then_red():
    assert cadence_state("standard", _ago(20), now=NOW) == "on_target"  # <=30
    assert cadence_state("standard", _ago(31), now=NOW) == "overdue"    # >30


def test_null_tier_defaults_to_standard():
    assert cadence_state(None, _ago(20), now=NOW) == "on_target"
    assert cadence_state(None, _ago(31), now=NOW) == "overdue"


def test_naive_datetime_is_treated_as_utc():
    assert cadence_state("core", _ago(20).replace(tzinfo=None), now=NOW) == "due"  # >14, <=30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cadence_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'cadence_state'`.

- [ ] **Step 3: Implement `cadence_state`**

Add to `app/services/crm_service.py` (near the existing `staleness_tier`, keeping that function unchanged):

```python
TIER_TARGET_DAYS = {"key": 7, "core": 14, "standard": 30, "prospect": 30}
CADENCE_RED_DAYS = 30  # universal ceiling — every tier goes overdue past this


def cadence_state(tier: str | None, last_outbound_at: datetime | None, now: datetime | None = None) -> str:
    """Cadence state from the OUTBOUND clock against the account's tier target.

    Returns "new" (never touched), "on_target" (<= tier target), "due"
    (past target, <= 30d), or "overdue" (> 30d, for every tier).
    """
    if last_outbound_at is None:
        return "new"
    now = now or datetime.now(timezone.utc)
    ts = last_outbound_at if last_outbound_at.tzinfo else last_outbound_at.replace(tzinfo=timezone.utc)
    days = (now - ts).days
    if days > CADENCE_RED_DAYS:
        return "overdue"
    target = TIER_TARGET_DAYS.get(tier or "standard", TIER_TARGET_DAYS["standard"])
    if days > target:
        return "due"
    return "on_target"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cadence_state.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/crm_service.py tests/test_cadence_state.py
git commit -m "feat(crm): cadence_state() tier-aware two-clock state"
```

---

### Task 3: Materialize clocks from ActivityLog

**Files:**
- Create: `app/services/cadence_service.py`
- Test: `tests/test_cadence_materialize.py`

**Interfaces:**
- Consumes: `ActivityLog` (`app.models.intelligence`), `Direction` (`app.constants`).
- Produces: `materialize_company_clocks(db, company_id: int) -> None` (recomputes `last_outbound_at`/`last_reply_at` for the company AND each of its sites/contacts) and `materialize_all_clocks(db) -> int` (recompute every company; returns count). Consumed by Task 5 (job/backfill).

Definition of each clock from the event log:
- `last_outbound_at` = max(`ActivityLog.created_at`) where `direction == "outbound"`.
- `last_reply_at` = max(`ActivityLog.created_at`) where `direction == "inbound"` AND `is_meaningful is True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cadence_materialize.py
from datetime import datetime, timedelta, timezone

from app.constants import ActivityType, Channel, Direction
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.intelligence import ActivityLog
from app.services.cadence_service import materialize_company_clocks

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _log(db, *, company_id, site_contact_id=None, customer_site_id=None, direction, meaningful, created):
    a = ActivityLog(
        activity_type=ActivityType.EMAIL_RECEIVED, channel=Channel.EMAIL,
        company_id=company_id, customer_site_id=customer_site_id, site_contact_id=site_contact_id,
        direction=direction, is_meaningful=meaningful, created_at=created, occurred_at=created,
    )
    db.add(a)
    db.flush()
    return a


def test_materialize_sets_outbound_and_meaningful_reply(db_session):
    co = Company(name="Mat Co")
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(customer_site_id=site.id, full_name="Reply Person")
    db_session.add(contact)
    db_session.flush()

    _log(db_session, company_id=co.id, customer_site_id=site.id, site_contact_id=contact.id,
         direction=Direction.OUTBOUND, meaningful=None, created=NOW - timedelta(days=5))
    _log(db_session, company_id=co.id, customer_site_id=site.id, site_contact_id=contact.id,
         direction=Direction.INBOUND, meaningful=True, created=NOW - timedelta(days=2))
    # noise: inbound but NOT meaningful — must NOT set the reply clock
    _log(db_session, company_id=co.id, customer_site_id=site.id, site_contact_id=contact.id,
         direction=Direction.INBOUND, meaningful=False, created=NOW)
    db_session.commit()

    materialize_company_clocks(db_session, co.id)
    db_session.commit()
    db_session.refresh(co)
    db_session.refresh(contact)

    assert co.last_outbound_at == NOW - timedelta(days=5)
    assert co.last_reply_at == NOW - timedelta(days=2)      # noise ignored
    assert contact.last_outbound_at == NOW - timedelta(days=5)
    assert contact.last_reply_at == NOW - timedelta(days=2)


def test_materialize_leaves_clocks_null_when_no_activity(db_session):
    co = Company(name="Quiet Co")
    db_session.add(co)
    db_session.commit()
    materialize_company_clocks(db_session, co.id)
    db_session.commit()
    db_session.refresh(co)
    assert co.last_outbound_at is None and co.last_reply_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cadence_materialize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.cadence_service'`.

- [ ] **Step 3: Implement the materializer**

```python
# app/services/cadence_service.py
"""CRM cadence clocks: derive last_outbound_at / last_reply_at from ActivityLog.

The ActivityLog event table is the source of truth; the clock columns on
Company/CustomerSite/SiteContact (and the vendor mirrors) are a materialized
cache kept fresh by bump_clocks_from_activity() (real-time) and these
functions (nightly self-healing backstop).
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..constants import Direction
from ..models.crm import Company, CustomerSite, SiteContact
from ..models.intelligence import ActivityLog


def _outbound_max(db: Session, col):
    return db.query(func.max(ActivityLog.created_at)).filter(col, ActivityLog.direction == Direction.OUTBOUND)


def _reply_max(db: Session, col):
    return db.query(func.max(ActivityLog.created_at)).filter(
        col, ActivityLog.direction == Direction.INBOUND, ActivityLog.is_meaningful.is_(True)
    )


def materialize_company_clocks(db: Session, company_id: int) -> None:
    """Recompute both clocks for a company and each of its sites + contacts."""
    db.query(Company).filter(Company.id == company_id).update(
        {
            "last_outbound_at": _outbound_max(db, ActivityLog.company_id == company_id).scalar_subquery(),
            "last_reply_at": _reply_max(db, ActivityLog.company_id == company_id).scalar_subquery(),
        },
        synchronize_session=False,
    )
    for site in db.query(CustomerSite).filter(CustomerSite.company_id == company_id).all():
        db.query(CustomerSite).filter(CustomerSite.id == site.id).update(
            {
                "last_outbound_at": _outbound_max(db, ActivityLog.customer_site_id == site.id).scalar_subquery(),
                "last_reply_at": _reply_max(db, ActivityLog.customer_site_id == site.id).scalar_subquery(),
            },
            synchronize_session=False,
        )
        for contact in db.query(SiteContact).filter(SiteContact.customer_site_id == site.id).all():
            db.query(SiteContact).filter(SiteContact.id == contact.id).update(
                {
                    "last_outbound_at": _outbound_max(db, ActivityLog.site_contact_id == contact.id).scalar_subquery(),
                    "last_reply_at": _reply_max(db, ActivityLog.site_contact_id == contact.id).scalar_subquery(),
                },
                synchronize_session=False,
            )


def materialize_all_clocks(db: Session) -> int:
    """Recompute clocks for every company. Returns number of companies processed."""
    ids = [row[0] for row in db.query(Company.id).all()]
    for cid in ids:
        materialize_company_clocks(db, cid)
    return len(ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cadence_materialize.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/cadence_service.py tests/test_cadence_materialize.py
git commit -m "feat(crm): materialize two-clock cache from ActivityLog"
```

---

### Task 4: Real-time forward-only clock bump at write time

**Files:**
- Modify: `app/services/cadence_service.py`
- Modify: `app/services/activity_service.py` (call sites in the ActivityLog writers)
- Test: `tests/test_cadence_bump.py`

**Interfaces:**
- Consumes: an `ActivityLog` instance (already flushed, so its `id`/FKs/`created_at` are set), `Direction`.
- Produces: `bump_clocks_from_activity(db: Session, activity: ActivityLog) -> None`. Called by the activity writers; advances the relevant entity clock forward only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cadence_bump.py
from datetime import datetime, timedelta, timezone

from app.constants import ActivityType, Channel, Direction
from app.models.crm import Company
from app.models.intelligence import ActivityLog
from app.services.cadence_service import bump_clocks_from_activity

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _mk(db, co, *, direction, meaningful, created):
    a = ActivityLog(
        activity_type=ActivityType.EMAIL_RECEIVED, channel=Channel.EMAIL, company_id=co.id,
        direction=direction, is_meaningful=meaningful, created_at=created, occurred_at=created,
    )
    db.add(a)
    db.flush()
    return a


def test_outbound_sets_outbound_clock(db_session):
    co = Company(name="Bump Co")
    db_session.add(co)
    db_session.flush()
    bump_clocks_from_activity(db_session, _mk(db_session, co, direction=Direction.OUTBOUND, meaningful=None, created=NOW))
    db_session.refresh(co)
    assert co.last_outbound_at == NOW and co.last_reply_at is None


def test_meaningful_inbound_sets_reply_clock(db_session):
    co = Company(name="Bump Co2")
    db_session.add(co)
    db_session.flush()
    bump_clocks_from_activity(db_session, _mk(db_session, co, direction=Direction.INBOUND, meaningful=True, created=NOW))
    db_session.refresh(co)
    assert co.last_reply_at == NOW and co.last_outbound_at is None


def test_noise_inbound_does_not_set_reply_clock(db_session):
    co = Company(name="Bump Co3")
    db_session.add(co)
    db_session.flush()
    bump_clocks_from_activity(db_session, _mk(db_session, co, direction=Direction.INBOUND, meaningful=False, created=NOW))
    db_session.refresh(co)
    assert co.last_reply_at is None


def test_clock_only_advances_forward(db_session):
    co = Company(name="Bump Co4", last_outbound_at=NOW)
    db_session.add(co)
    db_session.flush()
    # older outbound must NOT move the clock backward
    bump_clocks_from_activity(
        db_session, _mk(db_session, co, direction=Direction.OUTBOUND, meaningful=None, created=NOW - timedelta(days=3))
    )
    db_session.refresh(co)
    assert co.last_outbound_at == NOW
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cadence_bump.py -v`
Expected: FAIL — `ImportError: cannot import name 'bump_clocks_from_activity'`.

- [ ] **Step 3: Implement the bump**

Add to `app/services/cadence_service.py`:

```python
from datetime import datetime, timezone

from sqlalchemy import or_

from ..models.vendor import VendorCard, VendorContact  # confirm path via grep

_CLOCK_TARGETS = (
    (Company, "company_id"),
    (CustomerSite, "customer_site_id"),
    (SiteContact, "site_contact_id"),
    (VendorCard, "vendor_card_id"),
    (VendorContact, "vendor_contact_id"),
)


def _advance(db: Session, model, entity_id, field: str, when: datetime) -> None:
    if not entity_id:
        return
    col = getattr(model, field)
    db.query(model).filter(model.id == entity_id, or_(col.is_(None), col < when)).update(
        {field: when}, synchronize_session=False
    )


def bump_clocks_from_activity(db: Session, activity: ActivityLog) -> None:
    """Forward-only clock update from a freshly-written ActivityLog row.

    Outbound advances last_outbound_at; meaningful inbound advances last_reply_at.
    Non-meaningful inbound and NULL direction are ignored (timeline-only noise).
    """
    if activity.direction == Direction.OUTBOUND:
        field = "last_outbound_at"
    elif activity.direction == Direction.INBOUND and activity.is_meaningful:
        field = "last_reply_at"
    else:
        return
    when = activity.created_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    for model, fk in _CLOCK_TARGETS:
        _advance(db, model, getattr(activity, fk), field, when)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cadence_bump.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 5: Wire the bump into the ActivityLog writers**

In `app/services/activity_service.py`, find every place an `ActivityLog(...)` row is created and flushed (start from the known ones: `log_activity` ~L959 `db.flush()`, `log_company_call` ~L561 `db.flush()`; then `grep -n "ActivityLog(" app/services/activity_service.py` to find the email/call writers `log_email_activity` / `log_call_activity`). Immediately after each `db.flush()` that follows an `ActivityLog(...)` construction, add:

```python
    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)  # use the local variable holding the new ActivityLog
```

(The nightly job in Task 5 is the backstop for any writer missed here.)

- [ ] **Step 6: Write an integration test through a public writer**

```python
# append to tests/test_cadence_bump.py
from app.services.activity_service import log_company_call


def test_log_company_call_advances_outbound_clock(db_session):
    co = Company(name="Call Co")
    db_session.add(co)
    db_session.flush()
    log_company_call(
        user_id=None, company_id=co.id, direction="outbound", phone="+15551234567",
        duration_seconds=120, contact_name="Buyer", notes="left details", db=db_session,
    )
    db_session.refresh(co)
    assert co.last_outbound_at is not None
```

- [ ] **Step 7: Run the writer integration test + full activity-service suite**

Run: `pytest tests/test_cadence_bump.py tests/test_activity_service.py -v`
Expected: PASS (no regressions in existing activity tests).

- [ ] **Step 8: Commit**

```bash
git add app/services/cadence_service.py app/services/activity_service.py tests/test_cadence_bump.py
git commit -m "feat(crm): real-time forward-only clock bump on activity write"
```

---

### Task 5: Nightly materialization job + one-time backfill

**Files:**
- Create: `app/jobs/cadence_jobs.py`
- Modify: `app/jobs/__init__.py` (register)
- Create: `app/management/backfill_cadence_clocks.py`
- Test: `tests/test_cadence_jobs.py`

**Interfaces:**
- Consumes: `materialize_all_clocks` (Task 3), the scheduler + `_traced_job` pattern.
- Produces: `register_cadence_jobs(scheduler, settings)` and `backfill_cadence_clocks() -> int` (CLI entry). Job id `cadence_materialize`, `CronTrigger(hour=4, minute=0)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cadence_jobs.py
from datetime import datetime, timedelta, timezone

from app.constants import ActivityType, Channel, Direction
from app.models.crm import Company
from app.models.intelligence import ActivityLog
from app.management.backfill_cadence_clocks import backfill_for_session

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def test_backfill_populates_existing_companies(db_session):
    co = Company(name="Backfill Co")  # clock columns start NULL
    db_session.add(co)
    db_session.flush()
    db_session.add(ActivityLog(
        activity_type=ActivityType.RFQ_SENT, channel=Channel.EMAIL, company_id=co.id,
        direction=Direction.OUTBOUND, created_at=NOW - timedelta(days=4), occurred_at=NOW - timedelta(days=4),
    ))
    db_session.commit()

    count = backfill_for_session(db_session)
    db_session.commit()
    db_session.refresh(co)
    assert count == 1
    assert co.last_outbound_at == NOW - timedelta(days=4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cadence_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.management.backfill_cadence_clocks'`.

- [ ] **Step 3: Implement the backfill (session-level, testable) + CLI wrapper**

```python
# app/management/backfill_cadence_clocks.py
"""One-time backfill of CRM cadence clocks from historical ActivityLog.

Also seeds tier='key' for accounts already flagged is_strategic (idempotent).
Run: python -m app.management.backfill_cadence_clocks
"""

from sqlalchemy.orm import Session

from ..models.crm import Company
from ..services.cadence_service import materialize_all_clocks


def backfill_for_session(db: Session) -> int:
    db.query(Company).filter(Company.is_strategic.is_(True), Company.tier.is_(None)).update(
        {"tier": "key"}, synchronize_session=False
    )
    return materialize_all_clocks(db)


def backfill_cadence_clocks() -> int:
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        n = backfill_for_session(db)
        db.commit()
        return n
    finally:
        db.close()


if __name__ == "__main__":
    print(f"Backfilled cadence clocks for {backfill_cadence_clocks()} companies")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cadence_jobs.py -v`
Expected: PASS.

- [ ] **Step 5: Implement + register the nightly job**

```python
# app/jobs/cadence_jobs.py
"""Nightly CRM cadence clock recompute — self-healing backstop for the
real-time bump (bump_clocks_from_activity)."""

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_cadence_jobs(scheduler, settings):
    scheduler.add_job(
        _job_materialize_cadence,
        CronTrigger(hour=4, minute=0),
        id="cadence_materialize",
        name="Nightly CRM cadence clock recompute",
    )


@_traced_job
async def _job_materialize_cadence():
    from ..database import SessionLocal
    from ..services.cadence_service import materialize_all_clocks

    db = SessionLocal()
    try:
        n = materialize_all_clocks(db)
        db.commit()
        logger.info(f"Cadence job: recomputed clocks for {n} companies")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

In `app/jobs/__init__.py`, inside `register_all_jobs`, add the import alongside the others and call it (mirror the existing pattern):

```python
    from .cadence_jobs import register_cadence_jobs
    ...
    register_cadence_jobs(scheduler, settings)
```

- [ ] **Step 6: Verify registration test**

```python
# append to tests/test_cadence_jobs.py
def test_cadence_job_registered():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.jobs.cadence_jobs import register_cadence_jobs

    sched = AsyncIOScheduler()
    register_cadence_jobs(sched, settings=None)
    assert sched.get_job("cadence_materialize") is not None
```

Run: `pytest tests/test_cadence_jobs.py -v`
Expected: PASS (all tests).

- [ ] **Step 7: Commit**

```bash
git add app/jobs/cadence_jobs.py app/jobs/__init__.py app/management/backfill_cadence_clocks.py tests/test_cadence_jobs.py
git commit -m "feat(crm): nightly cadence recompute job + one-time backfill"
```

---

### Task 6: Sort/query helper for the call-list (clocks + state)

**Files:**
- Modify: `app/services/crm_service.py`
- Test: `tests/test_cadence_sort.py`

**Interfaces:**
- Consumes: `cadence_state` (Task 2), Company clock columns.
- Produces: `order_by_clock(query, clock: str, now: datetime | None = None)` where `clock in {"outbound", "reply"}` — orders so NULL clocks (never-contacted) sort FIRST (most overdue), then oldest→newest. Plan 3's list route consumes this.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cadence_sort.py
from datetime import datetime, timedelta, timezone

from app.models.crm import Company
from app.services.crm_service import order_by_clock

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def test_null_clock_sorts_first_then_oldest(db_session):
    recent = Company(name="Recent", last_outbound_at=NOW - timedelta(days=1))
    old = Company(name="Old", last_outbound_at=NOW - timedelta(days=20))
    never = Company(name="Never")  # NULL clock
    db_session.add_all([recent, old, never])
    db_session.commit()

    rows = order_by_clock(db_session.query(Company), "outbound").all()
    assert [c.name for c in rows] == ["Never", "Old", "Recent"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cadence_sort.py -v`
Expected: FAIL — `ImportError: cannot import name 'order_by_clock'`.

- [ ] **Step 3: Implement the sort helper**

Add to `app/services/crm_service.py`:

```python
from app.models.crm import Company  # if not already imported at top

_CLOCK_COLUMN = {"outbound": Company.last_outbound_at, "reply": Company.last_reply_at}


def order_by_clock(query, clock: str, now=None):
    """Order companies stalest-first: NULL clocks (never contacted) first, then oldest.

    NULLs-first is portable across SQLite (tests) and PostgreSQL (prod) by
    ordering on the IS-NULL flag before the timestamp.
    """
    col = _CLOCK_COLUMN[clock]
    return query.order_by(col.isnot(None), col.asc())
```

(Ordering by `col.isnot(None)` puts `False` (NULL rows) before `True`, then `col.asc()` puts oldest non-NULL next — portable, no `NULLS FIRST` needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cadence_sort.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/crm_service.py tests/test_cadence_sort.py
git commit -m "feat(crm): stalest-first clock sort helper (NULLs first, portable)"
```

---

### Task 7: Full-suite regression + live-PG verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full unit suite (parallel)**

Run: `pytest`
Expected: PASS, no regressions. (xdist parallel catches cross-test state issues unit isolation misses.)

- [ ] **Step 2: Apply the migration on a real PostgreSQL instance and verify**

Run (against a PG dev/staging DB, NOT SQLite):
```bash
alembic upgrade head
alembic downgrade -1   # verify reversibility
alembic upgrade head
```
Expected: clean up/down/up; `\d companies` shows `tier`, `last_outbound_at`, `last_reply_at` + indexes. (SQLite unit tests cannot catch PG-specific issues — verify here per project practice.)

- [ ] **Step 3: Run the backfill on the PG instance and spot-check**

Run: `python -m app.management.backfill_cadence_clocks`
Expected: prints a company count; spot-check a known-active account has a non-NULL `last_outbound_at` matching its newest outbound ActivityLog row, and `is_strategic` accounts now have `tier='key'`.

- [ ] **Step 4: Commit any verification fixes, then mark the plan complete.**

---

## Self-Review (plan author)

**Spec coverage (§ refers to the design spec):**
- §8 two-clock columns + SiteContact.last_activity_at + tier → Task 1 ✅
- §5 cadence tiers + universal 30-day backstop + NULL="new" → Task 2 ✅
- §8 materialization from ActivityLog + idempotent backfill → Tasks 3, 5 ✅
- §6/§8 direction-aware write paths + clobber-safety (forward-only) → Task 4 ✅
- §8 NULL-clock sorted as most-overdue (portable) → Task 6 ✅
- §10 full xdist suite + live-PG verification → Task 7 ✅
- **Deferred to later plans (noted in spec §11):** tags/segmentation, `do_not_contact`, `contact_role` extension, `is_key` → Plan 4 (governance/functions); UI swap from `staleness_tier` to `cadence_state` → Plan 3; AI grading of call/Teams (so non-email channels feed the reply clock) → Plan 2. These are intentionally out of this plan's scope.

**Placeholder scan:** no TBD/TODO; every code step shows complete code. The two grep-to-confirm notes (`app/models/vendor.py` path; the activity-writer call sites) are concrete mechanical instructions with the exact line to add, not vague directives.

**Type consistency:** `cadence_state(tier, last_outbound_at, now)` signature is consistent across Tasks 2/6; clock field names `last_outbound_at`/`last_reply_at` and `Direction.OUTBOUND`/`Direction.INBOUND` match the verified model/enum; `materialize_company_clocks`/`materialize_all_clocks`/`bump_clocks_from_activity`/`order_by_clock` names are stable across tasks and the Interfaces blocks.

**Note for executor:** Task 4 Step 5 depends on the exact set of ActivityLog writers in `activity_service.py`; confirm via grep before editing. The nightly job (Task 5) is the safety net if a writer is missed.
