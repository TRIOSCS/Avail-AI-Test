# SP4 — Account Reclamation & Park Inflows

**For agentic workers / REQUIRED SUB-SKILL: superpowers:subagent-driven-development**

**Goal:** Add three inflows that feed idle CRM accounts into the prospecting pool — manual
park, automatic reactivation of unassigned past customers, and a 90-day hardline auto-sweep
with rep+manager notification. Add an in-app reclaim action to reverse a sweep. All guarded
by idempotency, migration safety, and mocked Graph/scheduler tests.

**Architecture:**
- New columns on `prospect_accounts`: `swept_from_owner_id`, `swept_at`, `parked_by_id`
  (nullable FK integers) → Alembic migration 123 chained on 122_prospect_ai_scores.
- New service: `app/services/prospect_reclamation.py` — all business logic for park,
  auto-surface, sweep, notify, reclaim.
- New helper in `app/services/activity_service.py` —
  `get_last_activity_at(company_id, db)` — returns `datetime | None` using
  `max(ActivityLog.created_at)` filtered by `company_id` (the existing
  `days_since_last_activity()` uses the same query but returns `int | None`; the new
  helper returns the raw timestamp for the sweep comparison and for the email body).
- New jobs in `app/jobs/prospecting_jobs.py` —
  `_job_account_sweep` (daily 01:00) and `_job_auto_surface_reactivation` (daily 02:00).
- Four new config fields in `app/config.py`.
- Two new HTMX endpoints in `app/routers/htmx_views.py` — park + reclaim
  (UI elements require explicit user approval before build; plan marks these clearly).
- Docs: `docs/APP_MAP_INTERACTIONS.md` and `docs/APP_MAP_DATABASE.md` updated.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + HTMX 2.x + Jinja2 + Tailwind CSS 3.x +
APScheduler + Microsoft Graph (`GraphClient` + `/me/sendMail`) + PostgreSQL 16.

---

## Global Constraints

- **Test command:**
  `TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp4-reclamation pytest <file> -q -p no:cacheprovider -o addopts=""`
- **Migration smoke:** NEVER run `alembic upgrade` against the shared `DATABASE_URL`.
  Only run `alembic heads` (read-only) to verify a single head. Test the migration
  upgrade/downgrade path against an in-memory SQLite engine in the test (see Task 1).
- **Email / Graph:** ALL Graph `post_json("/me/sendMail", ...)` calls MUST be mocked in
  tests (`AsyncMock`). Never call real Graph in tests.
- **Scheduler jobs:** Jobs open their own `SessionLocal()`; tests patch
  `app.database.SessionLocal` to return the test session with `close()` disabled
  (mirror `test_jobs_prospecting.py` fixture).
- **Timestamps:** Jobs and services use `datetime.now(timezone.utc)` at call time.
  Tests inject or patch timestamps — never rely on real wall-clock in assertions.
- **HTMX/UI buttons:** Tasks 8 and 9 are UI-only (Park button + Reclaim button). These
  tasks MUST NOT be implemented until the user has explicitly approved the UI placement
  and label copy. Mark them `[APPROVAL GATE]` and stop before implementing.
- **CLAUDE.md rules in force:** `db.get(Model, id)` only; StrEnum constants; Loguru;
  Ruff + mypy; file headers on every new file; no raw strings for status values.
- **Merge/no-rebase:** This branch is pushed; use `git merge origin/main`, not rebase.

---

## File Structure

```
app/
  config.py                             # Task 2 — 4 new fields
  services/
    activity_service.py                 # Task 3 — add get_last_activity_at()
    prospect_reclamation.py             # Tasks 4–7 — new service file
  jobs/
    prospecting_jobs.py                 # Task 4 — register_sweep_jobs() + 2 job fns
    __init__.py                         # Task 4 — import register_sweep_jobs
  models/
    prospect_account.py                 # Task 1 — 3 new columns + relationships
  routers/
    htmx_views.py                       # Tasks 8–9 [APPROVAL GATE]
  templates/
    htmx/partials/customers/
      _company_detail.html (or equivalent)  # Tasks 8–9 [APPROVAL GATE]
alembic/
  versions/123_sp4_park_provenance.py   # Task 1
MIGRATION_NUMBERS_IN_FLIGHT.txt        # Task 1 — claim 123
tests/
  test_sp4_reclamation.py              # Tasks 3–7 unit tests
  test_sp4_jobs.py                     # Task 4 job-delegation tests
docs/
  APP_MAP_DATABASE.md                  # Task 10
  APP_MAP_INTERACTIONS.md              # Task 10
```

---

## Ambiguities Resolved

1. **"Owner" / "unassigned" in the real schema:** `Company.account_owner_id` is the
   canonical ownership field (nullable FK → `users.id`). Unassigned = `account_owner_id IS NULL`.
   The `CustomerSite.owner_id` field is a child-site-level owner and is NOT what SP4 reads;
   the sweep operates on `Company.account_owner_id` only, matching how `claim_prospect` and
   `release_prospect` work in `prospect_claim.py`.

2. **"Last activity" clock for the 90-day sweep:** `ActivityLog` has a `company_id`
   FK and `created_at`. The existing `days_since_last_activity(company_id, db)` already
   uses `max(ActivityLog.created_at)` filtered by `company_id` — this covers ALL event
   types (email, call, note, meeting, quote/RFQ system events, buy-plan updates) because
   all are written through `log_activity()` or `log_email_activity()` / `log_call_activity()`
   which all set `company_id`. Company also has `last_activity_at` (denormalized, updated
   by `_update_last_activity()`), but we will NOT use the denormalized column because it
   only updates on email/call match — quotes and buy-plan events written via `log_activity()`
   set `ActivityLog.company_id` but do NOT call `_update_last_activity()`. The authoritative
   query is `max(ActivityLog.created_at) WHERE company_id = ?` — expose this as
   `get_last_activity_at(company_id, db) -> datetime | None` in `activity_service.py`.

3. **"Past customer" definition for auto-surface:** A company is a past customer if it
   has at least one `Requisition` with `company_id = company.id` OR a `Quote` reached via
   `Quote.customer_site_id → CustomerSite.company_id = company.id`. Use a single EXISTS
   subquery across both (OR). BuyPlan links via `quote_id` which links via `customer_site_id`
   → company; checking Requisition + Quote covers all historical deal presence.

4. **Provenance storage — columns vs enrichment_data JSON:** Use real columns
   (`swept_from_owner_id`, `swept_at`, `parked_by_id`) on `prospect_accounts`. These values
   are queried by the reclaim permission guard (`swept_from_owner_id == user_id`) and by
   idempotency checks (`swept_at IS NOT NULL`); indexing and FK integrity justify columns
   over JSON. Migration 123 adds all three as nullable.

5. **"Park in prospecting" vs existing `send_company_to_prospecting`:** The existing
   `send_company_to_prospecting()` in `prospect_claim.py` uses `discovery_source="sent_back"`.
   SP4's manual park sets `discovery_source="sales_park"` and `parked_by_id`. SP4 adds a
   NEW thin wrapper `park_company_in_prospecting(company_id, user_id, db)` in
   `prospect_reclamation.py` that calls the existing machinery with the SP4 provenance
   fields overlaid after the ProspectAccount is created. Do NOT modify the existing
   `send_company_to_prospecting()` — keep backward compatibility.

6. **Rep token for sweep notification email:** The sweep runs as a system job with no
   user session token. Mirror `ownership_service.py` lines 636-644: call
   `get_valid_token(owner, db)` from `app.scheduler`; if no valid token, log a warning and
   skip the email (do not fail the sweep). CC the configured
   `settings.account_sweep_manager_email` if non-empty, else fallback to
   `settings.admin_emails[0]` if available.

7. **Reclaim permission:** Former owner (`swept_from_owner_id == user_id`) OR admin
   (`user.role == UserRole.ADMIN`) OR any user whose email matches
   `settings.account_sweep_manager_email`. This is checked in `reclaim_prospect_account()`.

---

## Tasks

---

### Task 1 — Migration 123: provenance columns on prospect_accounts

**Files:**
- `app/models/prospect_account.py` — add columns + relationships
- `alembic/versions/123_sp4_park_provenance.py` — new migration
- `MIGRATION_NUMBERS_IN_FLIGHT.txt` — claim 123

**Interfaces (Consumes):**
- `ProspectAccount` (existing) — add after `dismissed_at` block
- Current alembic head: `122_prospect_ai_scores`

**Interfaces (Produces):**
```python
# app/models/prospect_account.py — new columns
swept_from_owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
swept_at = Column(UTCDateTime, nullable=True)
parked_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

# new relationships (after dismissed_by_user)
swept_from_owner = relationship("User", foreign_keys=[swept_from_owner_id])
parked_by_user = relationship("User", foreign_keys=[parked_by_id])
```

**Steps:**
- [ ] Open `app/models/prospect_account.py`. After the `dismissed_by_user` relationship,
  add the three columns and two relationships above.
- [ ] Append to `MIGRATION_NUMBERS_IN_FLIGHT.txt`:
  `123 feat/sp4-account-reclamation park provenance columns (swept_from_owner_id, swept_at, parked_by_id) on prospect_accounts; chains onto 122_prospect_ai_scores`
- [ ] Create `alembic/versions/123_sp4_park_provenance.py` with:
  - `revision = "123_sp4_park_provenance"`, `down_revision = "122_prospect_ai_scores"`
  - upgrade: `op.add_column("prospect_accounts", sa.Column("swept_from_owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))` (×3 columns)
  - downgrade: `op.drop_column("prospect_accounts", col)` for each
- [ ] Write test in `tests/test_sp4_reclamation.py`:
  ```python
  def test_migration_123_upgrade_downgrade():
      """Migration 123 round-trips cleanly on a disposable SQLite engine."""
      # Use alembic config with sqlalchemy.url = sqlite:///... tmp file
      # run upgrade("123_sp4_park_provenance"); assert columns present
      # run downgrade("-1"); assert columns absent
  ```
  - [ ] Run: expect FAIL (file missing)
  - [ ] Write migration file
  - [ ] Run: expect PASS
- [ ] Run `alembic heads` in the worktree — confirm single head output.
- [ ] Commit: `feat(sp4): migration 123 — park provenance columns on prospect_accounts`

---

### Task 2 — Config: four new SP4 settings

**Files:**
- `app/config.py` — add fields after `prospecting_resurface_days`
- `.env.example` — document new vars

**Interfaces (Produces):**
```python
# app/config.py (Settings class, after prospecting_resurface_days: int = 180)
account_sweep_enabled: bool = False
account_sweep_inactivity_days: int = 90
account_sweep_manager_email: str = ""
account_reactivation_sweep_enabled: bool = True
```

**Steps:**
- [ ] Write test in `tests/test_sp4_reclamation.py`:
  ```python
  def test_sp4_config_defaults():
      from app.config import Settings
      s = Settings()
      assert s.account_sweep_enabled is False
      assert s.account_sweep_inactivity_days == 90
      assert s.account_sweep_manager_email == ""
      assert s.account_reactivation_sweep_enabled is True
  ```
  - [ ] Run: expect FAIL
  - [ ] Add the four fields to `Settings` in `app/config.py`
  - [ ] Add to `.env.example`: `ACCOUNT_SWEEP_ENABLED=false`, `ACCOUNT_SWEEP_INACTIVITY_DAYS=90`, `ACCOUNT_SWEEP_MANAGER_EMAIL=`, `ACCOUNT_REACTIVATION_SWEEP_ENABLED=true`
  - [ ] Run: expect PASS
- [ ] Commit: `feat(sp4): add account_sweep_* config settings`

---

### Task 3 — Activity service: get_last_activity_at() helper

**Files:**
- `app/services/activity_service.py` — add after `days_since_last_activity()`

**Interfaces (Consumes):**
```python
# existing, for reference
def days_since_last_activity(company_id: int, db: Session) -> int | None:
    latest = db.query(func.max(ActivityLog.created_at)).filter(ActivityLog.company_id == company_id).scalar()
    ...
```

**Interfaces (Produces):**
```python
def get_last_activity_at(company_id: int, db: Session) -> datetime | None:
    """Return the UTC datetime of the most recent ActivityLog entry for a company.

    None if no activity ever. Covers ALL event types (email, call, note, meeting,
    quote, RFQ, buy-plan updates) because all writers set ActivityLog.company_id.
    Used by the SP4 90-day sweep to determine dormancy.

    Called by: app/services/prospect_reclamation.py
    """
```

**Steps:**
- [ ] Write test in `tests/test_sp4_reclamation.py`:
  ```python
  def test_get_last_activity_at_no_activity(db_session):
      co = _make_company(db_session)
      assert get_last_activity_at(co.id, db_session) is None

  def test_get_last_activity_at_returns_latest(db_session, test_user):
      co = _make_company(db_session)
      t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
      t2 = datetime(2026, 3, 1, tzinfo=timezone.utc)
      for t in (t1, t2):
          db_session.add(ActivityLog(company_id=co.id, activity_type="note",
                                     channel="system", created_at=t))
      db_session.commit()
      result = get_last_activity_at(co.id, db_session)
      assert result == t2
  ```
  - [ ] Run: expect FAIL
  - [ ] Add `get_last_activity_at()` to `activity_service.py`
  - [ ] Run: expect PASS
- [ ] Commit: `feat(sp4): add get_last_activity_at() to activity_service`

---

### Task 4 — Sweep + auto-surface jobs (scheduler wiring)

**Files:**
- `app/jobs/prospecting_jobs.py` — add `register_sweep_jobs()` + 2 job fns
- `app/jobs/__init__.py` — import and call `register_sweep_jobs`
- `app/services/prospect_reclamation.py` — stub `job_account_sweep()` and `job_auto_surface_reactivation()` (full impl in Tasks 5+6; stubs raise NotImplementedError for now — replaced in those tasks)

**Interfaces (Produces):**
```python
# app/jobs/prospecting_jobs.py
def register_sweep_jobs(scheduler, settings):
    """Register SP4 account-sweep + reactivation jobs."""
    if settings.account_sweep_enabled:
        scheduler.add_job(
            _job_account_sweep,
            CronTrigger(hour=1, minute=0),
            id="account_sweep",
            name="90-day account hardline sweep",
        )
    if settings.account_reactivation_sweep_enabled:
        scheduler.add_job(
            _job_auto_surface_reactivation,
            CronTrigger(hour=2, minute=0),
            id="auto_surface_reactivation",
            name="Auto-surface unassigned past customers",
        )

@_traced_job
async def _job_account_sweep():
    """Daily 1AM — sweep dormant owned accounts into prospecting pool."""
    from ..services.prospect_reclamation import job_account_sweep
    await job_account_sweep()

@_traced_job
async def _job_auto_surface_reactivation():
    """Daily 2AM — surface unassigned past customers."""
    from ..services.prospect_reclamation import job_auto_surface_reactivation
    await job_auto_surface_reactivation()
```

**Steps:**
- [ ] Create stub `app/services/prospect_reclamation.py`:
  ```python
  """SP4 account reclamation service — park, sweep, notify, reclaim.

  Called by: app/jobs/prospecting_jobs.py (jobs), app/routers/htmx_views.py (HTMX actions)
  Depends on: app/services/activity_service.py, app/services/prospect_claim.py,
              app/utils/graph_client.py, app/models/prospect_account.py, app/models/crm.py
  """
  async def job_account_sweep(): raise NotImplementedError
  async def job_auto_surface_reactivation(): raise NotImplementedError
  ```
- [ ] Write tests in `tests/test_sp4_jobs.py` (mirror `test_jobs_prospecting.py`):
  ```python
  @pytest.fixture() def scheduler_db(db_session): ...  # same patch pattern
  @pytest.fixture(autouse=True) def _clear_jobs(): ...  # same clear pattern

  def test_sweep_job_registered_when_enabled(scheduler_db):
      from app.config import Settings
      s = Settings(account_sweep_enabled=True)
      from app.jobs.prospecting_jobs import register_sweep_jobs
      register_sweep_jobs(scheduler, s)
      ids = [j.id for j in scheduler.get_jobs()]
      assert "account_sweep" in ids

  def test_sweep_job_not_registered_when_disabled(scheduler_db):
      from app.config import Settings
      s = Settings(account_sweep_enabled=False)
      from app.jobs.prospecting_jobs import register_sweep_jobs
      register_sweep_jobs(scheduler, s)
      assert "account_sweep" not in [j.id for j in scheduler.get_jobs()]

  def test_reactivation_job_registered(scheduler_db): ...  # same pattern

  def test_sweep_job_delegates(scheduler_db):
      mock_fn = AsyncMock()
      with patch("app.services.prospect_reclamation.job_account_sweep", mock_fn):
          from app.jobs.prospecting_jobs import _job_account_sweep
          asyncio.get_event_loop().run_until_complete(_job_account_sweep())
          mock_fn.assert_awaited_once()
  ```
  - [ ] Run: expect FAIL
  - [ ] Add `register_sweep_jobs()` and the two job functions to `prospecting_jobs.py`
  - [ ] Add `from .prospecting_jobs import register_sweep_jobs` + call in `__init__.py`
  - [ ] Run: expect PASS
- [ ] Commit: `feat(sp4): register sweep + reactivation jobs in APScheduler`

---

### Task 5 — 90-day hardline sweep + Graph notification

**Files:**
- `app/services/prospect_reclamation.py` — implement `job_account_sweep()` + helpers

**Interfaces (Consumes):**
```python
# app/services/activity_service.py
def get_last_activity_at(company_id: int, db: Session) -> datetime | None: ...

# app/services/prospect_claim.py
def send_company_to_prospecting(company_id: int, user_id: int, db: Session, *, is_admin: bool = False) -> dict: ...

# app/scheduler.py
async def get_valid_token(user: User, db: Session) -> str | None: ...  # from scheduler module

# app/utils/graph_client.py
class GraphClient:
    def __init__(self, token: str): ...
    async def post_json(self, path: str, payload: dict) -> dict: ...
```

**Interfaces (Produces):**
```python
async def job_account_sweep() -> None:
    """Daily sweep: find owned Companies with last activity > inactivity_days ago.

    For each dormant company:
    - Send to prospecting (discovery_source="auto_sweep") via send_company_to_prospecting
    - Set swept_from_owner_id, swept_at on the resulting ProspectAccount
    - Send loss-notification email (TO: rep, CC: manager) via Graph
    - Idempotent: skip companies whose linked ProspectAccount already has swept_at set

    Called by: app/jobs/prospecting_jobs.py
    """

async def _send_sweep_notification(
    owner: User,
    company: Company,
    last_activity_at: datetime | None,
    prospect_id: int,
    db: Session,
) -> None:
    """Send Graph /me/sendMail loss-notification to owner + CC manager.

    Uses get_valid_token(owner, db). On missing token: log warning, return.
    CC: settings.account_sweep_manager_email if set, else first admin_email.
    """
```

**Steps:**
- [ ] Write tests in `tests/test_sp4_reclamation.py`:
  ```python
  def test_sweep_skips_unowned(db_session):
      """Company with no owner is not swept."""
      co = _make_company(db_session, owner_id=None)
      # plant stale activity (100 days ago)
      _plant_activity(db_session, co.id, days_ago=100)
      asyncio.run(job_account_sweep_with_db(db_session))
      assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0

  def test_sweep_dormant_company(db_session, test_user):
      """Owned company with no activity in 100 days is swept."""
      co = _make_company(db_session, owner_id=test_user.id)
      _plant_activity(db_session, co.id, days_ago=100)
      with patch("app.services.prospect_reclamation._send_sweep_notification", AsyncMock()):
          asyncio.run(job_account_sweep_with_db(db_session))
      pa = db_session.query(ProspectAccount).filter_by(company_id=co.id).first()
      assert pa is not None
      assert pa.discovery_source == "auto_sweep"
      assert pa.swept_from_owner_id == test_user.id
      assert pa.swept_at is not None
      co_fresh = db_session.get(Company, co.id)
      assert co_fresh.account_owner_id is None

  def test_sweep_skips_recent_activity(db_session, test_user):
      """Owned company with activity 10 days ago is NOT swept."""
      co = _make_company(db_session, owner_id=test_user.id)
      _plant_activity(db_session, co.id, days_ago=10)
      with patch("app.services.prospect_reclamation._send_sweep_notification", AsyncMock()):
          asyncio.run(job_account_sweep_with_db(db_session))
      assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0

  def test_sweep_idempotent(db_session, test_user):
      """Running sweep twice does not create duplicate ProspectAccounts."""
      co = _make_company(db_session, owner_id=test_user.id)
      _plant_activity(db_session, co.id, days_ago=100)
      with patch("app.services.prospect_reclamation._send_sweep_notification", AsyncMock()):
          asyncio.run(job_account_sweep_with_db(db_session))
          asyncio.run(job_account_sweep_with_db(db_session))
      assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 1

  def test_sweep_notification_sends_to_cc(db_session, test_user):
      """Notification email is sent TO rep, CC manager; includes last-activity date."""
      mock_gc = AsyncMock()
      mock_gc.post_json = AsyncMock(return_value={})
      last_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
      co = _make_company(db_session, owner_id=test_user.id)
      with patch("app.scheduler.get_valid_token", AsyncMock(return_value="tok")), \
           patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
          asyncio.run(_send_sweep_notification(
              owner=test_user, company=co, last_activity_at=last_dt,
              prospect_id=1, db=db_session
          ))
      mock_gc.post_json.assert_awaited_once()
      call_args = mock_gc.post_json.call_args[0][1]
      recipients = call_args["message"]["toRecipients"]
      assert any(r["emailAddress"]["address"] == test_user.email for r in recipients)
      body = call_args["message"]["body"]["content"]
      assert "2026-01-01" in body  # last-activity date in body

  def test_sweep_notification_skips_on_no_token(db_session, test_user):
      """Missing token logs warning and returns without raising."""
      co = _make_company(db_session, owner_id=test_user.id)
      with patch("app.scheduler.get_valid_token", AsyncMock(return_value=None)):
          # should not raise
          asyncio.run(_send_sweep_notification(
              owner=test_user, company=co, last_activity_at=None,
              prospect_id=1, db=db_session
          ))
  ```
  - [ ] Run: expect FAIL
  - [ ] Implement `job_account_sweep()` and `_send_sweep_notification()` in
    `prospect_reclamation.py`. Key logic:
    - Query: `Company` where `account_owner_id IS NOT NULL` AND
      `max(ActivityLog.created_at) WHERE company_id = company.id < now - inactivity_days`
      (use `get_last_activity_at()` per row, or a single correlated subquery for scale).
    - For each candidate: check if a `ProspectAccount` with `company_id=co.id` and
      `swept_at IS NOT NULL` already exists → skip (idempotency).
    - Call `send_company_to_prospecting(co.id, co.account_owner_id, db, is_admin=True)`
      to clear owner + create/find ProspectAccount.
    - Update the resulting `ProspectAccount`: set `swept_from_owner_id`, `swept_at`,
      change `discovery_source` to `"auto_sweep"`.
    - Call `_send_sweep_notification(owner, company, last_activity_at, prospect_id, db)`.
    - Commit after each company to scope failures.
  - [ ] Run: expect PASS
- [ ] Commit: `feat(sp4): 90-day hardline sweep job with Graph notification`

---

### Task 6 — Auto-surface: unassigned past customers

**Files:**
- `app/services/prospect_reclamation.py` — implement `job_auto_surface_reactivation()`

**Interfaces (Consumes):**
```python
# Requisition.company_id (app/models/sourcing.py, line 48)
# Quote.customer_site_id → CustomerSite.company_id (app/models/quotes.py + crm.py)
# ProspectAccount.company_id (existing)
# send_company_to_prospecting() — reuse but set discovery_source="reactivation" via patch
```

**Interfaces (Produces):**
```python
async def job_auto_surface_reactivation() -> None:
    """Surface unassigned past-customer Companies into the prospecting pool.

    Criteria: Company.account_owner_id IS NULL AND (has Requisition OR has Quote).
    Skip if ProspectAccount already linked (company_id set, status != dismissed).
    Sets discovery_source="reactivation".

    Called by: app/jobs/prospecting_jobs.py
    """
```

**Steps:**
- [ ] Write tests in `tests/test_sp4_reclamation.py`:
  ```python
  def test_reactivation_surfaces_past_customer_with_req(db_session, test_user):
      """Unassigned company with a Requisition gets a ProspectAccount."""
      co = _make_company(db_session, owner_id=None)
      _make_requisition(db_session, company_id=co.id)
      asyncio.run(job_auto_surface_with_db(db_session))
      pa = db_session.query(ProspectAccount).filter_by(company_id=co.id).first()
      assert pa is not None
      assert pa.discovery_source == "reactivation"

  def test_reactivation_skips_owned_company(db_session, test_user):
      """Company with an owner is not auto-surfaced."""
      co = _make_company(db_session, owner_id=test_user.id)
      _make_requisition(db_session, company_id=co.id)
      asyncio.run(job_auto_surface_with_db(db_session))
      assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0

  def test_reactivation_skips_company_already_in_pool(db_session):
      """Company already linked to an active ProspectAccount is not duplicated."""
      co = _make_company(db_session, owner_id=None)
      _make_requisition(db_session, company_id=co.id)
      db_session.add(ProspectAccount(name=co.name, domain="x.com",
                                      discovery_source="reactivation",
                                      status="suggested", fit_score=0,
                                      readiness_score=0, company_id=co.id))
      db_session.commit()
      asyncio.run(job_auto_surface_with_db(db_session))
      assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 1

  def test_reactivation_skips_no_history(db_session):
      """Unassigned company with no quote or requisition is not surfaced."""
      co = _make_company(db_session, owner_id=None)
      asyncio.run(job_auto_surface_with_db(db_session))
      assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0
  ```
  - [ ] Run: expect FAIL
  - [ ] Implement `job_auto_surface_reactivation()`. Use a single query:
    `Company.account_owner_id IS NULL` AND
    `(EXISTS(SELECT 1 FROM requisitions WHERE company_id=c.id) OR
      EXISTS(SELECT 1 FROM quotes q JOIN customer_sites s ON q.customer_site_id=s.id WHERE s.company_id=c.id))`.
    For each: check existing non-dismissed ProspectAccount by `company_id` → skip.
    Else: create `ProspectAccount(discovery_source="reactivation", status="suggested", company_id=co.id, ...)`.
    Set `company.domain` if available; do NOT call `send_company_to_prospecting()` (that
    clears an existing owner — unassigned companies have no owner to clear). Insert directly.
  - [ ] Run: expect PASS
- [ ] Commit: `feat(sp4): auto-surface unassigned past customers as reactivation prospects`

---

### Task 7 — Reclaim action (backend service + endpoint)

**Files:**
- `app/services/prospect_reclamation.py` — implement `reclaim_prospect_account()`
- `app/routers/htmx_views.py` — add `POST /v2/partials/prospects/{prospect_id}/reclaim`

**Interfaces (Consumes):**
```python
# app/services/activity_service.py
def log_activity(db, *, activity_type, channel, company_id, user_id, summary, details) -> ActivityLog: ...

# ProspectAccount.swept_from_owner_id, ProspectAccount.swept_at (Task 1 columns)
```

**Interfaces (Produces):**
```python
def reclaim_prospect_account(
    prospect_id: int,
    user_id: int,
    db: Session,
    *,
    is_admin: bool = False,
    justification: str | None = None,
) -> dict:
    """Reclaim a swept prospect: re-assign Company owner, remove from pool, reset clock.

    Permission: swept_from_owner_id == user_id OR is_admin OR
                user.email == settings.account_sweep_manager_email.
    Actions:
    - Set Company.account_owner_id = user_id; Company.ownership_cleared_at = None
    - Set ProspectAccount.status = ProspectAccountStatus.DISMISSED (removes from pool)
    - Log a "reclaim" ActivityLog entry on the company (resets activity clock)
    Returns: {prospect_id, company_id, company_name, status: "reclaimed"}
    Raises: LookupError (not found), ValueError (permission denied / wrong status)
    """

# Router endpoint (htmx_views.py)
# POST /v2/partials/prospects/{prospect_id}/reclaim
# Requires: require_user (buyer or admin)
# Returns: HTMLResponse (re-renders prospect card with toast)
```

**Steps:**
- [ ] Write tests in `tests/test_sp4_reclamation.py`:
  ```python
  def test_reclaim_by_former_owner(db_session, test_user):
      co = _make_swept_company(db_session, swept_owner=test_user)
      result = reclaim_prospect_account(co["prospect_id"], test_user.id, db_session)
      assert result["status"] == "reclaimed"
      co_fresh = db_session.get(Company, co["company_id"])
      assert co_fresh.account_owner_id == test_user.id
      pa = db_session.get(ProspectAccount, co["prospect_id"])
      assert pa.status == ProspectAccountStatus.DISMISSED

  def test_reclaim_logs_activity(db_session, test_user):
      co = _make_swept_company(db_session, swept_owner=test_user)
      reclaim_prospect_account(co["prospect_id"], test_user.id, db_session)
      log = db_session.query(ActivityLog).filter_by(
          company_id=co["company_id"], activity_type="reclaim"
      ).first()
      assert log is not None

  def test_reclaim_permission_denied_for_stranger(db_session, test_user):
      other = _make_user(db_session)
      co = _make_swept_company(db_session, swept_owner=test_user)
      with pytest.raises(ValueError, match="permission"):
          reclaim_prospect_account(co["prospect_id"], other.id, db_session)

  def test_reclaim_allowed_for_admin(db_session, test_user):
      other_admin = _make_user(db_session, role="admin")
      co = _make_swept_company(db_session, swept_owner=test_user)
      result = reclaim_prospect_account(
          co["prospect_id"], other_admin.id, db_session, is_admin=True
      )
      assert result["status"] == "reclaimed"
  ```
  - [ ] Run: expect FAIL
  - [ ] Implement `reclaim_prospect_account()` in `prospect_reclamation.py`
  - [ ] Add the HTMX endpoint to `htmx_views.py` (following the pattern of
    `send_company_to_prospecting_htmx` at line 5182):
    - `@router.post("/v2/partials/prospects/{prospect_id}/reclaim", response_class=HTMLResponse)`
    - Guard: `require_user`
    - Call: `reclaim_prospect_account(prospect_id, user.id, db, is_admin=(user.role == UserRole.ADMIN))`
    - On success: return existing prospect-card partial with `HX-Trigger showToast`
    - On LookupError → 404; ValueError → 403
  - [ ] Run: expect PASS
- [ ] Commit: `feat(sp4): reclaim_prospect_account() service + HTMX endpoint`

---

### Task 8 — [APPROVAL GATE] UI: "Park in prospecting" button on Company detail

**Files:**
- `app/routers/htmx_views.py` — add `POST /v2/partials/customers/{company_id}/park-in-prospecting`
- `app/services/prospect_reclamation.py` — add `park_company_in_prospecting()`
- CRM company detail template (trace from router to find exact path)

**STOP HERE.** Do NOT implement until the user has approved:
1. Exact placement of the button in the company detail panel.
2. Button label copy.
3. Confirmation modal requirement (yes/no).

**When approved, implement:**

`park_company_in_prospecting(company_id, user_id, db)`:
- Calls `send_company_to_prospecting(company_id, user_id, db)` to clear owner + create
  ProspectAccount.
- Overrides: `pa.discovery_source = "sales_park"`, `pa.parked_by_id = user_id`; commit.
- Returns: `{company_id, company_name, prospect_id, pooled: bool}`

HTMX endpoint: `POST /v2/partials/customers/{company_id}/park-in-prospecting`
- Guard: `require_buyer`
- Returns: re-render of company detail partial + showToast
- Permission: owner or admin (same guard as `send_company_to_prospecting_htmx`)

---

### Task 9 — [APPROVAL GATE] UI: "Reclaim" button on Prospect card

**Files:**
- Prospect card partial (trace from prospecting tab router to find exact template)

**STOP HERE.** Do NOT implement until the user has approved:
1. Where the Reclaim button appears (prospect card, prospect detail modal, or email deep-link).
2. Whether a justification text field is required in the UI.
3. Who can see the button (only swept prospects, or all claimed prospects).

**When approved, implement:**
- Add a `hx-post` button pointing to `POST /v2/partials/prospects/{prospect_id}/reclaim`
  (endpoint built in Task 7).
- Only render the button when `prospect.swept_at IS NOT NULL` (i.e., it was auto-swept).
- Pass `justification` as a form field if approved.
- Bind Alpine confirm dialog if a confirmation step is approved.

---

### Task 10 — Docs: APP_MAP_DATABASE + APP_MAP_INTERACTIONS

**Files:**
- `docs/APP_MAP_DATABASE.md` — document 3 new columns on `prospect_accounts`; 4 new config fields
- `docs/APP_MAP_INTERACTIONS.md` — document sweep job flow, notification email flow, reclaim flow

**Steps:**
- [ ] Open `docs/APP_MAP_DATABASE.md`. In the `prospect_accounts` table section, add:
  `swept_from_owner_id INT FK users | swept_at UTCDateTime | parked_by_id INT FK users`.
  In the config section, add the 4 new `account_sweep_*` fields.
- [ ] Open `docs/APP_MAP_INTERACTIONS.md`. Add a "SP4 Account Reclamation" section describing:
  - Daily 1AM sweep job → `prospect_reclamation.job_account_sweep` → Graph `/me/sendMail`
  - Daily 2AM reactivation job → `prospect_reclamation.job_auto_surface_reactivation`
  - `park_company_in_prospecting` → clears owner, creates ProspectAccount (sales_park)
  - `reclaim_prospect_account` → re-assigns owner, dismisses ProspectAccount, logs activity
- [ ] Commit: `docs(sp4): update APP_MAP_DATABASE + APP_MAP_INTERACTIONS for SP4`

---

## Build Order

```
Task 1 (migration + model columns)
  → Task 2 (config)
    → Task 3 (activity helper)
      → Task 4 (job wiring stubs)
        → Task 5 (sweep job + notification)
          → Task 6 (auto-surface job)
            → Task 7 (reclaim backend + endpoint)
              → Task 8 [APPROVAL GATE] (park UI)
              → Task 9 [APPROVAL GATE] (reclaim UI)
              → Task 10 (docs)
```

Tasks 8 and 9 are parallel after Task 7 once approved. Task 10 can be done after Task 7
even if 8+9 are not yet approved (document the backend interfaces).
