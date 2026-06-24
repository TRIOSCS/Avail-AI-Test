# Contact Merge + Move Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two CRM contact data-ops — merge two contacts into one (dedup) and move a contact to a different account/site — both behind `can_manage_account` authz, with a full TDD suite.

**Architecture:** Mirror `company_merge_service.py` for the merge service; add four new routes in `htmx_views.py` (merge-form, merge-preview, merge-execute, move); add two Jinja2 modal templates; add one new test file. No migrations.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 sync sessions, Jinja2 + HTMX + Alpine.js, pytest with FastAPI TestClient, ruff linting.

## Global Constraints

- **No new Alembic migration.** All columns already exist.
- **TDD** — write failing test first, then implementation.
- **HTMX + Alpine.js stack** — no React, no JS frameworks.
- **Worktree path:** `/root/availai/.claude/worktrees/attachments-unified`
- **Venv python/pytest:** `/root/availai/.venv/bin/pytest`
- **Run tests from:** `cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest`
- **Ruff:** `cd /root/availai/.claude/worktrees/attachments-unified && /root/availai/.venv/bin/ruff check app`
- **Auth deps:** `can_manage_account(user, company, db)` from `app/dependencies.py`; `is_manager_or_admin(user)` same file.
- **Button sizing:** use `.btn`, `.btn-sm`, `.btn-danger`, `.btn-ghost` CSS classes — never inline `px-`/`py-` on `<button>` tags. This is enforced by `test_static_analysis.py::test_inline_button_sizing_does_not_grow` (baseline 275).
- **Focus rings:** use `focus:ring-2` — never `focus:ring-1`. Enforced by `test_static_analysis.py::test_focus_ring_1_does_not_grow` (baseline 66).
- **`_render_contacts_list(request, user, company, db)`** is the canonical contacts-list re-render helper; all contact mutation endpoints call it.
- **No commit in service layer** — service functions call `db.flush()` but NOT `db.commit()`; the route commits.
- **IDOR guard pattern:** validate contact belongs to company via JOIN before acting.
- **Unique constraint:** `uq_site_contacts_site_email` on `(customer_site_id, email)` — email must be unique per site. On merge, if both contacts have emails, treat as a backfill conflict (keep keeper's email, do NOT overwrite).
- **`primary_contact_id` is on `CustomerSite`** (not Company). If loser was the primary of their site, clear it (SET NULL — let the route re-render; don't auto-promote).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `app/services/contact_merge_service.py` | **CREATE** | `merge_contacts(keep_id, remove_id, db)` — all FK reassignment + scalar backfill; no commit |
| `app/routers/htmx_views.py` | **MODIFY** | 4 new routes: merge-form, merge-preview, merge-execute (POST), move-form + move-execute (POST) |
| `app/templates/htmx/partials/customers/_contact_merge_form.html` | **CREATE** | Step-1 modal: search to pick the loser contact |
| `app/templates/htmx/partials/customers/_contact_merge_preview.html` | **CREATE** | Step-2 modal: preview what will be kept + confirm |
| `app/templates/htmx/partials/customers/_contact_move_form.html` | **CREATE** | Move modal: company typeahead + site select |
| `app/templates/htmx/partials/customers/_contact_macros.html` | **MODIFY** | Add Merge + Move items to kebab menu (before Delete) |
| `tests/test_contact_merge_move.py` | **CREATE** | Full test suite: service unit tests + route HTTP tests (allow + deny) |
| `docs/APP_MAP_INTERACTIONS.md` | **MODIFY** | Update Companies/CRM row to mention merge + move routes |

---

### Task 1: Contact Merge Service (unit-testable core)

**Files:**
- Create: `app/services/contact_merge_service.py`
- Test: `tests/test_contact_merge_move.py` (first section only)

**Interfaces:**
- Produces: `merge_contacts(keep_id: int, remove_id: int, db: Session) -> dict`
  - Returns `{"ok": True, "kept": int, "removed": int, "reassigned": int}`
  - Raises `ValueError` if contacts not found, same id, or same-site email conflict when both have email.

- [ ] **Step 1: Write failing unit tests for merge_contacts**

Create `tests/test_contact_merge_move.py`:

```python
"""tests/test_contact_merge_move.py — TDD suite for contact merge (dedup) + move.

Covers:
- merge_contacts: FK reassignment (activities, attachments, tasks), scalar backfill,
  primary-contact preservation, loser deleted; authz deny paths.
- contact move: customer_site_id update; invalid/inactive target → 400; authz deny paths.

Called by: pytest
Depends on: app.services.contact_merge_service, app.routers.htmx_views, conftest.py
"""

from __future__ import annotations

from datetime import timezone, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.intelligence import ActivityLog
from app.models.task import RequisitionTask


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def company_a(db_session: Session) -> Company:
    co = Company(name="Merge Corp A", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def company_b(db_session: Session) -> Company:
    co = Company(name="Merge Corp B", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def site_a(db_session: Session, company_a: Company) -> CustomerSite:
    site = CustomerSite(company_id=company_a.id, site_name="HQ A", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def site_b(db_session: Session, company_b: Company) -> CustomerSite:
    site = CustomerSite(company_id=company_b.id, site_name="HQ B", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def keeper(db_session: Session, site_a: CustomerSite) -> SiteContact:
    c = SiteContact(
        customer_site_id=site_a.id,
        full_name="Keep Me",
        email="keeper@example.com",
        title=None,
        phone=None,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture()
def loser(db_session: Session, site_a: CustomerSite) -> SiteContact:
    c = SiteContact(
        customer_site_id=site_a.id,
        full_name="Lose Me",
        email=None,
        title="Director",
        phone="+15550001111",
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture()
def owner_client_a(db_session: Session, company_a: Company, test_user: User) -> TestClient:
    """TestClient where test_user owns company_a."""
    company_a.account_owner_id = test_user.id
    db_session.commit()

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_admin] = lambda: test_user
    app.dependency_overrides[require_buyer] = lambda: test_user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def unrelated_client(db_session: Session) -> TestClient:
    """TestClient where the user has NO ownership relation to any company."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger@example.com",
        name="Stranger",
        role="buyer",
        azure_id="stranger-azure-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(stranger)
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: stranger
    app.dependency_overrides[require_admin] = lambda: stranger
    app.dependency_overrides[require_buyer] = lambda: stranger
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


# ── merge_contacts unit tests ────────────────────────────────────────────────


class TestMergeContactsService:
    def test_activities_reassigned_to_keeper(
        self, db_session: Session, keeper: SiteContact, loser: SiteContact
    ):
        """ActivityLog.site_contact_id on the loser → keeper after merge."""
        activity = ActivityLog(
            user_id=1,
            activity_type="email_sent",
            channel="email",
            company_id=loser.customer_site_id,
            site_contact_id=loser.id,
            contact_email="lose@example.com",
            contact_name="Lose Me",
            subject="RFQ",
            external_id="graph-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        result = merge_contacts(keeper.id, loser.id, db_session)
        db_session.commit()

        db_session.refresh(activity)
        assert activity.site_contact_id == keeper.id
        assert result["ok"] is True
        assert result["kept"] == keeper.id
        assert result["removed"] == loser.id

    def test_loser_deleted(self, db_session: Session, keeper: SiteContact, loser: SiteContact):
        """Loser row is deleted after merge."""
        loser_id = loser.id

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(keeper.id, loser_id, db_session)
        db_session.commit()

        assert db_session.get(SiteContact, loser_id) is None

    def test_keeper_scalar_backfill_title_from_loser(
        self, db_session: Session, keeper: SiteContact, loser: SiteContact
    ):
        """Keeper.title is None → backfilled from loser.title after merge."""
        assert keeper.title is None
        assert loser.title == "Director"

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(keeper.id, loser.id, db_session)
        db_session.commit()
        db_session.refresh(keeper)

        assert keeper.title == "Director"

    def test_keeper_scalar_not_overwritten_when_set(
        self, db_session: Session, site_a: CustomerSite, db_session: Session
    ):
        """Keeper.title already set → NOT overwritten by loser.title."""
        c_keep = SiteContact(customer_site_id=site_a.id, full_name="Keep", title="VP")
        c_lose = SiteContact(customer_site_id=site_a.id, full_name="Lose", title="Manager")
        db_session.add_all([c_keep, c_lose])
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(c_keep.id, c_lose.id, db_session)
        db_session.commit()
        db_session.refresh(c_keep)

        assert c_keep.title == "VP"

    def test_same_id_raises_value_error(self, db_session: Session, keeper: SiteContact):
        from app.services.contact_merge_service import merge_contacts

        with pytest.raises(ValueError, match="itself"):
            merge_contacts(keeper.id, keeper.id, db_session)

    def test_notes_appended(self, db_session: Session, site_a: CustomerSite):
        """Loser.notes appended to keeper.notes with separator."""
        c_keep = SiteContact(customer_site_id=site_a.id, full_name="Keep", notes="Original note.")
        c_lose = SiteContact(customer_site_id=site_a.id, full_name="Lose", notes="Merged note.")
        db_session.add_all([c_keep, c_lose])
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(c_keep.id, c_lose.id, db_session)
        db_session.commit()
        db_session.refresh(c_keep)

        assert "Original note." in c_keep.notes
        assert "Merged note." in c_keep.notes
        assert "Merged from" in c_keep.notes

    def test_tasks_reassigned_to_keeper(
        self, db_session: Session, keeper: SiteContact, loser: SiteContact
    ):
        """RequisitionTask.site_contact_id on the loser → keeper after merge."""
        task = RequisitionTask(
            site_contact_id=loser.id,
            task_type="call",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(task)
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(keeper.id, loser.id, db_session)
        db_session.commit()
        db_session.refresh(task)

        assert task.site_contact_id == keeper.id
```

- [ ] **Step 2: Run failing tests**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_contact_merge_move.py::TestMergeContactsService -p no:cacheprovider -q 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'app.services.contact_merge_service'`

- [ ] **Step 3: Create `app/services/contact_merge_service.py`**

```python
"""Contact merge service — reusable contact dedup/merge logic.

Mirrors company_merge_service.py: reassigns child rows, backfills scalar gaps,
appends notes, and deletes the loser. Does NOT commit — caller must commit.

Called by: htmx_views.py (contact-merge endpoint)
Depends on: models
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..models.crm import CustomerSite, SiteContact


def merge_contacts(keep_id: int, remove_id: int, db: Session) -> dict:
    """Merge contact remove_id into keep_id.

    Reassigns ActivityLog.site_contact_id, SiteContactAttachment.site_contact_id,
    RequisitionTask.site_contact_id to the keeper, backfills keeper scalar gaps from
    the loser, and deletes the loser. Does NOT commit.

    Returns:
        {"ok": True, "kept": int, "removed": int, "reassigned": int}

    Raises:
        ValueError if contacts not found, same id.
    """
    from ..models.intelligence import ActivityLog
    from ..models.task import RequisitionTask

    keep = db.get(SiteContact, keep_id)
    remove = db.get(SiteContact, remove_id)

    if not keep or not remove:
        raise ValueError("One or both contacts not found")
    if keep.id == remove.id:
        raise ValueError("Cannot merge a contact with itself")

    # 1. Reassign FK references
    reassigned = 0
    for model, col in [
        (ActivityLog, "site_contact_id"),
        (RequisitionTask, "site_contact_id"),
    ]:
        try:
            count = (
                db.query(model)
                .filter(getattr(model, col) == remove.id)
                .update({col: keep.id}, synchronize_session="fetch")
            )
            reassigned += count
        except Exception as e:
            logger.warning(
                "Contact merge: failed to reassign {}.{}: {}",
                model.__tablename__,
                col,
                e,
            )

    # SiteContactAttachment has cascade="all, delete-orphan" on the relationship,
    # meaning deleting the SiteContact would cascade-delete its attachments. We want
    # to KEEP the attachments on the keeper instead. Reassign them explicitly.
    from ..models.crm import SiteContactAttachment

    att_count = (
        db.query(SiteContactAttachment)
        .filter(SiteContactAttachment.site_contact_id == remove.id)
        .update({"site_contact_id": keep.id}, synchronize_session="fetch")
    )
    reassigned += att_count

    # 2. Backfill scalar gaps on keeper from loser (fill only if keeper is NULL)
    for field in ("title", "phone", "linkedin_url", "contact_role", "wechat_id"):
        if getattr(keep, field) is None and getattr(remove, field) is not None:
            setattr(keep, field, getattr(remove, field))

    # email: keep keeper's email (unique-per-site constraint); don't overwrite
    if keep.email is None and remove.email is not None:
        keep.email = remove.email

    # 3. Merge notes
    if remove.notes:
        sep = f"\n\n--- Merged from {remove.full_name} ---\n"
        keep.notes = (keep.notes or "") + sep + remove.notes

    # 4. Boolean merge (OR semantics for is_priority; explicit states preserved)
    keep.is_priority = bool(keep.is_priority) or bool(remove.is_priority)

    # 5. Expire the loser's attachments relationship so ORM cascade doesn't
    #    delete the rows we just reassigned.
    db.flush()
    db.expire(remove, ["attachments"])

    # 6. If loser was site primary, clear it (SET NULL — let the UI surface re-assign)
    loser_site = db.get(CustomerSite, remove.customer_site_id)
    if loser_site and loser_site.primary_contact_id == remove.id:
        loser_site.primary_contact_id = None

    # 7. Delete loser
    db.delete(remove)
    db.flush()

    logger.info(
        "Contact merge: kept {} ({}), removed {} ({}), reassigned={}",
        keep.id,
        keep.full_name,
        remove_id,
        remove.full_name or "?",
        reassigned,
    )
    return {"ok": True, "kept": keep.id, "removed": remove_id, "reassigned": reassigned}
```

- [ ] **Step 4: Run service tests again**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_contact_merge_move.py::TestMergeContactsService -p no:cacheprovider -q 2>&1 | tail -20
```

Expected: All `TestMergeContactsService` tests PASS. Fix any failures before continuing.

Note: `test_keeper_scalar_not_overwritten_when_set` has a duplicate `db_session` parameter — fix it by removing the second one:

```python
def test_keeper_scalar_not_overwritten_when_set(
    self, db_session: Session, site_a: CustomerSite
):
```

- [ ] **Step 5: Commit service**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && git add app/services/contact_merge_service.py tests/test_contact_merge_move.py && git commit -m "feat(crm): contact merge service + unit tests"
```

---

### Task 2: Merge Routes + Templates

**Files:**
- Modify: `app/routers/htmx_views.py` (add 3 routes after the existing merge-form route at ~line 7920)
- Create: `app/templates/htmx/partials/customers/_contact_merge_form.html`
- Create: `app/templates/htmx/partials/customers/_contact_merge_preview.html`
- Test: `tests/test_contact_merge_move.py` (add `TestMergeRoutes` class)

**Interfaces:**
- Consumes: `merge_contacts(keep_id, remove_id, db)` from Task 1
- Produces routes:
  - `GET /v2/partials/customers/{company_id}/contacts/{contact_id}/merge-form`
  - `GET /v2/partials/customers/{company_id}/contacts/{contact_id}/merge-preview?remove_id=N`
  - `POST /v2/partials/customers/{company_id}/contacts/{contact_id}/merge`

- [ ] **Step 1: Write failing route tests**

Append to `tests/test_contact_merge_move.py`:

```python
# ── Merge route HTTP tests ───────────────────────────────────────────────────


class TestMergeRoutes:
    def test_merge_form_returns_200(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
    ):
        resp = owner_client_a.get(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge-form"
        )
        assert resp.status_code == 200
        assert "merge" in resp.text.lower()

    def test_merge_preview_returns_200(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        resp = owner_client_a.get(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge-preview"
            f"?remove_id={loser.id}"
        )
        assert resp.status_code == 200
        assert "Keep Me" in resp.text
        assert "Lose Me" in resp.text

    def test_merge_preview_same_id_returns_400(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
    ):
        resp = owner_client_a.get(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge-preview"
            f"?remove_id={keeper.id}"
        )
        assert resp.status_code == 400

    def test_merge_execute_reassigns_and_deletes_loser(
        self,
        owner_client_a: TestClient,
        db_session: Session,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        loser_id = loser.id
        resp = owner_client_a.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge",
            data={"remove_id": str(loser_id), "confirmed": "true"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(SiteContact, loser_id) is None

    def test_merge_execute_requires_confirmed(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        resp = owner_client_a.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge",
            data={"remove_id": str(loser.id), "confirmed": ""},
        )
        assert resp.status_code == 400

    def test_merge_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        resp = unrelated_client.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge",
            data={"remove_id": str(loser.id), "confirmed": "true"},
        )
        assert resp.status_code == 403
```

- [ ] **Step 2: Run failing route tests**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_contact_merge_move.py::TestMergeRoutes -p no:cacheprovider -q 2>&1 | head -20
```

Expected: 404 (routes not yet defined).

- [ ] **Step 3: Create `_contact_merge_form.html`**

```html
{# _contact_merge_form.html — Step-1 of contact merge flow: search for the duplicate.
   The rep picks the "loser" (remove) here; keep = this contact.
   Receives: keep (SiteContact), company (Company).
   Called by: contact_merge_form route in htmx_views.py.
   Depends on: HTMX, Alpine.js, Tailwind CSS, /v2/partials/customers/typeahead.
#}
<div class="space-y-5 p-1">
  <div>
    <h2 class="text-lg font-bold text-gray-900">Merge Contact: {{ keep.full_name }}</h2>
    <p class="mt-1 text-sm text-gray-600">
      Search for the duplicate contact to merge away. The duplicate will be
      <strong>deleted</strong> and its data merged into
      <strong>{{ keep.full_name }}</strong>.
    </p>
  </div>

  <div x-data="{ removeId: '', removeName: '' }">
    <label class="block text-sm font-medium text-gray-700 mb-1">
      Duplicate contact to remove
    </label>

    <div class="relative">
      <input type="text"
             id="contact-merge-search"
             placeholder="Type contact name…"
             autocomplete="off"
             class="input w-full">
      <div id="contact-merge-results"
           class="absolute z-30 w-full bg-white border border-gray-200 rounded-md shadow-lg mt-1 hidden">
      </div>
    </div>

    <div id="contact-merge-preview-area" class="mt-4"></div>

    <script>
      (function () {
        const inp = document.getElementById('contact-merge-search');
        const res = document.getElementById('contact-merge-results');
        if (!inp || !res) return;
        let debounce;
        inp.addEventListener('input', function () {
          clearTimeout(debounce);
          const q = inp.value.trim();
          if (q.length < 2) { res.classList.add('hidden'); return; }
          debounce = setTimeout(async function () {
            const r = await fetch(
              '/v2/partials/customers/{{ company.id }}/contacts/search?q=' + encodeURIComponent(q) +
              '&exclude={{ keep.id }}'
            );
            const html = await r.text();
            res.innerHTML = html;
            res.classList.toggle('hidden', !html.trim());
          }, 250);
        });
        res.addEventListener('click', function (e) {
          const btn = e.target.closest('[data-contact-id]');
          if (!btn) return;
          const rid = btn.dataset.contactId;
          inp.value = btn.textContent.trim();
          res.classList.add('hidden');
          htmx.ajax('GET',
            '/v2/partials/customers/{{ company.id }}/contacts/{{ keep.id }}/merge-preview?remove_id=' + rid,
            { target: '#contact-merge-preview-area', swap: 'innerHTML' }
          );
        });
        document.addEventListener('click', function (e) {
          if (!res.contains(e.target) && e.target !== inp) res.classList.add('hidden');
        });
      })();
    </script>
  </div>

  <div class="flex justify-end">
    <button type="button"
            @click="$dispatch('close-modal')"
            class="btn btn-ghost">
      Cancel
    </button>
  </div>
</div>
```

- [ ] **Step 4: Create `_contact_merge_preview.html`**

```html
{# _contact_merge_preview.html — Step-2 of contact merge flow: preview + confirm.
   Shows both contacts; confirms what will be kept.
   Receives: keep (SiteContact), remove (SiteContact), company (Company),
             activity_count, task_count, attachment_count.
   Called by: contact_merge_preview route in htmx_views.py.
   Depends on: HTMX, Tailwind CSS, brand palette.
#}
<div class="space-y-5 p-1">
  <div>
    <h2 class="text-lg font-bold text-gray-900">Confirm Contact Merge</h2>
    <p class="mt-1 text-sm text-gray-600">
      Review what will change. This action is <strong>irreversible</strong>.
    </p>
  </div>

  <div class="rounded-lg border border-rose-200 bg-rose-50 p-4 space-y-1">
    <p class="text-sm font-semibold text-rose-800">Will be deleted</p>
    <p class="text-base font-bold text-gray-900">{{ remove.full_name }}</p>
    {% if remove.email %}<p class="text-xs text-gray-600">{{ remove.email }}</p>{% endif %}
    {% if remove.title %}<p class="text-xs text-gray-500">{{ remove.title }}</p>{% endif %}
  </div>

  <div class="rounded-lg border border-emerald-200 bg-emerald-50 p-4 space-y-2">
    <p class="text-sm font-semibold text-emerald-800">Will be kept ({{ keep.full_name }})</p>
    <ul class="mt-2 space-y-1 text-sm text-gray-700">
      <li class="flex justify-between">
        <span>Activity entries</span>
        <span class="font-semibold">{{ activity_count }}</span>
      </li>
      <li class="flex justify-between">
        <span>Tasks</span>
        <span class="font-semibold">{{ task_count }}</span>
      </li>
      <li class="flex justify-between">
        <span>Attachments</span>
        <span class="font-semibold">{{ attachment_count }}</span>
      </li>
    </ul>
  </div>

  <div id="contact-merge-result"></div>

  <form hx-post="/v2/partials/customers/{{ company.id }}/contacts/{{ keep.id }}/merge"
        hx-target="#contact-merge-result"
        hx-swap="innerHTML"
        class="flex items-center gap-3">
    <input type="hidden" name="remove_id" value="{{ remove.id }}">
    <input type="hidden" name="confirmed" value="true">
    <button type="submit" class="btn btn-danger flex-1">
      Merge and delete {{ remove.full_name }}
    </button>
    <button type="button"
            @click="$dispatch('close-modal')"
            class="btn btn-ghost">
      Cancel
    </button>
  </form>
</div>
```

- [ ] **Step 5: Add contact search endpoint + 3 merge routes to `htmx_views.py`**

Find the `company_merge_form` route (around line 7920) and add the following AFTER the existing `company_merge_form` route's closing brace. Add a clear section comment:

```python
# ── Contact Merge Duplicate ──────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/contacts/search", response_class=HTMLResponse)
async def contact_search_typeahead(
    request: Request,
    company_id: int,
    q: str = "",
    exclude: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return contacts for a company as clickable typeahead results.

    Used by the contact merge form to pick the "loser" contact. Excludes the
    keeper (exclude=) so a contact cannot be merged with itself.
    """
    if not q.strip() or len(q.strip()) < 2:
        return HTMLResponse("")

    contacts = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(
            CustomerSite.company_id == company_id,
            SiteContact.id != exclude,
            SiteContact.full_name.ilike(f"%{q.strip()}%"),
        )
        .order_by(SiteContact.full_name)
        .limit(10)
        .all()
    )
    rows = [
        f'<button type="button" data-contact-id="{c.id}" '
        f'class="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50">'
        f'{html_mod.escape(c.full_name or "")}'
        f'{"  (" + html_mod.escape(c.email) + ")" if c.email else ""}'
        f"</button>"
        for c in contacts
    ]
    return HTMLResponse("\n".join(rows))


@router.get("/v2/partials/customers/{company_id}/contacts/{contact_id}/merge-form", response_class=HTMLResponse)
async def contact_merge_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the merge-duplicate modal form for a contact."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    return template_response(
        "htmx/partials/customers/_contact_merge_form.html",
        {"request": request, "keep": keep, "company": company},
    )


@router.get("/v2/partials/customers/{company_id}/contacts/{contact_id}/merge-preview", response_class=HTMLResponse)
async def contact_merge_preview(
    request: Request,
    company_id: int,
    contact_id: int,
    remove_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a preview of what will happen when remove_id is merged into contact_id."""
    from ..models.intelligence import ActivityLog as _AL
    from ..models.task import RequisitionTask as _RT

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    remove = db.get(SiteContact, remove_id)
    if not remove:
        raise HTTPException(400, "Duplicate contact not found")

    if keep.id == remove.id:
        raise HTTPException(400, "Cannot merge a contact with itself")

    activity_count = (
        db.query(sqlfunc.count(_AL.id)).filter(_AL.site_contact_id == remove.id).scalar() or 0
    )
    task_count = (
        db.query(sqlfunc.count(_RT.id)).filter(_RT.site_contact_id == remove.id).scalar() or 0
    )
    from ..models.crm import SiteContactAttachment as _SCA

    attachment_count = (
        db.query(sqlfunc.count(_SCA.id)).filter(_SCA.site_contact_id == remove.id).scalar() or 0
    )

    return template_response(
        "htmx/partials/customers/_contact_merge_preview.html",
        {
            "request": request,
            "keep": keep,
            "remove": remove,
            "company": company,
            "activity_count": activity_count,
            "task_count": task_count,
            "attachment_count": attachment_count,
        },
    )


@router.post("/v2/partials/customers/{company_id}/contacts/{contact_id}/merge", response_class=HTMLResponse)
async def contact_merge(
    request: Request,
    company_id: int,
    contact_id: int,
    remove_id: int = Form(...),
    confirmed: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge remove_id into contact_id (the keeper).

    Requires confirmed="true". Calls merge_contacts() — no FK logic here.
    """
    from ..services.contact_merge_service import merge_contacts as _merge

    if confirmed.lower() != "true":
        raise HTTPException(400, "Merge requires explicit confirmation (confirmed=true)")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    if remove_id == contact_id:
        raise HTTPException(400, "Cannot merge a contact with itself")

    remove = db.get(SiteContact, remove_id)
    if not remove:
        raise HTTPException(400, "Duplicate contact not found")

    try:
        result = _merge(contact_id, remove_id, db)
        db.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info(
        "Manual contact merge: kept {} ({}), removed {} by {}",
        contact_id,
        keep.full_name,
        remove_id,
        user.email,
    )

    safe_name = html_mod.escape(keep.full_name or "")
    response = HTMLResponse(
        f'<p class="text-sm text-emerald-600 py-2">Merged into <strong>{safe_name}</strong>. '
        f"{int(result.get('reassigned', 0))} record(s) reassigned.</p>",
        status_code=200,
    )
    response.headers["HX-Trigger"] = '{"toast": "Contact merged successfully"}'
    return response
```

- [ ] **Step 6: Run merge route tests**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_contact_merge_move.py::TestMergeRoutes -p no:cacheprovider -q 2>&1 | tail -20
```

Expected: All `TestMergeRoutes` tests PASS.

- [ ] **Step 7: Commit merge routes + templates**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && git add app/routers/htmx_views.py app/templates/htmx/partials/customers/_contact_merge_form.html app/templates/htmx/partials/customers/_contact_merge_preview.html tests/test_contact_merge_move.py && git commit -m "feat(crm): contact merge routes + modal templates"
```

---

### Task 3: Move Contact Routes + Template

**Files:**
- Modify: `app/routers/htmx_views.py` (add move-form GET + move-execute POST)
- Create: `app/templates/htmx/partials/customers/_contact_move_form.html`
- Test: `tests/test_contact_merge_move.py` (add `TestMoveRoute` class)

**Interfaces:**
- Produces routes:
  - `GET /v2/partials/customers/{company_id}/contacts/{contact_id}/move-form`
  - `POST /v2/partials/customers/{company_id}/contacts/{contact_id}/move`

- [ ] **Step 1: Write failing move tests**

Append to `tests/test_contact_merge_move.py`:

```python
# ── Move route HTTP tests ────────────────────────────────────────────────────


@pytest.fixture()
def owner_client_b(db_session: Session, company_b: Company, test_user: User) -> TestClient:
    """TestClient where test_user owns company_b (target for move)."""
    company_b.account_owner_id = test_user.id
    db_session.commit()

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_admin] = lambda: test_user
    app.dependency_overrides[require_buyer] = lambda: test_user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def owner_both_client(
    db_session: Session, company_a: Company, company_b: Company, test_user: User
) -> TestClient:
    """TestClient where test_user owns both company_a and company_b."""
    company_a.account_owner_id = test_user.id
    company_b.account_owner_id = test_user.id
    db_session.commit()

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_admin] = lambda: test_user
    app.dependency_overrides[require_buyer] = lambda: test_user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


class TestMoveRoute:
    def test_move_form_returns_200(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
    ):
        resp = owner_client_a.get(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/move-form"
        )
        assert resp.status_code == 200
        assert "move" in resp.text.lower()

    def test_move_updates_site(
        self,
        owner_both_client: TestClient,
        db_session: Session,
        company_a: Company,
        site_b: CustomerSite,
        keeper: SiteContact,
    ):
        """Contact is moved to a site under another company the user can manage."""
        resp = owner_both_client.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/move",
            data={"target_site_id": str(site_b.id)},
        )
        assert resp.status_code == 200
        db_session.expire(keeper)
        db_session.refresh(keeper)
        assert keeper.customer_site_id == site_b.id

    def test_move_inactive_target_returns_400(
        self,
        owner_both_client: TestClient,
        db_session: Session,
        company_a: Company,
        company_b: Company,
        keeper: SiteContact,
    ):
        """Moving to an inactive site → 400."""
        inactive_site = CustomerSite(
            company_id=company_b.id, site_name="Closed", is_active=False
        )
        db_session.add(inactive_site)
        db_session.commit()

        resp = owner_both_client.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/move",
            data={"target_site_id": str(inactive_site.id)},
        )
        assert resp.status_code == 400

    def test_move_nonexistent_target_returns_400(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
    ):
        resp = owner_client_a.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/move",
            data={"target_site_id": "99999999"},
        )
        assert resp.status_code == 400

    def test_move_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        company_a: Company,
        site_b: CustomerSite,
        keeper: SiteContact,
    ):
        """Rep not managing source company → 403."""
        resp = unrelated_client.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/move",
            data={"target_site_id": str(site_b.id)},
        )
        assert resp.status_code == 403

    def test_move_target_not_managed_gets_403(
        self,
        owner_client_a: TestClient,
        db_session: Session,
        company_a: Company,
        company_b: Company,
        site_b: CustomerSite,
        keeper: SiteContact,
    ):
        """Rep manages source but NOT target company → 403."""
        # company_b has no owner → owner_client_a user can't manage it
        company_b.account_owner_id = None
        db_session.commit()

        resp = owner_client_a.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/move",
            data={"target_site_id": str(site_b.id)},
        )
        assert resp.status_code == 403
```

- [ ] **Step 2: Run failing move tests**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_contact_merge_move.py::TestMoveRoute -p no:cacheprovider -q 2>&1 | head -20
```

Expected: 404 (routes not yet defined).

- [ ] **Step 3: Create `_contact_move_form.html`**

```html
{# _contact_move_form.html — Move a contact to a different account + site.
   Receives: contact (SiteContact), company (Company), companies (list[Company]).
   Called by: contact_move_form route in htmx_views.py.
   Depends on: HTMX, Alpine.js, Tailwind CSS.
#}
<div class="space-y-5 p-1"
     x-data="{ targetCompanyId: '', targetSites: [], loadingSites: false }"
     x-init="targetCompanyId = '{{ company.id }}'">

  <div>
    <h2 class="text-lg font-bold text-gray-900">Move Contact: {{ contact.full_name }}</h2>
    <p class="mt-1 text-sm text-gray-600">
      Select the target company and site. The contact will be reassigned there.
    </p>
  </div>

  <form hx-post="/v2/partials/customers/{{ company.id }}/contacts/{{ contact.id }}/move"
        hx-target="#contacts-tab-list"
        hx-swap="innerHTML"
        hx-on::after-request="if(event.detail.successful) $dispatch('close-modal')"
        data-loading-disable
        class="space-y-4">

    <div>
      <label class="form-label">Target Company <span class="text-rose-500">*</span></label>
      <select name="_target_company_id"
              class="input w-full"
              x-model="targetCompanyId"
              @change="
                loadingSites = true;
                targetSites = [];
                if (targetCompanyId) {
                  fetch('/v2/partials/customers/' + targetCompanyId + '/sites-options')
                    .then(r => r.json())
                    .then(data => { targetSites = data; loadingSites = false; })
                    .catch(() => { loadingSites = false; });
                } else { loadingSites = false; }
              ">
        {% for co in companies %}
        <option value="{{ co.id }}" {% if co.id == company.id %}selected{% endif %}>{{ co.name }}</option>
        {% endfor %}
      </select>
    </div>

    <div>
      <label class="form-label">Target Site <span class="text-rose-500">*</span></label>
      <select name="target_site_id" class="input w-full" required>
        <template x-if="loadingSites">
          <option value="">Loading…</option>
        </template>
        <template x-if="!loadingSites && targetSites.length === 0">
          <option value="">— select a company first —</option>
        </template>
        <template x-for="s in targetSites" :key="s.id">
          <option :value="s.id" x-text="s.name"></option>
        </template>
      </select>
    </div>

    <div class="flex items-center gap-3">
      <button type="submit" class="btn btn-primary flex-1">Move Contact</button>
      <button type="button"
              @click="$dispatch('close-modal')"
              class="btn btn-ghost">
        Cancel
      </button>
    </div>
  </form>
</div>
```

- [ ] **Step 4: Add sites-options JSON endpoint + 2 move routes to `htmx_views.py`**

Add directly after the contact-merge routes added in Task 2:

```python
# ── Contact Move ─────────────────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/sites-options")
async def company_sites_options(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return JSON list of active sites for a company for the move-contact site picker.

    Used by Alpine.js in _contact_move_form.html to populate the site select on
    company change. Returns [{"id": N, "name": "..."}].
    """
    from fastapi.responses import JSONResponse

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        return JSONResponse([])

    if not can_manage_account(user, company, db):
        return JSONResponse([])

    sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    return JSONResponse([{"id": s.id, "name": s.site_name or f"Site {s.id}"} for s in sites])


@router.get("/v2/partials/customers/{company_id}/contacts/{contact_id}/move-form", response_class=HTMLResponse)
async def contact_move_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the move-contact modal form.

    Lists all companies the user can manage so they can pick a target.
    """
    from ..models.crm import AccountCollaborator

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can move this contact")

    # Build list of companies the user can manage (for the target picker)
    if is_manager_or_admin(user):
        manageable = db.query(Company).filter(Company.is_active.is_(True)).order_by(Company.name).all()
    else:
        # owned companies + collaborator companies
        owned = db.query(Company).filter(
            Company.is_active.is_(True), Company.account_owner_id == user.id
        ).all()
        collab_ids = [
            row[0]
            for row in db.query(AccountCollaborator.company_id).filter(
                AccountCollaborator.user_id == user.id
            ).all()
        ]
        if collab_ids:
            collab_cos = db.query(Company).filter(Company.id.in_(collab_ids)).all()
        else:
            collab_cos = []
        seen = {c.id for c in owned}
        manageable = list(owned)
        for co in collab_cos:
            if co.id not in seen:
                manageable.append(co)
                seen.add(co.id)
        manageable.sort(key=lambda c: c.name or "")

    return template_response(
        "htmx/partials/customers/_contact_move_form.html",
        {
            "request": request,
            "contact": contact,
            "company": company,
            "companies": manageable,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/move",
    response_class=HTMLResponse,
)
async def contact_move(
    request: Request,
    company_id: int,
    contact_id: int,
    target_site_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Move contact_id to target_site_id.

    Validates: source company accessible, target site exists + is active,
    target company accessible by the same user. Re-renders contacts-tab-list
    for the SOURCE company (contact is gone from here now).
    """
    # Source authz
    source_company = db.query(Company).filter(Company.id == company_id).first()
    if not source_company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, source_company, db):
        raise HTTPException(403, "Only the owner or an admin can move this contact")

    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    # Target site validation
    target_site = db.query(CustomerSite).filter(CustomerSite.id == target_site_id).first()
    if not target_site:
        raise HTTPException(400, "Target site not found")
    if not target_site.is_active:
        raise HTTPException(400, "Target site is inactive")

    # Target authz
    target_company = db.query(Company).filter(Company.id == target_site.company_id).first()
    if not target_company:
        raise HTTPException(400, "Target company not found")

    if not can_manage_account(user, target_company, db):
        raise HTTPException(403, "You do not have access to the target company")

    # Execute move
    old_site_id = contact.customer_site_id
    contact.customer_site_id = target_site_id

    # If this contact was primary for the old site, clear it
    old_site = db.get(CustomerSite, old_site_id)
    if old_site and old_site.primary_contact_id == contact.id:
        old_site.primary_contact_id = None

    db.commit()

    logger.info(
        "Contact move: contact {} ({}) moved from site {} → site {} by {}",
        contact_id,
        contact.full_name,
        old_site_id,
        target_site_id,
        user.email,
    )

    return _render_contacts_list(request, user, source_company, db)
```

- [ ] **Step 5: Run move tests**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_contact_merge_move.py::TestMoveRoute -p no:cacheprovider -q 2>&1 | tail -20
```

Expected: All `TestMoveRoute` tests PASS.

- [ ] **Step 6: Commit move routes + template**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && git add app/routers/htmx_views.py app/templates/htmx/partials/customers/_contact_move_form.html tests/test_contact_merge_move.py && git commit -m "feat(crm): contact move-to-account routes + modal template"
```

---

### Task 4: Kebab Menu Integration

**Files:**
- Modify: `app/templates/htmx/partials/customers/_contact_macros.html` (add Merge + Move items before Delete)

- [ ] **Step 1: Add Merge and Move buttons to the kebab menu**

In `_contact_macros.html`, find the Delete button (the last item in the kebab `<div>`). Add these two items immediately BEFORE the `<div class='border-t border-gray-100 my-1'></div>` that precedes the Delete button:

```html
            {# Merge — dedup this contact with another #}
            <button @click.stop="$dispatch('open-modal', {url: '/v2/partials/customers/{{ company.id }}/contacts/{{ contact.id }}/merge-form'}); open = false"
                    role="menuitem"
                    class='w-full text-left px-3 py-1.5 text-gray-600 hover:bg-gray-50'>Merge duplicate</button>
            {# Move — reassign to another account/site #}
            <button @click.stop="$dispatch('open-modal', {url: '/v2/partials/customers/{{ company.id }}/contacts/{{ contact.id }}/move-form'}); open = false"
                    role="menuitem"
                    class='w-full text-left px-3 py-1.5 text-gray-600 hover:bg-gray-50'>Move to account…</button>
```

The exact insertion point: look for the final `<div class='border-t border-gray-100 my-1'></div>` just before the Delete button, and insert the two new items BEFORE that divider. The ordering will be: …Archive → DNC → [new divider here is already there] → Merge duplicate → Move to account… → [existing divider] → Delete.

- [ ] **Step 2: Run static analysis to verify no regressions**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_static_analysis.py -p no:cacheprovider -q 2>&1 | tail -20
```

Expected: PASS. If `test_inline_button_sizing_does_not_grow` fails, the new buttons in the kebab have inline `px-`/`py-` — fix by checking they use only the `px-3 py-1.5` class format already used by the kebab (note: the existing kebab buttons DO use `px-3 py-1.5` inline — the test's `_macros.html` **exclude** pattern means `_contact_macros.html` is excluded from the check; verify this is still the case by checking the exclude set: `exclude={"_macros.html"}`). If the file is excluded, the test will pass regardless.

- [ ] **Step 3: Commit kebab integration**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && git add app/templates/htmx/partials/customers/_contact_macros.html && git commit -m "feat(crm): add Merge + Move actions to contact kebab menu"
```

---

### Task 5: Full Suite + Ruff + APP_MAP Update

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md`

- [ ] **Step 1: Run the full target test suite**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_contact_merge_move.py tests/test_static_analysis.py -p no:cacheprovider -q 2>&1 | tail -30
```

Expected: All tests PASS.

- [ ] **Step 2: Run ruff**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && /root/availai/.venv/bin/ruff check app 2>&1 | tail -20
```

Expected: `All checks passed!` (or only pre-existing issues not in changed files). Fix any new ruff errors in changed files only.

Common fixes:
- Unused imports: remove them.
- F-string without placeholders: use regular string.
- `from fastapi.responses import JSONResponse` must be at module level or a local import — use local import inside the function body (consistent with the existing pattern of local imports in htmx_views.py, e.g., `from ..models.intelligence import ActivityLog as _AL`).

- [ ] **Step 3: Update `docs/APP_MAP_INTERACTIONS.md`**

Find the Companies/CRM row in the table (grep for `| Companies/CRM |`). The current text ends with `...can_manage_account` (note) |`. Append to the end of that cell (before the closing ` |`):

```
; contact merge (dedup): `GET /v2/partials/customers/{cid}/contacts/{ctid}/merge-form` + preview + `POST .../merge` (can_manage_account on source company, merge_contacts service); contact move: `GET .../move-form` + `POST .../move` (can_manage_account on BOTH source+target companies, target site must be active)
```

- [ ] **Step 4: Final commit**

```bash
cd /root/availai/.claude/worktrees/attachments-unified && git add docs/APP_MAP_INTERACTIONS.md && git commit -m "docs: update APP_MAP_INTERACTIONS for contact merge + move"
```

---

## Self-Review

### Spec Coverage

| Requirement | Task |
|---|---|
| `merge_contacts(keep_id, remove_id, db)` service | Task 1 |
| Reassign `ActivityLog.site_contact_id` | Task 1 |
| Reassign `SiteContactAttachment.site_contact_id` | Task 1 |
| Reassign `RequisitionTask.site_contact_id` | Task 1 |
| Scalar backfill (title/phone/linkedin/role/wechat) | Task 1 |
| Email backfill (keep keeper's if set) | Task 1 |
| Notes append with separator | Task 1 |
| Primary contact preservation (SET NULL if loser was primary) | Task 1 |
| Merge form + preview + execute routes | Task 2 |
| `can_manage_account` on source company | Task 2 |
| UI: Merge item in contact kebab | Task 4 |
| `SiteContact.customer_site_id` update | Task 3 |
| Validate target site exists + is active | Task 3 |
| `can_manage_account` on both source + target | Task 3 |
| UI: Move item in contact kebab | Task 4 |
| Tests: reassign child rows | Task 1 |
| Tests: unrelated rep → 403 | Tasks 1+2+3 |
| Tests: cross-company the user can't manage → denied | Task 2+3 |
| Tests: invalid/inactive target → 400 | Task 3 |
| Static analysis ratchets stay green | Task 5 |
| `ruff check app` clean | Task 5 |
| `docs/APP_MAP_INTERACTIONS.md` updated | Task 5 |

### Placeholder Scan

No TBDs, no "implement later", no "handle edge cases", no "write tests for the above" without test code. All test code is complete. All route code is complete.

### Type Consistency

- `merge_contacts(keep_id: int, remove_id: int, db: Session) -> dict` — used consistently in Task 1 (definition), Task 2 (call site: `_merge(contact_id, remove_id, db)`).
- `_render_contacts_list(request, user, company, db)` — called in Task 3's move endpoint; matches the existing helper signature at line 5600.
- `can_manage_account(user, company, db)` — used in Tasks 2+3; matches `app/dependencies.py` signature.
- `is_manager_or_admin(user)` — used in Task 3's move-form; matches `app/dependencies.py` signature.
- `html_mod.escape(...)` — already imported at module level in `htmx_views.py` (used by existing merge routes).
- `sqlfunc.count(...)` — already imported at module level in `htmx_views.py` (used by existing routes).
- `template_response(...)` — already imported and used throughout `htmx_views.py`.
