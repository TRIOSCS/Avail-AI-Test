# CRM Split-Screen Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the CRM split-screen workspace (persistent left rail + right detail pane) for both Customers and Vendors tabs, with full 8x8 click-to-dial and Microsoft Graph send-email integrations writing to `ActivityLog`.

**Architecture:** Replace the existing flat `customers/list.html` and `vendors/list.html` partials with a workspace layout (`#crm-rail` + `#crm-pane`) at the same URLs. The right pane reuses the existing detail templates parameterized via `pane_target`. Two new service modules wrap 8x8 and Graph; one new sub-router exposes four interaction endpoints; one schema migration adds `User.eightxeight_extension`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, HTMX 2.x + Alpine.js 3.x, Jinja2, Tailwind, pytest (in-memory SQLite), Playwright, Microsoft Graph, 8x8 Work REST API.

**Spec source:** `docs/superpowers/specs/2026-04-30-crm-split-screen-design.md` (commit `c5dd1e50`). Read it before starting.

**Branch context:** `feat/crm-split-screen` is **stacked** off `fix/ci-unblock-alembic-and-audit` (the migration 001 rewrite, in flight in a parallel session). Do NOT modify any 001-rewrite files (`alembic/versions/001_initial_schema.py`, `scripts/reconstruct_001_baseline.py`, `scripts/validate_001_against_chain.py`, `scripts/check_schema_matches_models.py`, `tests/scripts/*`). The CRM PR merges only after the 001 PR lands on green main and this branch is rebased.

**Pattern references in the existing codebase (read these once before starting):**
- `app/routers/htmx_views.py:4263-4318` — `STALENESS_OVERDUE_DAYS`, `STALENESS_DUE_SOON_DAYS`, `_sanitize_hx_params`, `_staleness_tier` (we extract these in Task 2).
- `app/routers/htmx_views.py:4296-4357` — `companies_list_partial` (current implementation; the workspace endpoint replaces this body).
- `app/routers/htmx_views.py:4472-4522` — `company_detail_partial` (existing; gets scoping added in Task 15). **Note the route function is named `company_detail_partial`, singular.**
- `app/routers/htmx_views.py:3400-3475` — `vendors_list_partial` (current implementation; workspace endpoint replaces this body).
- `app/routers/htmx_views.py:3569+` — `vendor_detail_partial`.
- `app/routers/crm/views.py` — already exposes the CRM shell + Performance tab; we mount the new workspace + interactions sub-routers via `app/routers/crm/__init__.py`.
- `app/templates/htmx/partials/customers/list.html` — current flat list (deleted in Task 18).
- `app/templates/htmx/partials/customers/detail.html` — reused as right-pane content; gets click-to-call/email buttons.
- `app/templates/htmx/partials/customers/tabs/site_contacts.html` — has today's `tel:`/`mailto:` links to replace.
- `app/templates/htmx/partials/customers/tabs/site_card.html` — has `tel:`/`mailto:` to replace.
- `app/templates/htmx/partials/vendors/tabs/contacts.html` + `vendors/tabs/contact_row.html` + `vendors/contact_nudges.html` — same.
- `app/utils/graph_client.py` — `_post()` helper (line 193) handles `202 Accepted` from sendMail.
- `app/services/activity_service.py::_update_last_activity` — pattern for stamping `Company.last_activity_at` / `VendorCard.last_activity_at`.
- `tests/conftest.py:222-258` — `client` TestClient fixture with auth override; `test_user`, `test_company`, `test_vendor_card` factories.
- `app/models/intelligence.py:257+` — `ActivityLog` schema (no migration needed; reuse `event_type`, `external_id`, `subject`, `duration_seconds`, `details` JSON).
- `app/models/crm.py:163+` — `SiteContact` (customer-side contact).
- `app/models/vendors.py:136+` — `VendorContact`.
- `app/models/auth.py:17` — `User.role` enum, `User.email_signature` (line 25).

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `app/services/staleness.py` | Pure helpers: `STALENESS_OVERDUE_DAYS`, `STALENESS_DUE_SOON_DAYS`, `staleness_tier(dt)`. Extracted from `htmx_views.py` for reuse in two routers. |
| `app/services/eightxeight_service.py` | 8x8 Work API wrapper: `click_to_dial`, `verify_webhook`, `handle_call_event`. |
| `app/services/graph_send_service.py` | Microsoft Graph sendMail wrapper: `send`. |
| `app/routers/crm/interactions.py` | Four endpoints: composer modal, dial, send-email, 8x8 webhook. |
| `app/templates/htmx/partials/crm/_rail.html` | Shared rail body partial (rows + Needs Attention band). Receives `accounts`, `kind`, `staleness`, `sort`, etc. |
| `app/templates/htmx/partials/crm/_rail_controls.html` | Shared controls partial (search + chips + sort + optional My toggle). |
| `app/templates/htmx/partials/crm/customers_workspace.html` | Workspace shell for Customers tab — wraps `_rail_controls`, `_rail`, `#crm-pane`, mobile back button, keyboard handler. |
| `app/templates/htmx/partials/crm/vendors_workspace.html` | Workspace shell for Vendors tab — same structure, vendor-specific controls. |
| `app/templates/htmx/partials/crm/email_composer.html` | Modal partial for click-to-email. |
| `alembic/versions/<auto>_add_eightxeight_extension_to_users.py` | Migration adding `users.eightxeight_extension VARCHAR(50)`. **`down_revision` placeholder fixed at execution time** — see Task 3. |
| `tests/services/test_staleness.py` | Unit tests for staleness tier function. |
| `tests/services/test_eightxeight_service.py` | Unit tests with mocked HTTP layer. |
| `tests/services/test_graph_send_service.py` | Unit tests with mocked Graph client. |
| `tests/test_crm_workspace.py` | Integration tests for workspace + rail endpoints (customers + vendors). |
| `tests/test_crm_interactions.py` | Integration tests for the four interaction endpoints. |
| `tests/test_crm_helpers.py` | Unit test for `scope_companies_to_user`. |
| `tests/e2e/crm-split-screen.spec.ts` | Playwright spec for the full split-screen flow. |

### Modified files

| File | Change |
|---|---|
| `app/routers/crm/views.py` | Add `customers_workspace`, `customers_rail`, `vendors_workspace`, `vendors_rail` routes. |
| `app/routers/crm/_helpers.py` | Add `scope_companies_to_user`. |
| `app/routers/crm/__init__.py` | Mount `interactions` sub-router. |
| `app/routers/htmx_views.py` | Delete or thin out `companies_list_partial` and `vendors_list_partial` (their URLs now resolve via the new CRM workspace routes). Add scoping enforcement to `company_detail_partial`. Accept `pane_target` / `push_url_base` query params on both detail handlers. Re-export `staleness_tier` from `app/services/staleness` for backward compatibility (kept lazy if needed). |
| `app/models/auth.py::User` | Add `eightxeight_extension = Column(String(50), nullable=True)`. |
| `app/templates/htmx/partials/customers/detail.html` | Honor `pane_target` for tab/insights HTMX; embed `click-to-call`/`click-to-email` buttons in primary contact card. |
| `app/templates/htmx/partials/vendors/detail.html` | Same. |
| `app/templates/htmx/partials/customers/tabs/site_contacts.html` | Replace `tel:`/`mailto:` anchors with HTMX buttons firing dial / composer endpoints. |
| `app/templates/htmx/partials/customers/tabs/site_card.html` | Same. |
| `app/templates/htmx/partials/vendors/tabs/contacts.html` + `vendors/tabs/contact_row.html` + `vendors/contact_nudges.html` | Same. |
| `app/templates/htmx/partials/crm/shell.html` | No change required (verify that the existing `hx-get="/v2/partials/customers"` lazy-load now resolves to the workspace endpoint). |
| `.env.example` | Add `EIGHTXEIGHT_API_BASE_URL`, `EIGHTXEIGHT_API_KEY`, `EIGHTXEIGHT_WEBHOOK_SECRET`. |
| `app/config.py` | Add `Settings.eightxeight_api_base_url`, `eightxeight_api_key`, `eightxeight_webhook_secret`. |

### Deleted files

| File | Reason |
|---|---|
| `app/templates/htmx/partials/customers/list.html` | Replaced by `customers_workspace.html` + `_rail.html`. |
| `app/templates/htmx/partials/vendors/list.html` | Replaced by `vendors_workspace.html` + `_rail.html`. |

---

## Tasks

### Task 1: Pre-flight — verify branch, worktree, and base state

**Files:** none modified. State verification only.

- [ ] **Step 1: Confirm branch and starting commit**

```bash
cd /root/availai
git status --short
git rev-parse --abbrev-ref HEAD
git log --oneline -3
```

Expected:
- Branch: `feat/crm-split-screen`
- HEAD log shows `c5dd1e50 docs(spec): CRM split-screen workspace …`
- Working tree may show `M requirements.txt` and `M scripts/reconstruct_001_baseline.py`, `M scripts/validate_001_against_chain.py` plus untracked `alembic/versions/001_initial_schema.py.draft` and `scripts/validate_001_against_chain.last_run.txt`. **These belong to the parallel 001 work — do NOT stage, commit, or revert them in this branch.** They follow the working tree across branch switches and should be left alone.

If branch is wrong: `git checkout feat/crm-split-screen`.

- [ ] **Step 2: Verify the spec file is committed and readable**

```bash
git show --stat c5dd1e50 -- docs/superpowers/specs/2026-04-30-crm-split-screen-design.md | head -5
ls -la docs/superpowers/specs/2026-04-30-crm-split-screen-design.md
```

Expected: file exists, commit hash matches, ~24KB.

- [ ] **Step 3: Run the existing test suite as a baseline (record fail count)**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_views.py tests/test_routers.py -x --override-ini="addopts=" -q 2>&1 | tail -10
```

Expected: passes or a known-stable fail count. If new failures appear that aren't related to this branch, capture them in a note before continuing — they may have been introduced by the parallel 001 work and should not block CRM tasks.

- [ ] **Step 4: No commit. Move to Task 2.**

---

### Task 2: Extract staleness helpers to `app/services/staleness.py`

**Files:**
- Create: `app/services/staleness.py`
- Test: `tests/services/test_staleness.py`
- Modify: `app/routers/htmx_views.py` (re-import + delegate)

The staleness math currently lives at `app/routers/htmx_views.py:4263-4292`. Extracting it removes the cross-module duplication we'd otherwise need in `app/routers/crm/views.py`.

- [ ] **Step 1: Create `tests/services/__init__.py` if missing**

```bash
mkdir -p tests/services
touch tests/services/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/services/test_staleness.py`:

```python
"""Tests for staleness tier classification."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.staleness import (
    STALENESS_DUE_SOON_DAYS,
    STALENESS_OVERDUE_DAYS,
    staleness_tier,
)


def _ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_constants_match_spec():
    assert STALENESS_OVERDUE_DAYS == 30
    assert STALENESS_DUE_SOON_DAYS == 14


def test_none_returns_new():
    assert staleness_tier(None) == "new"


def test_recent_within_14_days():
    assert staleness_tier(_ago(0)) == "recent"
    assert staleness_tier(_ago(13)) == "recent"


def test_due_soon_14_to_30_days():
    assert staleness_tier(_ago(14)) == "due_soon"
    assert staleness_tier(_ago(29)) == "due_soon"


def test_overdue_30_plus_days():
    assert staleness_tier(_ago(30)) == "overdue"
    assert staleness_tier(_ago(120)) == "overdue"


def test_naive_datetime_treated_as_utc():
    naive = datetime.utcnow() - timedelta(days=5)
    assert staleness_tier(naive) == "recent"
```

- [ ] **Step 3: Run test to verify failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/services/test_staleness.py -v --override-ini="addopts="
```

Expected: `ModuleNotFoundError: No module named 'app.services.staleness'`.

- [ ] **Step 4: Implement `app/services/staleness.py`**

```python
"""Staleness tier classification for CRM accounts.

Pure helper module — no database, no FastAPI. Imported by both
app/routers/htmx_views.py and app/routers/crm/views.py.

Called by: customer/vendor list partials, customer/vendor workspace partials.
"""

from datetime import datetime, timezone

STALENESS_OVERDUE_DAYS = 30
STALENESS_DUE_SOON_DAYS = 14


def staleness_tier(last_activity_at: datetime | None) -> str:
    """Classify a last_activity timestamp into one of: overdue, due_soon, recent, new.

    None → 'new' (never contacted).
    """
    if last_activity_at is None:
        return "new"
    if last_activity_at.tzinfo is None:
        last_activity_at = last_activity_at.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - last_activity_at).days
    if days >= STALENESS_OVERDUE_DAYS:
        return "overdue"
    if days >= STALENESS_DUE_SOON_DAYS:
        return "due_soon"
    return "recent"
```

- [ ] **Step 5: Run test to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/services/test_staleness.py -v --override-ini="addopts="
```

Expected: 6 passed.

- [ ] **Step 6: Update `htmx_views.py` to delegate to the new module**

Open `app/routers/htmx_views.py`. Find the existing constants and `_staleness_tier` (around lines 4263-4292). Replace with re-imports:

```python
from ..services.staleness import (
    STALENESS_DUE_SOON_DAYS,
    STALENESS_OVERDUE_DAYS,
    staleness_tier as _staleness_tier,
)
```

Place that import near the other top-level imports (alphabetical order with neighbors). Delete the inline constants and the old `_staleness_tier` function body. The alias `_staleness_tier` keeps existing call sites in this file working unchanged.

- [ ] **Step 7: Run a smoke regression on the affected routes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_views.py -v --override-ini="addopts="
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py -k "compan or vendor or list" -v --override-ini="addopts=" 2>&1 | tail -20
```

Expected: pre-existing test count unchanged (no new failures from the extraction).

- [ ] **Step 8: Commit**

```bash
git add app/services/staleness.py tests/services/__init__.py tests/services/test_staleness.py app/routers/htmx_views.py
git commit -m "$(cat <<'EOF'
refactor(staleness): extract tier helpers from htmx_views into app/services/staleness

Pure helper module. No behavior change. Sets up shared use across the
CRM workspace routes added in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add `User.eightxeight_extension` column + Alembic migration

**Files:**
- Modify: `app/models/auth.py`
- Create: `alembic/versions/<auto>_add_eightxeight_extension_to_users.py`
- Test: `tests/test_models.py` (append a small assertion test)

Per CLAUDE.md ABSOLUTE RULE #2: never use `Base.metadata.create_all()` for schema changes — explicit `op.add_column()` only.

**`down_revision` handling:** This branch is stacked off the 001 rewrite branch. The `down_revision` of the new migration must point to whatever Alembic head exists at execution time. **Do not hard-code it from this plan.** Instead, run `alembic heads` after `git pull origin fix/ci-unblock-alembic-and-audit` to discover the correct value.

- [ ] **Step 1: Add the column to the model**

Open `app/models/auth.py`. Find the User class. After `email_signature = Column(Text)` (line 25), add:

```python
    eightxeight_extension = Column(String(50), nullable=True)
```

- [ ] **Step 2: Discover the current Alembic head**

```bash
cd /root/availai
docker compose run --rm app alembic heads 2>&1 | tail -5
```

Or, if Docker is unavailable in this environment, read the head from the chain:

```bash
ls alembic/versions/ | sort | tail -5
grep -l "^down_revision = None\|^down_revision: " alembic/versions/*.py | head -3
```

Capture the single revision ID printed (e.g. `restructure_substitutes_json` or whatever the latest filename's `revision = "..."` declares). Save it to a shell variable:

```bash
HEAD_REV=<paste here>
```

- [ ] **Step 3: Generate the migration via autogenerate (then sanity-edit)**

```bash
cd /root/availai
docker compose run --rm app alembic revision --autogenerate -m "add eightxeight_extension to users" 2>&1 | tail -10
```

Or, if Docker isn't available, hand-write the migration. Path: `alembic/versions/<next_id>_add_eightxeight_extension_to_users.py` (use a free numeric prefix; check `ls alembic/versions/ | sort` for the next free number, e.g. `132`).

Hand-written body:

```python
"""add eightxeight_extension to users

Revision ID: <pick a unique short id, e.g. 'add_eightxeight_extension'>
Revises: <HEAD_REV from Step 2>
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "add_eightxeight_extension"
down_revision = "<HEAD_REV from Step 2>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("eightxeight_extension", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "eightxeight_extension")
```

- [ ] **Step 4: Verify migration heads remain singular**

```bash
docker compose run --rm app alembic heads 2>&1 | tail -3
```

Expected: a single revision (the new one). If two heads appear, investigate before continuing — usually means another migration landed on `down_revision`'s parent.

- [ ] **Step 5: Round-trip test the migration locally**

```bash
docker compose run --rm app alembic upgrade head 2>&1 | tail -5
docker compose run --rm app alembic downgrade -1 2>&1 | tail -5
docker compose run --rm app alembic upgrade head 2>&1 | tail -5
```

Expected: all three commands exit 0. If `alembic downgrade -1` errors, the `op.drop_column` in `downgrade()` is broken — fix before commit.

- [ ] **Step 6: Add a model field test**

Open `tests/test_models.py`. Append at the end of the file:

```python
def test_user_has_eightxeight_extension_column(db_session):
    """User.eightxeight_extension is nullable VARCHAR(50)."""
    from app.models import User

    u = User(email="ext-test@example.com", name="Ext Test", role="sales")
    u.eightxeight_extension = "1234"
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    assert u.eightxeight_extension == "1234"

    # Nullable
    u2 = User(email="ext-null@example.com", name="Null Ext", role="sales")
    db_session.add(u2)
    db_session.commit()
    db_session.refresh(u2)
    assert u2.eightxeight_extension is None
```

- [ ] **Step 7: Run the test**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_models.py::test_user_has_eightxeight_extension_column -v --override-ini="addopts="
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/models/auth.py alembic/versions/*_add_eightxeight_extension_to_users.py tests/test_models.py
git commit -m "$(cat <<'EOF'
feat(auth): add User.eightxeight_extension for click-to-dial mapping

One nullable VARCHAR(50) on users. Maps an AvailAI user to their 8x8
extension number; null disables click-to-dial in the UI. Explicit
op.add_column migration, no metadata.create_all.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Add `scope_companies_to_user` helper

**Files:**
- Modify: `app/routers/crm/_helpers.py`
- Test: `tests/test_crm_helpers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_crm_helpers.py`:

```python
"""Tests for app.routers.crm._helpers.scope_companies_to_user."""

import pytest

from app.models import Company, User
from app.routers.crm._helpers import scope_companies_to_user


@pytest.fixture()
def alice(db_session):
    u = User(email="alice@example.com", name="Alice Sales", role="sales")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def bob(db_session):
    u = User(email="bob@example.com", name="Bob Sales", role="sales")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def manager(db_session):
    u = User(email="mgr@example.com", name="Mgr", role="manager")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def admin(db_session):
    u = User(email="adm@example.com", name="Adm", role="admin")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def companies(db_session, alice, bob):
    a = Company(name="A-Corp", account_owner_id=alice.id, is_active=True)
    b = Company(name="B-Corp", account_owner_id=bob.id, is_active=True)
    c = Company(name="C-Corp", account_owner_id=None, is_active=True)
    db_session.add_all([a, b, c])
    db_session.commit()
    return a, b, c


def test_sales_sees_only_own(db_session, alice, companies):
    a, b, c = companies
    q = db_session.query(Company)
    scoped = scope_companies_to_user(q, alice).all()
    assert {x.id for x in scoped} == {a.id}


def test_manager_sees_all(db_session, manager, companies):
    q = db_session.query(Company)
    scoped = scope_companies_to_user(q, manager).all()
    assert {x.name for x in scoped} == {"A-Corp", "B-Corp", "C-Corp"}


def test_admin_sees_all(db_session, admin, companies):
    q = db_session.query(Company)
    scoped = scope_companies_to_user(q, admin).all()
    assert len(scoped) == 3


def test_unowned_company_not_visible_to_non_manager(db_session, alice, companies):
    a, b, c = companies
    q = db_session.query(Company)
    scoped = scope_companies_to_user(q, alice).all()
    ids = {x.id for x in scoped}
    assert c.id not in ids  # unowned not visible to alice
```

- [ ] **Step 2: Run test to verify failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_helpers.py -v --override-ini="addopts="
```

Expected: `ImportError` on `scope_companies_to_user`.

- [ ] **Step 3: Implement the helper**

Open `app/routers/crm/_helpers.py`. Find the imports block at top, ensure `Company` and `User` are importable. Append at the end of the file (or after the last helper):

```python
def scope_companies_to_user(query, user):
    """Apply ownership scoping for the customer rail.

    Managers/admins see everything. Everyone else sees only companies
    where account_owner_id == user.id.
    """
    from ...models import Company

    if user.role in ("manager", "admin"):
        return query
    return query.filter(Company.account_owner_id == user.id)
```

The lazy import avoids any circular-import risk with `app.routers.crm.companies`, which already imports from `_helpers`.

- [ ] **Step 4: Run test to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_helpers.py -v --override-ini="addopts="
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/routers/crm/_helpers.py tests/test_crm_helpers.py
git commit -m "$(cat <<'EOF'
feat(crm): add scope_companies_to_user ownership helper

Centralizes the manager/admin override logic for the customer rail.
Sales/buyer/trader roles see only their own accounts; managers/admins
see all. Used by the workspace + rail endpoints (next commit).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 8x8 service module — `app/services/eightxeight_service.py`

**Files:**
- Create: `app/services/eightxeight_service.py`
- Test: `tests/services/test_eightxeight_service.py`
- Modify: `app/config.py`

- [ ] **Step 1: Add settings fields**

Open `app/config.py`. Find the `Settings` class. Add (group with other env-fed strings, alphabetical):

```python
    eightxeight_api_base_url: str = ""
    eightxeight_api_key: str = ""
    eightxeight_webhook_secret: str = ""
```

These default to empty so non-configured environments simply disable the feature; the route guards on `if not settings.eightxeight_api_key`.

- [ ] **Step 2: Write the failing test**

Create `tests/services/test_eightxeight_service.py`:

```python
"""Tests for the 8x8 click-to-dial service."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import ActivityLog, Company, SiteContact, User
from app.services import eightxeight_service


# ── click_to_dial ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_click_to_dial_posts_correct_body():
    user = User(email="rep@x.com", name="Rep", role="sales")
    user.id = 7
    user.eightxeight_extension = "4242"

    fake_resp = AsyncMock()
    fake_resp.status_code = 200
    fake_resp.json = lambda: {"call_id": "DIAL-ABC-123"}

    mock_post = AsyncMock(return_value=fake_resp)
    with patch("app.services.eightxeight_service.httpx.AsyncClient.post", mock_post):
        with patch.object(eightxeight_service, "_settings") as st:
            st.eightxeight_api_base_url = "https://api.8x8.com"
            st.eightxeight_api_key = "K"
            external_id = await eightxeight_service.click_to_dial(
                user=user, contact_phone="+12125550142", contact_label="Jane"
            )
    assert external_id == "DIAL-ABC-123"
    args, kwargs = mock_post.call_args
    body = kwargs["json"]
    assert body["from_extension"] == "4242"
    assert body["to_number"] == "+12125550142"


@pytest.mark.asyncio
async def test_click_to_dial_raises_on_non_200():
    user = User(email="rep@x.com", name="Rep", role="sales")
    user.eightxeight_extension = "4242"

    fake_resp = AsyncMock()
    fake_resp.status_code = 503
    fake_resp.text = "service unavailable"

    mock_post = AsyncMock(return_value=fake_resp)
    with patch("app.services.eightxeight_service.httpx.AsyncClient.post", mock_post):
        with patch.object(eightxeight_service, "_settings") as st:
            st.eightxeight_api_base_url = "https://api.8x8.com"
            st.eightxeight_api_key = "K"
            with pytest.raises(eightxeight_service.EightxeightError):
                await eightxeight_service.click_to_dial(
                    user=user, contact_phone="+12125550142", contact_label="Jane"
                )


@pytest.mark.asyncio
async def test_click_to_dial_raises_when_no_extension():
    user = User(email="rep@x.com", name="Rep", role="sales")
    user.eightxeight_extension = None
    with pytest.raises(eightxeight_service.MissingExtensionError):
        await eightxeight_service.click_to_dial(
            user=user, contact_phone="+12125550142", contact_label="Jane"
        )


# ── verify_webhook ────────────────────────────────────────────────────


def test_verify_webhook_accepts_good_signature():
    body = b'{"call_id":"X"}'
    secret = "abc"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with patch.object(eightxeight_service, "_settings") as st:
        st.eightxeight_webhook_secret = secret
        assert eightxeight_service.verify_webhook({"X-8x8-Signature": sig}, body) is True


def test_verify_webhook_rejects_bad_signature():
    with patch.object(eightxeight_service, "_settings") as st:
        st.eightxeight_webhook_secret = "abc"
        assert eightxeight_service.verify_webhook(
            {"X-8x8-Signature": "sha256=deadbeef"}, b'{"call_id":"X"}'
        ) is False


def test_verify_webhook_rejects_missing_header():
    with patch.object(eightxeight_service, "_settings") as st:
        st.eightxeight_webhook_secret = "abc"
        assert eightxeight_service.verify_webhook({}, b'{"call_id":"X"}') is False


def test_verify_webhook_rejects_when_secret_not_configured():
    with patch.object(eightxeight_service, "_settings") as st:
        st.eightxeight_webhook_secret = ""
        assert eightxeight_service.verify_webhook(
            {"X-8x8-Signature": "sha256=any"}, b'{}'
        ) is False


# ── handle_call_event ─────────────────────────────────────────────────


def test_handle_call_event_updates_existing_log(db_session, test_user, test_company):
    db_session.add(test_user)
    db_session.add(test_company)
    db_session.commit()

    log = ActivityLog(
        user_id=test_user.id,
        activity_type="call",
        channel="phone",
        company_id=test_company.id,
        event_type="call",
        direction="outbound",
        external_id="DIAL-ABC-123",
        auto_logged=False,
        occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(log)
    db_session.commit()

    payload = {
        "call_id": "DIAL-ABC-123",
        "duration_seconds": 92,
        "disposition": "connected",
        "recording_url": "https://8x8.example/r/abc.mp3",
        "ended_at": "2026-04-30T12:34:56Z",
    }
    eightxeight_service.handle_call_event(payload, db_session)

    db_session.refresh(log)
    assert log.duration_seconds == 92
    assert log.auto_logged is True
    assert log.details["disposition"] == "connected"
    assert log.details["recording_url"] == "https://8x8.example/r/abc.mp3"


def test_handle_call_event_idempotent(db_session, test_user, test_company):
    db_session.add(test_user)
    db_session.add(test_company)
    db_session.commit()
    log = ActivityLog(
        user_id=test_user.id,
        activity_type="call",
        channel="phone",
        company_id=test_company.id,
        event_type="call",
        direction="outbound",
        external_id="DIAL-XYZ",
        auto_logged=False,
        occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(log)
    db_session.commit()

    payload = {
        "call_id": "DIAL-XYZ",
        "duration_seconds": 10,
        "disposition": "voicemail",
        "ended_at": "2026-04-30T01:02:03Z",
    }
    eightxeight_service.handle_call_event(payload, db_session)
    eightxeight_service.handle_call_event(payload, db_session)  # second call

    rows = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.external_id == "DIAL-XYZ")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].duration_seconds == 10


def test_handle_call_event_unknown_external_id_is_noop(db_session):
    payload = {"call_id": "NOT-IN-DB", "duration_seconds": 5, "ended_at": "2026-04-30T00:00:00Z"}
    # Must not raise; should log a warning
    eightxeight_service.handle_call_event(payload, db_session)
    assert (
        db_session.query(ActivityLog).filter(ActivityLog.external_id == "NOT-IN-DB").count() == 0
    )
```

- [ ] **Step 3: Run test to verify failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/services/test_eightxeight_service.py -v --override-ini="addopts="
```

Expected: ImportError on `app.services.eightxeight_service`.

- [ ] **Step 4: Implement the service**

Create `app/services/eightxeight_service.py`:

```python
"""8x8 Work API integration — click-to-dial + call-event webhook.

Public surface:
- click_to_dial(user, contact_phone, contact_label) -> str
    Initiates an outbound call from the user's 8x8 extension. Returns
    the 8x8-assigned call_id used as the ActivityLog.external_id.
- verify_webhook(headers, body) -> bool
    HMAC-SHA256 verification of the X-8x8-Signature header against
    EIGHTXEIGHT_WEBHOOK_SECRET. Returns False on bad/missing signature.
- handle_call_event(payload, db) -> None
    Updates the matching ActivityLog by external_id. Idempotent.

Called by: app/routers/crm/interactions.py
Depends on: httpx, app.config (settings), app.models.ActivityLog
"""

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings as _settings
from ..models import ActivityLog, User


class EightxeightError(Exception):
    """8x8 API returned a non-success status."""


class MissingExtensionError(Exception):
    """User has no eightxeight_extension configured."""


_TIMEOUT = 10.0  # seconds


async def click_to_dial(*, user: User, contact_phone: str, contact_label: str) -> str:
    """POST to the 8x8 click-to-dial endpoint. Returns the 8x8 call_id."""
    if not user.eightxeight_extension:
        raise MissingExtensionError(
            f"User {user.email} has no 8x8 extension configured"
        )
    if not _settings.eightxeight_api_base_url or not _settings.eightxeight_api_key:
        raise EightxeightError("8x8 API not configured")

    url = f"{_settings.eightxeight_api_base_url}/v1/click-to-dial"
    body = {
        "from_extension": user.eightxeight_extension,
        "to_number": contact_phone,
        "label": contact_label,
    }
    headers = {
        "Authorization": f"Bearer {_settings.eightxeight_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as http:
        resp = await http.post(url, json=body, headers=headers, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise EightxeightError(
            f"8x8 returned {resp.status_code}: {resp.text[:200]}"
        )
    payload = resp.json()
    call_id = payload.get("call_id")
    if not call_id:
        raise EightxeightError(f"8x8 response missing call_id: {payload}")
    return call_id


def verify_webhook(headers: dict[str, str], body: bytes) -> bool:
    """HMAC-SHA256 signature verification."""
    secret = _settings.eightxeight_webhook_secret
    if not secret:
        return False
    sig = headers.get("X-8x8-Signature") or headers.get("x-8x8-signature")
    if not sig or not sig.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig.removeprefix("sha256="), expected)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def handle_call_event(payload: dict[str, Any], db: Session) -> None:
    """Update the ActivityLog row matching payload['call_id']. No-op if missing."""
    call_id = payload.get("call_id")
    if not call_id:
        logger.warning("8x8 webhook missing call_id; ignoring")
        return

    log = (
        db.query(ActivityLog)
        .filter(ActivityLog.external_id == call_id)
        .one_or_none()
    )
    if log is None:
        logger.warning("8x8 webhook for unknown call_id={}; ignoring", call_id)
        return

    log.duration_seconds = int(payload.get("duration_seconds") or 0) or log.duration_seconds
    log.auto_logged = True
    ended_at = _parse_iso(payload.get("ended_at"))
    if ended_at is not None:
        log.occurred_at = ended_at

    details = dict(log.details or {})
    if "disposition" in payload:
        details["disposition"] = payload["disposition"]
    if "recording_url" in payload:
        details["recording_url"] = payload["recording_url"]
    log.details = details

    db.commit()

    # Stamp last_activity_at on the linked entity
    if log.company_id and ended_at:
        from ..models import Company

        co = db.get(Company, log.company_id)
        if co is not None:
            co.last_activity_at = ended_at
            db.commit()
    if log.vendor_card_id and ended_at:
        from ..models import VendorCard

        vc = db.get(VendorCard, log.vendor_card_id)
        if vc is not None:
            vc.last_activity_at = ended_at
            db.commit()
```

- [ ] **Step 5: Run test to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/services/test_eightxeight_service.py -v --override-ini="addopts="
```

Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/services/eightxeight_service.py tests/services/test_eightxeight_service.py
git commit -m "$(cat <<'EOF'
feat(8x8): click-to-dial service with HMAC webhook verification

Three public functions: click_to_dial fires the 8x8 click-to-dial REST
endpoint, verify_webhook does HMAC-SHA256 verification of inbound
events, handle_call_event updates the matching ActivityLog by
external_id (idempotent). Three new env vars in app/config.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Graph send-mail service module — `app/services/graph_send_service.py`

**Files:**
- Create: `app/services/graph_send_service.py`
- Test: `tests/services/test_graph_send_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_graph_send_service.py`:

```python
"""Tests for graph_send_service."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import User
from app.services import graph_send_service


@pytest.mark.asyncio
async def test_send_posts_to_sendmail():
    user = User(email="rep@x.com", name="Rep", role="sales")
    user.id = 1
    fake_post = AsyncMock(return_value={})  # 202 returns {}
    fake_token = AsyncMock(return_value="TOK")

    with patch("app.services.graph_send_service._post", fake_post), patch(
        "app.services.graph_send_service._get_user_token", fake_token
    ):
        msg_id = await graph_send_service.send(
            user=user,
            to_email="jane@acme.com",
            to_name="Jane Doe",
            subject="Hi",
            body="Body text",
        )
    assert isinstance(msg_id, str) and len(msg_id) > 0
    args, kwargs = fake_post.call_args
    url = args[0] if args else kwargs.get("url")
    assert url.endswith("/me/sendMail")
    body = kwargs.get("json_data") or args[1]
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == "jane@acme.com"
    assert body["message"]["subject"] == "Hi"


@pytest.mark.asyncio
async def test_send_raises_on_non_2xx():
    user = User(email="rep@x.com", name="Rep", role="sales")
    user.id = 1
    fake_post = AsyncMock(return_value={"error": 400, "detail": "bad recipient"})
    fake_token = AsyncMock(return_value="TOK")

    with patch("app.services.graph_send_service._post", fake_post), patch(
        "app.services.graph_send_service._get_user_token", fake_token
    ):
        with pytest.raises(graph_send_service.GraphSendError):
            await graph_send_service.send(
                user=user,
                to_email="bad@",
                to_name="Bad",
                subject="x",
                body="x",
            )


@pytest.mark.asyncio
async def test_send_raises_when_token_missing():
    user = User(email="rep@x.com", name="Rep", role="sales")
    user.id = 1
    fake_token = AsyncMock(return_value=None)
    with patch("app.services.graph_send_service._get_user_token", fake_token):
        with pytest.raises(graph_send_service.GraphSendError):
            await graph_send_service.send(
                user=user, to_email="x@y", to_name="x", subject="x", body="x"
            )
```

- [ ] **Step 2: Run test to verify failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/services/test_graph_send_service.py -v --override-ini="addopts="
```

Expected: ImportError.

- [ ] **Step 3: Implement the service**

Create `app/services/graph_send_service.py`:

```python
"""Microsoft Graph sendMail wrapper.

Public surface:
- send(user, to_email, to_name, subject, body) -> str
    POSTs /me/sendMail using the user's delegated token. Returns a
    synthetic message-id (Graph 202 doesn't return one) used as
    ActivityLog.external_id for traceability.

Called by: app/routers/crm/interactions.py
Depends on: app.utils.graph_client (token retrieval + low-level _post)
"""

import uuid

from loguru import logger

from ..models import User
from ..utils.graph_client import _get_user_token, _post  # type: ignore[attr-defined]


class GraphSendError(Exception):
    """Microsoft Graph rejected the sendMail call."""


async def send(
    *, user: User, to_email: str, to_name: str, subject: str, body: str
) -> str:
    """Send an email on behalf of `user` to `to_email`.

    Returns a synthetic external_id (uuid4 hex prefixed with 'graph-') so
    the resulting ActivityLog row is traceable even though Graph 202
    doesn't echo a message id.
    """
    token = await _get_user_token(user)
    if not token:
        raise GraphSendError(
            f"No Graph token available for user {user.email}"
        )

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [
                {"emailAddress": {"address": to_email, "name": to_name or to_email}}
            ],
        },
        "saveToSentItems": "true",
    }

    result = await _post("https://graph.microsoft.com/v1.0/me/sendMail", payload, token=token)
    if isinstance(result, dict) and result.get("error"):
        raise GraphSendError(f"Graph error: {result}")

    msg_id = f"graph-{uuid.uuid4().hex}"
    logger.info("Graph sendMail accepted for user={} to={}", user.email, to_email)
    return msg_id
```

**Note on `_get_user_token` and `_post`:** These are existing helpers in `app/utils/graph_client.py`. If their public signatures don't match exactly (`_post(url, json, token=...)`), introduce a thin adapter in `graph_send_service.py` rather than modifying `graph_client.py` (it's owned by the email-mining feature). Read the existing signatures first:

```bash
grep -nE "^async def _post|^def _get_user_token|^async def _get_user_token" app/utils/graph_client.py
```

Adapt the call accordingly.

- [ ] **Step 4: Run test to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/services/test_graph_send_service.py -v --override-ini="addopts="
```

Expected: 3 passed. If the test mocks misalign with the actual `_post` signature, fix the test mocks (not the service module).

- [ ] **Step 5: Commit**

```bash
git add app/services/graph_send_service.py tests/services/test_graph_send_service.py
git commit -m "$(cat <<'EOF'
feat(graph): send-mail wrapper for click-to-email

Single public function send(); wraps Microsoft Graph /me/sendMail with
delegated user token. Raises GraphSendError on any non-2xx so the
calling route can surface failure to the user (no silent failures).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `interactions` sub-router — four endpoints

**Files:**
- Create: `app/routers/crm/interactions.py`
- Modify: `app/routers/crm/__init__.py`
- Test: `tests/test_crm_interactions.py`

- [ ] **Step 1: Write failing tests covering all four endpoints**

Create `tests/test_crm_interactions.py`:

```python
"""Tests for CRM interaction endpoints (dial, send-email, webhook, composer)."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    SiteContact,
    User,
    VendorCard,
    VendorContact,
)


@pytest.fixture()
def site_contact(db_session, test_company):
    site = CustomerSite(company_id=test_company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.commit()
    c = SiteContact(
        site_id=site.id,
        full_name="Jane Doe",
        email="jane@acme.com",
        phone="+12125550142",
    )
    db_session.add(c)
    db_session.commit()
    return c


@pytest.fixture()
def vendor_contact(db_session, test_vendor_card):
    c = VendorContact(
        vendor_card_id=test_vendor_card.id,
        full_name="Vince Vendor",
        email="vince@vendor.com",
        phone="+13105550000",
    )
    db_session.add(c)
    db_session.commit()
    return c


# ── /dial ─────────────────────────────────────────────────────────────


def test_dial_creates_activity_log(client, db_session, test_user, site_contact):
    test_user.eightxeight_extension = "4242"
    db_session.commit()
    fake_dial = AsyncMock(return_value="DIAL-AAA")
    with patch(
        "app.routers.crm.interactions.eightxeight_service.click_to_dial", fake_dial
    ):
        r = client.post(f"/v2/crm/contacts/{site_contact.id}/dial")
    assert r.status_code == 200
    log = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.external_id == "DIAL-AAA")
        .one()
    )
    assert log.event_type == "call"
    assert log.direction == "outbound"
    assert log.site_contact_id == site_contact.id
    assert log.auto_logged is False
    assert "showToast" in r.headers.get("HX-Trigger", "")


def test_dial_returns_400_when_extension_missing(client, db_session, test_user, site_contact):
    test_user.eightxeight_extension = None
    db_session.commit()
    r = client.post(f"/v2/crm/contacts/{site_contact.id}/dial")
    assert r.status_code == 400
    assert (
        db_session.query(ActivityLog)
        .filter(ActivityLog.event_type == "call")
        .count()
        == 0
    )


def test_dial_404_for_unknown_contact(client, test_user, db_session):
    test_user.eightxeight_extension = "4242"
    db_session.commit()
    r = client.post("/v2/crm/contacts/99999/dial")
    assert r.status_code == 404


# ── /email-composer (modal GET) ───────────────────────────────────────


def test_email_composer_renders(client, test_user, site_contact, db_session):
    test_user.email_signature = "—\nRep\nAvailAI"
    db_session.commit()
    r = client.get(f"/v2/crm/contacts/{site_contact.id}/email-composer")
    assert r.status_code == 200
    assert "jane@acme.com" in r.text
    assert "—" in r.text  # signature appears


# ── /send-email ───────────────────────────────────────────────────────


def test_send_email_creates_log_and_closes_modal(client, db_session, site_contact):
    fake_send = AsyncMock(return_value="graph-xyz123")
    with patch("app.routers.crm.interactions.graph_send_service.send", fake_send):
        r = client.post(
            f"/v2/crm/contacts/{site_contact.id}/send-email",
            data={"subject": "Hi", "body": "Body text"},
        )
    assert r.status_code == 200
    log = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.external_id == "graph-xyz123")
        .one()
    )
    assert log.event_type == "email"
    assert log.direction == "outbound"
    assert log.subject == "Hi"
    assert "closeModal" in r.headers.get("HX-Trigger", "")


def test_send_email_validates_non_empty_subject(client, site_contact):
    r = client.post(
        f"/v2/crm/contacts/{site_contact.id}/send-email",
        data={"subject": "", "body": "Body"},
    )
    assert r.status_code == 400


def test_send_email_does_not_create_log_on_graph_error(client, db_session, site_contact):
    from app.services.graph_send_service import GraphSendError

    fake_send = AsyncMock(side_effect=GraphSendError("boom"))
    with patch("app.routers.crm.interactions.graph_send_service.send", fake_send):
        r = client.post(
            f"/v2/crm/contacts/{site_contact.id}/send-email",
            data={"subject": "Hi", "body": "Body"},
        )
    assert r.status_code == 502
    assert (
        db_session.query(ActivityLog)
        .filter(ActivityLog.event_type == "email")
        .count()
        == 0
    )


# ── /8x8/webhook ──────────────────────────────────────────────────────


def test_webhook_rejects_bad_hmac(client):
    r = client.post(
        "/v2/crm/8x8/webhook",
        content=b'{"call_id":"X"}',
        headers={"X-8x8-Signature": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_webhook_accepts_good_hmac_and_updates_log(
    client, db_session, test_user, test_company, settings
):
    log = ActivityLog(
        user_id=test_user.id,
        activity_type="call",
        channel="phone",
        company_id=test_company.id,
        event_type="call",
        direction="outbound",
        external_id="DIAL-WB-1",
        auto_logged=False,
    )
    db_session.add(log)
    db_session.commit()

    secret = "wb-secret"
    settings.eightxeight_webhook_secret = secret
    body = json.dumps(
        {"call_id": "DIAL-WB-1", "duration_seconds": 17, "ended_at": "2026-04-30T10:00:00Z"}
    ).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    r = client.post(
        "/v2/crm/8x8/webhook",
        content=body,
        headers={"X-8x8-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 204
    db_session.refresh(log)
    assert log.duration_seconds == 17
    assert log.auto_logged is True


# ── vendor-side polymorphism ──────────────────────────────────────────


def test_dial_routes_to_vendor_contact(client, db_session, test_user, vendor_contact):
    test_user.eightxeight_extension = "4242"
    db_session.commit()
    fake_dial = AsyncMock(return_value="DIAL-VEN-1")
    with patch(
        "app.routers.crm.interactions.eightxeight_service.click_to_dial", fake_dial
    ):
        # Route by /v2/crm/vendor-contacts/{id}/dial to disambiguate
        r = client.post(f"/v2/crm/vendor-contacts/{vendor_contact.id}/dial")
    assert r.status_code == 200
    log = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.external_id == "DIAL-VEN-1")
        .one()
    )
    assert log.vendor_contact_id == vendor_contact.id
```

A `settings` fixture is needed for the webhook test. Add to `tests/conftest.py` (append, don't modify existing fixtures):

```python
@pytest.fixture()
def settings():
    """Mutable settings access for tests that need to flip env values."""
    from app.config import settings as s
    return s
```

- [ ] **Step 2: Run test to verify failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_interactions.py -v --override-ini="addopts=" 2>&1 | tail -20
```

Expected: 404s (routes don't exist).

- [ ] **Step 3: Implement the router**

Create `app/routers/crm/interactions.py`:

```python
"""CRM interaction endpoints — click-to-dial, click-to-email, 8x8 webhook.

Routes:
- GET  /v2/crm/contacts/{contact_id}/email-composer — modal partial
- POST /v2/crm/contacts/{contact_id}/dial          — site-contact dial
- POST /v2/crm/contacts/{contact_id}/send-email    — site-contact email
- POST /v2/crm/vendor-contacts/{contact_id}/dial   — vendor-contact dial
- POST /v2/crm/vendor-contacts/{contact_id}/send-email — vendor-contact email
- POST /v2/crm/8x8/webhook                          — call-event ingest

Called by: HTMX buttons in customers/detail.html, vendors/detail.html,
           plus the 8x8 service webhook delivery.
Depends on: app.services.eightxeight_service, app.services.graph_send_service,
            app.routers.crm._helpers.scope_companies_to_user.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import (
    ActivityLog,
    Company,
    CustomerSite,
    SiteContact,
    User,
    VendorCard,
    VendorContact,
)
from ...services import eightxeight_service, graph_send_service

router = APIRouter(tags=["crm-interactions"])


# ── Helpers ──────────────────────────────────────────────────────────


def _resolve_site_contact(
    contact_id: int, user: User, db: Session
) -> tuple[SiteContact, CustomerSite, Company]:
    contact = db.get(SiteContact, contact_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    site = db.get(CustomerSite, contact.site_id)
    if site is None:
        raise HTTPException(404, "Contact site not found")
    company = db.get(Company, site.company_id)
    if company is None:
        raise HTTPException(404, "Contact company not found")
    if user.role not in ("manager", "admin") and company.account_owner_id != user.id:
        raise HTTPException(403, "Not authorized for this account")
    return contact, site, company


def _resolve_vendor_contact(
    contact_id: int, db: Session
) -> tuple[VendorContact, VendorCard]:
    contact = db.get(VendorContact, contact_id)
    if contact is None:
        raise HTTPException(404, "Vendor contact not found")
    vendor = db.get(VendorCard, contact.vendor_card_id)
    if vendor is None:
        raise HTTPException(404, "Vendor not found")
    return contact, vendor


def _hx_trigger(payload: dict) -> dict[str, str]:
    return {"HX-Trigger": json.dumps(payload)}


# ── Email composer modal ────────────────────────────────────────────


@router.get("/v2/crm/contacts/{contact_id}/email-composer", response_class=HTMLResponse)
def site_contact_email_composer(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the email composer modal for a site contact."""
    from ...template_env import templates

    contact, _site, company = _resolve_site_contact(contact_id, user, db)
    if not contact.email:
        raise HTTPException(400, "Contact has no email address")
    return templates.TemplateResponse(
        "htmx/partials/crm/email_composer.html",
        {
            "request": request,
            "user": user,
            "to_email": contact.email,
            "to_name": contact.full_name,
            "post_url": f"/v2/crm/contacts/{contact.id}/send-email",
            "signature": user.email_signature or "",
        },
    )


@router.get("/v2/crm/vendor-contacts/{contact_id}/email-composer", response_class=HTMLResponse)
def vendor_contact_email_composer(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...template_env import templates

    contact, _vendor = _resolve_vendor_contact(contact_id, db)
    if not contact.email:
        raise HTTPException(400, "Contact has no email address")
    return templates.TemplateResponse(
        "htmx/partials/crm/email_composer.html",
        {
            "request": request,
            "user": user,
            "to_email": contact.email,
            "to_name": contact.full_name,
            "post_url": f"/v2/crm/vendor-contacts/{contact.id}/send-email",
            "signature": user.email_signature or "",
        },
    )


# ── Click-to-dial ────────────────────────────────────────────────────


@router.post("/v2/crm/contacts/{contact_id}/dial")
async def dial_site_contact(
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    contact, _site, company = _resolve_site_contact(contact_id, user, db)
    if not contact.phone:
        raise HTTPException(400, "Contact has no phone")
    try:
        external_id = await eightxeight_service.click_to_dial(
            user=user, contact_phone=contact.phone, contact_label=contact.full_name
        )
    except eightxeight_service.MissingExtensionError:
        raise HTTPException(400, "Your 8x8 extension is not configured")
    except eightxeight_service.EightxeightError as e:
        raise HTTPException(502, f"Couldn't start call: {e}")

    now = datetime.now(timezone.utc)
    log = ActivityLog(
        user_id=user.id,
        activity_type="call",
        channel="phone",
        company_id=company.id,
        site_contact_id=contact.id,
        contact_email=contact.email,
        contact_phone=contact.phone,
        contact_name=contact.full_name,
        event_type="call",
        direction="outbound",
        external_id=external_id,
        auto_logged=False,
        occurred_at=now,
    )
    db.add(log)
    company.last_activity_at = now
    db.commit()

    return Response(
        status_code=200,
        headers=_hx_trigger(
            {
                "showToast": {
                    "type": "success",
                    "message": f"Calling {contact.full_name} — your phone will ring",
                }
            }
        ),
    )


@router.post("/v2/crm/vendor-contacts/{contact_id}/dial")
async def dial_vendor_contact(
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    contact, vendor = _resolve_vendor_contact(contact_id, db)
    phone = contact.phone or (vendor.phones[0] if vendor.phones else None)
    if not phone:
        raise HTTPException(400, "No phone number for this vendor contact")
    try:
        external_id = await eightxeight_service.click_to_dial(
            user=user, contact_phone=phone, contact_label=contact.full_name
        )
    except eightxeight_service.MissingExtensionError:
        raise HTTPException(400, "Your 8x8 extension is not configured")
    except eightxeight_service.EightxeightError as e:
        raise HTTPException(502, f"Couldn't start call: {e}")

    now = datetime.now(timezone.utc)
    log = ActivityLog(
        user_id=user.id,
        activity_type="call",
        channel="phone",
        vendor_card_id=vendor.id,
        vendor_contact_id=contact.id,
        contact_email=contact.email,
        contact_phone=phone,
        contact_name=contact.full_name,
        event_type="call",
        direction="outbound",
        external_id=external_id,
        auto_logged=False,
        occurred_at=now,
    )
    db.add(log)
    vendor.last_activity_at = now
    db.commit()

    return Response(
        status_code=200,
        headers=_hx_trigger(
            {
                "showToast": {
                    "type": "success",
                    "message": f"Calling {contact.full_name} — your phone will ring",
                }
            }
        ),
    )


# ── Send-email ──────────────────────────────────────────────────────


@router.post("/v2/crm/contacts/{contact_id}/send-email")
async def send_email_site_contact(
    contact_id: int,
    subject: str = Form(...),
    body: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not subject.strip():
        raise HTTPException(400, "Subject is required")
    if not body.strip():
        raise HTTPException(400, "Body is required")

    contact, _site, company = _resolve_site_contact(contact_id, user, db)
    try:
        external_id = await graph_send_service.send(
            user=user,
            to_email=contact.email,
            to_name=contact.full_name,
            subject=subject,
            body=body,
        )
    except graph_send_service.GraphSendError as e:
        raise HTTPException(502, f"Couldn't send email: {e}")

    now = datetime.now(timezone.utc)
    log = ActivityLog(
        user_id=user.id,
        activity_type="email",
        channel="email",
        company_id=company.id,
        site_contact_id=contact.id,
        contact_email=contact.email,
        contact_name=contact.full_name,
        event_type="email",
        direction="outbound",
        subject=subject,
        external_id=external_id,
        auto_logged=False,
        occurred_at=now,
    )
    db.add(log)
    company.last_activity_at = now
    db.commit()

    return Response(
        status_code=200,
        headers=_hx_trigger(
            {
                "showToast": {"type": "success", "message": "Email sent"},
                "closeModal": True,
            }
        ),
    )


@router.post("/v2/crm/vendor-contacts/{contact_id}/send-email")
async def send_email_vendor_contact(
    contact_id: int,
    subject: str = Form(...),
    body: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not subject.strip():
        raise HTTPException(400, "Subject is required")
    if not body.strip():
        raise HTTPException(400, "Body is required")

    contact, vendor = _resolve_vendor_contact(contact_id, db)
    try:
        external_id = await graph_send_service.send(
            user=user,
            to_email=contact.email,
            to_name=contact.full_name,
            subject=subject,
            body=body,
        )
    except graph_send_service.GraphSendError as e:
        raise HTTPException(502, f"Couldn't send email: {e}")

    now = datetime.now(timezone.utc)
    log = ActivityLog(
        user_id=user.id,
        activity_type="email",
        channel="email",
        vendor_card_id=vendor.id,
        vendor_contact_id=contact.id,
        contact_email=contact.email,
        contact_name=contact.full_name,
        event_type="email",
        direction="outbound",
        subject=subject,
        external_id=external_id,
        auto_logged=False,
        occurred_at=now,
    )
    db.add(log)
    vendor.last_activity_at = now
    db.commit()

    return Response(
        status_code=200,
        headers=_hx_trigger(
            {
                "showToast": {"type": "success", "message": "Email sent"},
                "closeModal": True,
            }
        ),
    )


# ── 8x8 webhook ──────────────────────────────────────────────────────


@router.post("/v2/crm/8x8/webhook")
async def eightxeight_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    if not eightxeight_service.verify_webhook(dict(request.headers), body):
        raise HTTPException(401, "Invalid signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")
    eightxeight_service.handle_call_event(payload, db)
    return Response(status_code=204)
```

- [ ] **Step 4: Mount the sub-router**

Open `app/routers/crm/__init__.py`. After the existing imports, add:

```python
from .interactions import router as interactions_router
```

After `router.include_router(views_router)`, add:

```python
router.include_router(interactions_router)
```

- [ ] **Step 5: Run tests to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_interactions.py -v --override-ini="addopts="
```

Expected: 11 passed. If `_get_user_token` patching fails because it's not at module top-level in `graph_client.py`, adjust the mock target string accordingly.

- [ ] **Step 6: Commit**

```bash
git add app/routers/crm/interactions.py app/routers/crm/__init__.py tests/test_crm_interactions.py tests/conftest.py
git commit -m "$(cat <<'EOF'
feat(crm): interactions router — click-to-dial, click-to-email, 8x8 webhook

Polymorphic across SiteContact (customers) and VendorContact (vendors)
via separate /v2/crm/contacts/* and /v2/crm/vendor-contacts/* routes.
ActivityLog rows are created with external_id; the 8x8 webhook
subsequently updates them with duration + recording_url. Email errors
surface to the rep — no silent failures.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: `_rail_controls.html` partial

**Files:**
- Create: `app/templates/htmx/partials/crm/_rail_controls.html`

This is a Jinja2 include (no test-of-its-own; tested transitively via Task 11). Receives:
- `kind` ("customers" or "vendors")
- `search` (current search string)
- `staleness` (current chip selection: "all" | "overdue" | "due_soon" | "recent" | "new")
- `sort` (current sort key)
- `my_only` (bool, customers only)
- `is_manager` (bool, customers only)
- `target_id` (defaults to `#crm-rail`)
- `workspace_url_base` ("/v2/partials/crm/customers/rail" or "/v2/partials/crm/vendors/rail")

- [ ] **Step 1: Create the file**

Create `app/templates/htmx/partials/crm/_rail_controls.html`:

```html
{# _rail_controls.html — search + staleness chips + sort + (optional) my-toggle.
   Included by customers_workspace.html and vendors_workspace.html.
   Receives: kind ("customers"|"vendors"), search, staleness, sort, my_only,
             is_manager, target_id, workspace_url_base.
#}

{% set staleness_chips = [
  ("all", "All"),
  ("overdue", "Overdue"),
  ("due_soon", "Due Soon"),
  ("recent", "Recent"),
  ("new", "New"),
] %}

{% set sort_options_customers = [
  ("most_overdue", "Most overdue"),
  ("recently_contacted", "Recently contacted"),
  ("name_asc", "Name A–Z"),
  ("created_desc", "Last created"),
] %}
{% set sort_options_vendors = [
  ("recently_active", "Recently active"),
  ("engagement_desc", "Top engagement"),
  ("name_asc", "Name A–Z"),
  ("sighting_desc", "Sighting count"),
] %}
{% set sort_options = sort_options_customers if kind == "customers" else sort_options_vendors %}

<form id="crm-rail-controls"
      class="space-y-2 px-3 py-2 border-b border-brand-200 bg-white sticky top-0 z-10">
  <input type="hidden" name="staleness" value="{{ staleness }}">
  <input type="hidden" name="sort" value="{{ sort }}">
  {% if kind == "customers" %}
  <input type="hidden" name="my_only" value="{{ '1' if my_only else '0' }}">
  {% endif %}

  <input aria-label="Search {{ kind }}"
         type="text" name="search" value="{{ search }}"
         placeholder="Search {{ kind }}…"
         hx-get="{{ workspace_url_base }}"
         hx-target="{{ target_id }}"
         hx-include="#crm-rail-controls"
         hx-swap="outerHTML"
         hx-trigger="keyup changed delay:300ms"
         data-rail-search="1"
         class="w-full px-2 py-1 text-sm border border-gray-200 rounded
                focus:ring-1 focus:ring-brand-500 focus:border-brand-500">

  <div class="flex flex-wrap gap-1">
    {% for value, label in staleness_chips %}
    <button type="button"
            class="px-2 py-0.5 text-[11px] rounded-full border
                   {% if staleness == value %}bg-brand-500 text-white border-brand-500
                   {% else %}bg-white text-gray-600 border-gray-200 hover:bg-brand-50{% endif %}"
            hx-get="{{ workspace_url_base }}?staleness={{ value }}"
            hx-target="{{ target_id }}"
            hx-include="#crm-rail-controls"
            hx-swap="outerHTML">
      {{ label }}
    </button>
    {% endfor %}
  </div>

  <div class="flex items-center gap-2">
    <select name="sort"
            hx-get="{{ workspace_url_base }}"
            hx-target="{{ target_id }}"
            hx-include="#crm-rail-controls"
            hx-swap="outerHTML"
            hx-trigger="change"
            class="text-[12px] border border-gray-200 rounded px-1 py-0.5">
      {% for value, label in sort_options %}
      <option value="{{ value }}" {% if sort == value %}selected{% endif %}>{{ label }}</option>
      {% endfor %}
    </select>

    {% if kind == "customers" and is_manager %}
    <label class="flex items-center gap-1 text-[12px] text-gray-600">
      <input type="checkbox" name="my_only" value="1"
             {% if my_only %}checked{% endif %}
             hx-get="{{ workspace_url_base }}"
             hx-target="{{ target_id }}"
             hx-include="#crm-rail-controls"
             hx-swap="outerHTML"
             hx-trigger="change">
      My accounts
    </label>
    {% endif %}
  </div>
</form>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/partials/crm/_rail_controls.html
git commit -m "feat(crm): _rail_controls partial — search, chips, sort, my-toggle

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: `_rail.html` partial — rows + Needs Attention band

**Files:**
- Create: `app/templates/htmx/partials/crm/_rail.html`

Receives:
- `kind` ("customers" or "vendors")
- `accounts` (list of model instances; each has `.id`, `.staleness`, `.last_activity_at`, `.name` (or `.display_name` for vendors))
- `selected_id` (int|None — the currently focused account)
- `overdue_count` (int — for the Needs Attention band; only set when kind="customers" and staleness != "overdue")
- `pane_target` ("#crm-pane")
- `push_url_base` ("/v2/crm/customers" or "/v2/crm/vendors")
- `detail_url_base` ("/v2/partials/customers" or "/v2/partials/vendors")
- `workspace_url_base` (for the Needs-Attention `Show overdue only` link)
- `staleness` (current chip — used to hide the band when already filtered)

- [ ] **Step 1: Create the file**

Create `app/templates/htmx/partials/crm/_rail.html`:

```html
{# _rail.html — rail body: rows + (optional) Needs Attention band.
   Included by customers_workspace.html and vendors_workspace.html, and
   returned standalone by /v2/partials/crm/{kind}/rail.
   Receives: kind, accounts, selected_id, overdue_count, staleness,
             pane_target, push_url_base, detail_url_base, workspace_url_base.
#}

{% set dot_colors = {
  "overdue":  "bg-rose-500",
  "due_soon": "bg-amber-400",
  "recent":   "bg-emerald-400",
  "new":      "bg-brand-300",
} %}

<div id="crm-rail"
     class="flex flex-col h-full overflow-hidden border-r border-brand-200 bg-white
            {% if selected_id %}hidden lg:flex{% endif %}"
     style="width: 320px; min-width: 320px;"
     x-data="crmRailKeyboard()"
     @keydown.window="onKey($event)">

  {% include "htmx/partials/crm/_rail_controls.html" %}

  {% if kind == "customers" and overdue_count and staleness != "overdue" %}
  <div class="mx-3 mt-2 mb-1 rounded-lg bg-rose-50 border border-rose-200 px-3 py-2 text-xs">
    <div class="flex items-center justify-between">
      <span class="font-medium text-rose-800">⚠ Needs Attention</span>
      <span class="text-rose-600">{{ overdue_count }} overdue</span>
    </div>
    <button type="button"
            hx-get="{{ workspace_url_base }}?staleness=overdue"
            hx-target="#crm-rail"
            hx-include="#crm-rail-controls"
            hx-swap="outerHTML"
            class="mt-1 text-[11px] text-rose-700 underline hover:text-rose-900">
      Show overdue only
    </button>
  </div>
  {% endif %}

  <ul class="flex-1 overflow-y-auto" data-rail-list="1" role="listbox">
    {% for a in accounts %}
    {% set name = a.name if kind == "customers" else a.display_name %}
    <li>
      <a href="{{ push_url_base }}?account_id={{ a.id }}"
         hx-get="{{ detail_url_base }}/{{ a.id }}?pane_target={{ pane_target|urlencode }}&push_url_base={{ push_url_base|urlencode }}"
         hx-target="{{ pane_target }}"
         hx-push-url="{{ push_url_base }}?account_id={{ a.id }}"
         data-rail-row="{{ a.id }}"
         role="option"
         {% if a.id == selected_id %}aria-current="true"{% endif %}
         class="flex items-center justify-between gap-2 px-3 py-1.5 text-sm cursor-pointer
                hover:bg-brand-50
                {% if a.id == selected_id %}bg-brand-50{% endif %}">
        <div class="flex items-center gap-2 min-w-0">
          <span class="w-2 h-2 rounded-full flex-shrink-0
                       {{ dot_colors.get(a.staleness, 'bg-brand-300') }}"></span>
          <span class="truncate text-gray-900">{{ name }}</span>
        </div>
        <span class="text-[11px] text-gray-500 flex-shrink-0">
          {% if a.last_activity_at %}{{ a.last_activity_at|timeago }}{% else %}—{% endif %}
        </span>
      </a>
    </li>
    {% else %}
    <li class="px-3 py-6 text-xs text-gray-400 text-center">
      No accounts match.
    </li>
    {% endfor %}
  </ul>
</div>
```

The `crmRailKeyboard` Alpine component is defined in Task 17. Until then, the rail will simply not respond to keyboard — clicks still work.

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/partials/crm/_rail.html
git commit -m "feat(crm): _rail partial — single-line rows + Needs Attention band

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: `email_composer.html` modal partial

**Files:**
- Create: `app/templates/htmx/partials/crm/email_composer.html`

- [ ] **Step 1: Create the file**

```html
{# email_composer.html — modal body for click-to-email.
   Receives: to_email, to_name, post_url, signature.
   Called by: GET /v2/crm/contacts/{id}/email-composer (and vendor variant).
   Depends on: existing modal mount (#modal) + closeModal HX-Trigger handler.
#}

<div class="bg-white rounded-lg shadow-xl max-w-2xl w-full mx-auto p-6"
     x-data="{ inlineError: '' }">
  <div class="flex items-center justify-between mb-4">
    <h2 class="text-lg font-semibold text-gray-900">Send email</h2>
    <button type="button" @click="$store.modal.close()"
            class="text-gray-400 hover:text-gray-600 text-2xl leading-none">&times;</button>
  </div>

  <form hx-post="{{ post_url }}"
        hx-swap="none"
        @htmx:response-error="inlineError = $event.detail.xhr.responseText"
        class="space-y-3">
    <div class="text-sm text-gray-700">
      <span class="font-medium">To:</span>
      {{ to_email }}{% if to_name %} <span class="text-gray-400">({{ to_name }})</span>{% endif %}
    </div>
    <input aria-label="Subject" type="text" name="subject" required placeholder="Subject"
           class="w-full px-3 py-2 border border-gray-300 rounded text-sm
                  focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
    <textarea aria-label="Body" name="body" rows="10" required
              class="w-full px-3 py-2 border border-gray-300 rounded text-sm
                     focus:ring-2 focus:ring-brand-500 focus:border-brand-500"
              placeholder="Write your message…">

{{ signature }}</textarea>

    <p x-show="inlineError" x-text="inlineError"
       class="text-xs text-rose-600"></p>

    <div class="flex justify-end gap-2 pt-2">
      <button type="button" @click="$store.modal.close()"
              class="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50">
        Cancel
      </button>
      <button type="submit"
              class="px-4 py-1.5 text-sm font-medium text-white bg-brand-500 rounded
                     hover:bg-brand-600">
        Send
      </button>
    </div>
  </form>
</div>
```

The `closeModal` HX-Trigger fired by the send-email endpoint must be wired to `$store.modal.close()`. If the existing modal store doesn't already listen for `closeModal`, add a handler in `app/static/htmx_app.js` (the bootstrap):

```js
document.body.addEventListener("closeModal", () => Alpine.store("modal").close());
```

Confirm whether this wiring exists by grepping `htmx_app.js` for `closeModal`. If absent, add it and update the commit message.

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/partials/crm/email_composer.html app/static/htmx_app.js
git commit -m "feat(crm): email composer modal + closeModal HX-Trigger handler

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Customer workspace template + routes

**Files:**
- Create: `app/templates/htmx/partials/crm/customers_workspace.html`
- Modify: `app/routers/crm/views.py`
- Modify: `app/routers/crm/_helpers.py` (already done in Task 4)
- Test: `tests/test_crm_workspace.py`

The route at `/v2/partials/customers` is currently in `htmx_views.py::companies_list_partial`. We're going to **leave that handler untouched** for this task and instead register the new workspace handler at a *different* path first, run tests, and only swap URLs at the end (in Task 18 after delete-list-template). Reason: avoids breaking the CRM shell mid-refactor.

Phase A path (this task): `/v2/partials/crm/customers/workspace` and `/v2/partials/crm/customers/rail`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_crm_workspace.py`:

```python
"""Tests for the CRM customer workspace + rail endpoints (Phase A path)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Company, User


@pytest.fixture()
def make_company(db_session):
    def _f(name, owner=None, last_activity_days_ago=None, is_active=True):
        co = Company(name=name, is_active=is_active)
        if owner is not None:
            co.account_owner_id = owner.id
        if last_activity_days_ago is not None:
            co.last_activity_at = datetime.now(timezone.utc) - timedelta(days=last_activity_days_ago)
        db_session.add(co)
        db_session.commit()
        return co

    return _f


def test_workspace_renders_rail_and_pane(client):
    r = client.get("/v2/partials/crm/customers/workspace")
    assert r.status_code == 200
    assert 'id="crm-rail"' in r.text
    assert 'id="crm-pane"' in r.text


def test_workspace_default_sort_is_overdue_first(client, db_session, test_user, make_company):
    fresh = make_company("Fresh Co", owner=test_user, last_activity_days_ago=2)
    overdue = make_company("Overdue Co", owner=test_user, last_activity_days_ago=60)
    r = client.get("/v2/partials/crm/customers/workspace")
    fresh_pos = r.text.find("Fresh Co")
    overdue_pos = r.text.find("Overdue Co")
    assert overdue_pos < fresh_pos  # overdue comes first


def test_workspace_filters_by_my_only_for_manager(client, db_session, make_company):
    from app.dependencies import require_user
    from app.main import app

    mgr = User(email="m@x", name="M", role="manager")
    other = User(email="o@x", name="O", role="sales")
    db_session.add_all([mgr, other])
    db_session.commit()
    make_company("Mine", owner=mgr, last_activity_days_ago=1)
    make_company("Theirs", owner=other, last_activity_days_ago=1)

    app.dependency_overrides[require_user] = lambda: mgr
    try:
        r = client.get("/v2/partials/crm/customers/workspace?my_only=1")
        assert "Mine" in r.text
        assert "Theirs" not in r.text
    finally:
        app.dependency_overrides[require_user] = lambda: client._test_user  # restored by client fixture


def test_workspace_scopes_to_user_for_non_manager(client, db_session, make_company, test_user):
    other = User(email="o@x", name="O", role="sales")
    db_session.add(other)
    db_session.commit()
    make_company("Mine", owner=test_user, last_activity_days_ago=1)
    make_company("Theirs", owner=other, last_activity_days_ago=1)

    r = client.get("/v2/partials/crm/customers/workspace")
    assert "Mine" in r.text
    assert "Theirs" not in r.text


def test_rail_endpoint_returns_only_rail_html(client):
    r = client.get("/v2/partials/crm/customers/rail")
    assert r.status_code == 200
    assert 'id="crm-rail"' in r.text
    assert 'id="crm-pane"' not in r.text


def test_workspace_loads_pane_when_account_id_present(client, db_session, test_user, make_company):
    co = make_company("Acme", owner=test_user, last_activity_days_ago=5)
    r = client.get(f"/v2/partials/crm/customers/workspace?account_id={co.id}")
    assert r.status_code == 200
    assert 'id="crm-rail"' in r.text
    assert 'aria-current="true"' in r.text  # row highlighted
    assert "Acme" in r.text


def test_workspace_needs_attention_band_renders_when_overdue(client, db_session, test_user, make_company):
    make_company("Old1", owner=test_user, last_activity_days_ago=60)
    make_company("Old2", owner=test_user, last_activity_days_ago=45)
    make_company("New1", owner=test_user, last_activity_days_ago=2)

    r = client.get("/v2/partials/crm/customers/workspace")
    assert "Needs Attention" in r.text
    assert "2 overdue" in r.text


def test_workspace_needs_attention_hidden_when_filtered_to_overdue(
    client, db_session, test_user, make_company
):
    make_company("Old", owner=test_user, last_activity_days_ago=60)
    r = client.get("/v2/partials/crm/customers/workspace?staleness=overdue")
    assert "Needs Attention" not in r.text


def test_workspace_search_filters_results(client, db_session, test_user, make_company):
    make_company("Acme Industries", owner=test_user, last_activity_days_ago=5)
    make_company("Beta Manufacturing", owner=test_user, last_activity_days_ago=5)
    r = client.get("/v2/partials/crm/customers/workspace?search=acme")
    assert "Acme" in r.text
    assert "Beta" not in r.text
```

The `client._test_user` reference assumes the conftest stores the test user on the client. Check `tests/conftest.py:222-258` — if it doesn't, capture the original override and restore explicitly (the dependency override is set per-test by the fixture; just remove it after).

- [ ] **Step 2: Run tests to verify failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_workspace.py -v --override-ini="addopts=" 2>&1 | tail -30
```

Expected: 9 failures with 404s.

- [ ] **Step 3: Build the workspace template**

Create `app/templates/htmx/partials/crm/customers_workspace.html`:

```html
{# customers_workspace.html — split-screen layout for Customers tab.
   Receives: accounts, selected_id, overdue_count, search, staleness, sort,
             my_only, is_manager, focused_company_html (optional).
   Called by: GET /v2/partials/crm/customers/workspace.
   Depends on: _rail.html, _rail_controls.html.
#}

<div id="crm-workspace"
     class="flex h-[calc(100vh-220px)] min-h-[480px] -mx-3"
     data-kind="customers">
  {% with
     kind="customers",
     workspace_url_base="/v2/partials/crm/customers/rail",
     pane_target="#crm-pane",
     push_url_base="/v2/crm/customers",
     detail_url_base="/v2/partials/customers",
     target_id="#crm-rail"
  %}
  {% include "htmx/partials/crm/_rail.html" %}
  {% endwith %}

  <div id="crm-pane" class="flex-1 overflow-y-auto px-4 py-2
                            {% if not selected_id %}hidden lg:block{% endif %}">
    {% if selected_id %}
      {# Mobile back button (lg:hidden) #}
      <button type="button"
              class="lg:hidden mb-2 text-sm text-brand-600 hover:text-brand-700"
              hx-get="/v2/partials/crm/customers/workspace"
              hx-target="#crm-workspace"
              hx-swap="outerHTML"
              hx-push-url="/v2/crm/customers">
        ← Back to accounts
      </button>
      {# Pane content already loaded server-side when account_id present #}
      {{ focused_company_html|safe if focused_company_html else "" }}
    {% else %}
      <div class="hidden lg:flex items-center justify-center h-full text-sm text-gray-400">
        Select an account to view details
      </div>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 4: Build the routes**

Open `app/routers/crm/views.py`. Add at the top of the file (with other imports):

```python
from datetime import datetime, timedelta, timezone

from fastapi import Query
from sqlalchemy import or_

from ...models import Company
from ...services.staleness import (
    STALENESS_DUE_SOON_DAYS,
    STALENESS_OVERDUE_DAYS,
    staleness_tier,
)
from ._helpers import scope_companies_to_user
```

Append the two new endpoints to `app/routers/crm/views.py`:

```python
def _customer_sort_clause(sort: str):
    if sort == "name_asc":
        return Company.name.asc()
    if sort == "created_desc":
        return Company.created_at.desc().nullslast()
    if sort == "recently_contacted":
        return Company.last_activity_at.desc().nullslast()
    # default: most_overdue
    return Company.last_activity_at.asc().nullsfirst()


def _apply_staleness_filter(query, staleness: str):
    if staleness == "all" or not staleness:
        return query
    now = datetime.now(timezone.utc)
    if staleness == "new":
        return query.filter(Company.last_activity_at.is_(None))
    if staleness == "overdue":
        return query.filter(
            Company.last_activity_at < now - timedelta(days=STALENESS_OVERDUE_DAYS)
        )
    if staleness == "due_soon":
        return query.filter(
            Company.last_activity_at < now - timedelta(days=STALENESS_DUE_SOON_DAYS),
            Company.last_activity_at >= now - timedelta(days=STALENESS_OVERDUE_DAYS),
        )
    if staleness == "recent":
        return query.filter(
            Company.last_activity_at >= now - timedelta(days=STALENESS_DUE_SOON_DAYS)
        )
    return query


def _build_customer_rail_context(
    *,
    db: Session,
    user: User,
    search: str,
    staleness: str,
    sort: str,
    my_only: bool,
    selected_id: int | None,
):
    """Shared query-and-context builder for workspace + rail endpoints."""
    is_manager = user.role in ("manager", "admin")
    base_q = db.query(Company).filter(Company.is_active.is_(True))

    if my_only or not is_manager:
        base_q = base_q.filter(Company.account_owner_id == user.id)
    else:
        base_q = scope_companies_to_user(base_q, user)

    if search.strip():
        term = f"%{search.strip()}%"
        base_q = base_q.filter(Company.name.ilike(term))

    filtered_q = _apply_staleness_filter(base_q, staleness)
    accounts = filtered_q.order_by(_customer_sort_clause(sort)).limit(200).all()
    for c in accounts:
        c.staleness = staleness_tier(c.last_activity_at)

    overdue_count = 0
    if staleness != "overdue":
        now = datetime.now(timezone.utc)
        overdue_count = base_q.filter(
            or_(
                Company.last_activity_at < now - timedelta(days=STALENESS_OVERDUE_DAYS),
                Company.last_activity_at.is_(None),
            )
        ).count()

    return {
        "accounts": accounts,
        "overdue_count": overdue_count,
        "selected_id": selected_id,
        "search": search,
        "staleness": staleness or "all",
        "sort": sort or "most_overdue",
        "my_only": my_only,
        "is_manager": is_manager,
    }


@router.get("/v2/partials/crm/customers/workspace", response_class=HTMLResponse)
async def customers_workspace(
    request: Request,
    account_id: int | None = Query(None),
    search: str = "",
    staleness: str = "all",
    sort: str = "most_overdue",
    my_only: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the customer workspace (rail + pane)."""
    from ...template_env import templates

    ctx = _build_customer_rail_context(
        db=db, user=user, search=search, staleness=staleness, sort=sort,
        my_only=my_only, selected_id=account_id,
    )

    focused_html = ""
    if account_id is not None:
        # Reuse the existing detail handler — call it as a function
        from ..htmx_views import company_detail_partial

        try:
            resp = await company_detail_partial(
                request=request, company_id=account_id, user=user, db=db
            )
            focused_html = resp.body.decode()
        except Exception as e:
            logger.warning("workspace: failed to load account_id={}: {}", account_id, e)

    ctx.update({"request": request, "user": user, "focused_company_html": focused_html})
    return templates.TemplateResponse("htmx/partials/crm/customers_workspace.html", ctx)


@router.get("/v2/partials/crm/customers/rail", response_class=HTMLResponse)
async def customers_rail(
    request: Request,
    search: str = "",
    staleness: str = "all",
    sort: str = "most_overdue",
    my_only: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return just the rail (used after sort/filter/search changes)."""
    from ...template_env import templates

    ctx = _build_customer_rail_context(
        db=db, user=user, search=search, staleness=staleness, sort=sort,
        my_only=my_only, selected_id=None,
    )
    ctx.update({
        "request": request, "user": user,
        "kind": "customers",
        "workspace_url_base": "/v2/partials/crm/customers/rail",
        "pane_target": "#crm-pane",
        "push_url_base": "/v2/crm/customers",
        "detail_url_base": "/v2/partials/customers",
        "target_id": "#crm-rail",
    })
    return templates.TemplateResponse("htmx/partials/crm/_rail.html", ctx)
```

Add to imports at the top (if missing): `from sqlalchemy.orm import Session`, `from ...dependencies import require_user`, `from ...database import get_db`, `from fastapi import Request`, etc.

- [ ] **Step 5: Run tests to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_workspace.py -v --override-ini="addopts="
```

Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add app/routers/crm/views.py app/templates/htmx/partials/crm/customers_workspace.html tests/test_crm_workspace.py
git commit -m "$(cat <<'EOF'
feat(crm): customer workspace + rail endpoints (Phase A path)

Workspace renders rail + pane shell; rail endpoint re-renders just the
rail body when filters/search/sort change. Both honor scope (managers
see all, others see own) and the my_only toggle. Default sort is
most-overdue. Needs Attention band shows overdue count and links to
filter. Account_id query param auto-loads the focused detail into the
pane via the existing company_detail_partial.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Vendor workspace template + routes

**Files:**
- Create: `app/templates/htmx/partials/crm/vendors_workspace.html`
- Modify: `app/routers/crm/views.py`
- Test: `tests/test_crm_workspace.py` (append vendor cases)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_crm_workspace.py`:

```python
@pytest.fixture()
def make_vendor(db_session):
    def _f(name, last_activity_days_ago=None, sighting_count=0, engagement_score=None):
        from app.models import VendorCard

        v = VendorCard(
            normalized_name=name.lower().replace(" ", "_"),
            display_name=name,
            sighting_count=sighting_count,
            engagement_score=engagement_score,
        )
        if last_activity_days_ago is not None:
            v.last_activity_at = datetime.now(timezone.utc) - timedelta(days=last_activity_days_ago)
        db_session.add(v)
        db_session.commit()
        return v

    return _f


def test_vendor_workspace_renders_rail_and_pane(client):
    r = client.get("/v2/partials/crm/vendors/workspace")
    assert r.status_code == 200
    assert 'id="crm-rail"' in r.text
    assert 'id="crm-pane"' in r.text


def test_vendor_workspace_no_my_only_toggle(client):
    r = client.get("/v2/partials/crm/vendors/workspace")
    assert "My accounts" not in r.text


def test_vendor_workspace_default_sort_recently_active(client, db_session, make_vendor):
    make_vendor("RecentVen", last_activity_days_ago=1)
    make_vendor("OldVen", last_activity_days_ago=120)
    r = client.get("/v2/partials/crm/vendors/workspace")
    recent_pos = r.text.find("RecentVen")
    old_pos = r.text.find("OldVen")
    assert recent_pos < old_pos


def test_vendor_workspace_open_to_non_manager(client, make_vendor, test_user, db_session):
    make_vendor("VendorOne", last_activity_days_ago=2)
    # test_user is a default 'sales' user; vendors visible to all
    r = client.get("/v2/partials/crm/vendors/workspace")
    assert "VendorOne" in r.text


def test_vendor_workspace_no_needs_attention_band(client, db_session, make_vendor):
    make_vendor("OldVen", last_activity_days_ago=120)
    r = client.get("/v2/partials/crm/vendors/workspace")
    assert "Needs Attention" not in r.text
```

- [ ] **Step 2: Verify failures**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_workspace.py -k "vendor" -v --override-ini="addopts="
```

Expected: 5 failures.

- [ ] **Step 3: Create the vendor workspace template**

Create `app/templates/htmx/partials/crm/vendors_workspace.html`:

```html
{# vendors_workspace.html — split-screen layout for Vendors tab.
   Receives: accounts (VendorCard list), selected_id, search, staleness,
             sort, focused_vendor_html (optional).
   Called by: GET /v2/partials/crm/vendors/workspace.
#}

<div id="crm-workspace"
     class="flex h-[calc(100vh-220px)] min-h-[480px] -mx-3"
     data-kind="vendors">
  {% with
     kind="vendors",
     workspace_url_base="/v2/partials/crm/vendors/rail",
     pane_target="#crm-pane",
     push_url_base="/v2/crm/vendors",
     detail_url_base="/v2/partials/vendors",
     target_id="#crm-rail",
     overdue_count=0,
     my_only=False,
     is_manager=False
  %}
  {% include "htmx/partials/crm/_rail.html" %}
  {% endwith %}

  <div id="crm-pane" class="flex-1 overflow-y-auto px-4 py-2
                            {% if not selected_id %}hidden lg:block{% endif %}">
    {% if selected_id %}
      <button type="button"
              class="lg:hidden mb-2 text-sm text-brand-600 hover:text-brand-700"
              hx-get="/v2/partials/crm/vendors/workspace"
              hx-target="#crm-workspace"
              hx-swap="outerHTML"
              hx-push-url="/v2/crm/vendors">
        ← Back to vendors
      </button>
      {{ focused_vendor_html|safe if focused_vendor_html else "" }}
    {% else %}
      <div class="hidden lg:flex items-center justify-center h-full text-sm text-gray-400">
        Select a vendor to view details
      </div>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 4: Add the vendor routes to views.py**

Append to `app/routers/crm/views.py`:

```python
from ...models import VendorCard


def _vendor_sort_clause(sort: str):
    if sort == "name_asc":
        return VendorCard.display_name.asc()
    if sort == "engagement_desc":
        return VendorCard.engagement_score.desc().nullslast()
    if sort == "sighting_desc":
        return VendorCard.sighting_count.desc().nullslast()
    return VendorCard.last_activity_at.desc().nullslast()


def _apply_vendor_staleness_filter(query, staleness: str):
    if staleness == "all" or not staleness:
        return query
    now = datetime.now(timezone.utc)
    if staleness == "new":
        return query.filter(VendorCard.last_activity_at.is_(None))
    if staleness == "overdue":
        return query.filter(
            VendorCard.last_activity_at < now - timedelta(days=STALENESS_OVERDUE_DAYS)
        )
    if staleness == "due_soon":
        return query.filter(
            VendorCard.last_activity_at < now - timedelta(days=STALENESS_DUE_SOON_DAYS),
            VendorCard.last_activity_at >= now - timedelta(days=STALENESS_OVERDUE_DAYS),
        )
    if staleness == "recent":
        return query.filter(
            VendorCard.last_activity_at >= now - timedelta(days=STALENESS_DUE_SOON_DAYS)
        )
    return query


def _build_vendor_rail_context(
    *,
    db: Session,
    search: str,
    staleness: str,
    sort: str,
    selected_id: int | None,
):
    base_q = db.query(VendorCard).filter(VendorCard.is_blacklisted.is_(False))
    if search.strip():
        term = f"%{search.strip()}%"
        base_q = base_q.filter(VendorCard.display_name.ilike(term))
    filtered_q = _apply_vendor_staleness_filter(base_q, staleness)
    accounts = filtered_q.order_by(_vendor_sort_clause(sort)).limit(200).all()
    for v in accounts:
        v.staleness = staleness_tier(v.last_activity_at)
    return {
        "accounts": accounts,
        "overdue_count": 0,  # vendors have no Needs Attention band
        "selected_id": selected_id,
        "search": search,
        "staleness": staleness or "all",
        "sort": sort or "recently_active",
        "my_only": False,
        "is_manager": False,
    }


@router.get("/v2/partials/crm/vendors/workspace", response_class=HTMLResponse)
async def vendors_workspace(
    request: Request,
    account_id: int | None = Query(None),
    search: str = "",
    staleness: str = "all",
    sort: str = "recently_active",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...template_env import templates

    ctx = _build_vendor_rail_context(
        db=db, search=search, staleness=staleness, sort=sort, selected_id=account_id,
    )
    focused_html = ""
    if account_id is not None:
        from ..htmx_views import vendor_detail_partial

        try:
            resp = await vendor_detail_partial(
                request=request, vendor_id=account_id, user=user, db=db
            )
            focused_html = resp.body.decode()
        except Exception as e:
            logger.warning("vendor workspace: failed to load id={}: {}", account_id, e)

    ctx.update({"request": request, "user": user, "focused_vendor_html": focused_html})
    return templates.TemplateResponse("htmx/partials/crm/vendors_workspace.html", ctx)


@router.get("/v2/partials/crm/vendors/rail", response_class=HTMLResponse)
async def vendors_rail(
    request: Request,
    search: str = "",
    staleness: str = "all",
    sort: str = "recently_active",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...template_env import templates

    ctx = _build_vendor_rail_context(
        db=db, search=search, staleness=staleness, sort=sort, selected_id=None,
    )
    ctx.update({
        "request": request, "user": user,
        "kind": "vendors",
        "workspace_url_base": "/v2/partials/crm/vendors/rail",
        "pane_target": "#crm-pane",
        "push_url_base": "/v2/crm/vendors",
        "detail_url_base": "/v2/partials/vendors",
        "target_id": "#crm-rail",
    })
    return templates.TemplateResponse("htmx/partials/crm/_rail.html", ctx)
```

- [ ] **Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_workspace.py -v --override-ini="addopts="
```

Expected: all (customer + vendor) tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/routers/crm/views.py app/templates/htmx/partials/crm/vendors_workspace.html tests/test_crm_workspace.py
git commit -m "feat(crm): vendor workspace + rail endpoints

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Add click-to-call/email buttons to detail templates

**Files:**
- Modify: `app/templates/htmx/partials/customers/detail.html`
- Modify: `app/templates/htmx/partials/customers/tabs/site_contacts.html`
- Modify: `app/templates/htmx/partials/customers/tabs/site_card.html`
- Modify: `app/templates/htmx/partials/vendors/detail.html`
- Modify: `app/templates/htmx/partials/vendors/tabs/contacts.html`
- Modify: `app/templates/htmx/partials/vendors/tabs/contact_row.html`
- Modify: `app/templates/htmx/partials/vendors/contact_nudges.html`

**Pattern to apply everywhere a contact appears:**

Replace each `<a href="tel:{{ c.phone }}">` with:

```html
{% if c.phone and user.eightxeight_extension %}
<button type="button"
        hx-post="/v2/crm/contacts/{{ c.id }}/dial"
        hx-swap="none"
        title="Call {{ c.full_name }}"
        class="text-brand-500 hover:text-brand-600">
  ☎ {{ c.phone }}
</button>
{% elif c.phone %}
<span class="text-gray-400" title="Configure your 8x8 extension to enable click-to-dial">
  ☎ {{ c.phone }}
</span>
{% endif %}
```

For vendors, replace `/contacts/` with `/vendor-contacts/` in the URL.

Replace each `<a href="mailto:{{ c.email }}">` with:

```html
{% if c.email %}
<button type="button"
        hx-get="/v2/crm/contacts/{{ c.id }}/email-composer"
        hx-target="#modal"
        hx-swap="innerHTML"
        @click="$store.modal.open()"
        class="text-brand-500 hover:text-brand-600">
  ✉ {{ c.email }}
</button>
{% endif %}
```

Vendor variant: `/vendor-contacts/{{ c.id }}/email-composer`.

- [ ] **Step 1: Smoke-render test for detail templates**

Add to `tests/test_crm_workspace.py`:

```python
def test_company_detail_renders_call_button_when_extension_set(
    client, db_session, test_user, make_company
):
    from app.models import CustomerSite, SiteContact

    test_user.eightxeight_extension = "4242"
    co = make_company("HasContact", owner=test_user, last_activity_days_ago=1)
    site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.commit()
    sc = SiteContact(site_id=site.id, full_name="A", email="a@a", phone="+15555550100")
    db_session.add(sc)
    db_session.commit()

    r = client.get(f"/v2/partials/customers/{co.id}")
    assert "/v2/crm/contacts/" in r.text
    assert "/dial" in r.text


def test_company_detail_renders_disabled_call_when_extension_unset(
    client, db_session, test_user, make_company
):
    from app.models import CustomerSite, SiteContact

    test_user.eightxeight_extension = None
    co = make_company("HasContact", owner=test_user, last_activity_days_ago=1)
    site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.commit()
    sc = SiteContact(site_id=site.id, full_name="A", email="a@a", phone="+15555550100")
    db_session.add(sc)
    db_session.commit()

    r = client.get(f"/v2/partials/customers/{co.id}")
    assert "Configure your 8x8 extension" in r.text or "text-gray-400" in r.text
```

- [ ] **Step 2: Run failing tests**

Expect failure.

- [ ] **Step 3: Apply the substitutions**

Open each of the seven template files. Find every `tel:` and `mailto:` anchor and replace per the pattern above. Be sure each replacement preserves the surrounding flex/grid layout classes.

For `vendors/contact_nudges.html` and `contact_row.html`, swap `c.id` for the vendor-contact id and use `/v2/crm/vendor-contacts/{{ c.id }}/...`.

In `customers/detail.html`, the primary contact card lives near the top — the current implementation may not have a click-to-call there yet; add buttons next to the contact's phone/email if present.

- [ ] **Step 4: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_workspace.py -v --override-ini="addopts="
```

Expected: pass.

- [ ] **Step 5: Smoke render every modified template via existing tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "detail or contact" -v --override-ini="addopts=" 2>&1 | tail -30
```

Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add app/templates/htmx/partials/customers/ app/templates/htmx/partials/vendors/ tests/test_crm_workspace.py
git commit -m "$(cat <<'EOF'
feat(crm): replace tel/mailto with HTMX click-to-call/email buttons

Customer + vendor detail templates and their contact tabs now invoke
the interactions router. Disabled-with-tooltip state when the user has
no eightxeight_extension. Email button opens the composer modal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: Enforce ownership scoping on `company_detail_partial`

**Files:**
- Modify: `app/routers/htmx_views.py` (function `company_detail_partial`, ~line 4472)
- Test: `tests/test_crm_workspace.py` (append)

- [ ] **Step 1: Append failing test**

```python
def test_company_detail_403_for_non_owner_non_manager(client, db_session, make_company):
    from app.dependencies import require_user
    from app.main import app

    other = User(email="o@x", name="O", role="sales")
    self_user = User(email="me@x", name="Me", role="sales")
    db_session.add_all([other, self_user])
    db_session.commit()
    co = make_company("Theirs", owner=other, last_activity_days_ago=1)
    app.dependency_overrides[require_user] = lambda: self_user
    try:
        r = client.get(f"/v2/partials/customers/{co.id}")
        assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_company_detail_visible_to_manager(client, db_session, make_company):
    from app.dependencies import require_user
    from app.main import app

    other = User(email="o2@x", name="O2", role="sales")
    mgr = User(email="m@x", name="Mgr", role="manager")
    db_session.add_all([other, mgr])
    db_session.commit()
    co = make_company("Theirs2", owner=other, last_activity_days_ago=1)
    app.dependency_overrides[require_user] = lambda: mgr
    try:
        r = client.get(f"/v2/partials/customers/{co.id}")
        assert r.status_code == 200
    finally:
        app.dependency_overrides.pop(require_user, None)
```

- [ ] **Step 2: Verify failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_workspace.py -k "403 or manager" -v --override-ini="addopts="
```

Expect 403 test fails (currently returns 200).

- [ ] **Step 3: Modify `company_detail_partial`**

Open `app/routers/htmx_views.py` at the `company_detail_partial` function. After the `company is not found` check, add:

```python
    if user.role not in ("manager", "admin") and company.account_owner_id != user.id:
        raise HTTPException(403, "Not authorized for this account")
```

Also accept the new optional pane params (used by inline-loaded detail in the workspace):

```python
    pane_target: str = Query("#main-content", alias="pane_target"),
    push_url_base: str = Query("/v2/customers", alias="push_url_base"),
```

Pass them into `ctx`:

```python
    ctx.update({"pane_target": pane_target, "push_url_base": push_url_base, ...})
```

Then in `customers/detail.html`, anywhere there's an `hx-target="#main-content"` on a tab/insights call, replace with `hx-target="{{ pane_target|default('#main-content') }}"`. Same for `hx-push-url`.

- [ ] **Step 4: Verify tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_workspace.py -v --override-ini="addopts="
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/customers/detail.html tests/test_crm_workspace.py
git commit -m "$(cat <<'EOF'
fix(crm): enforce ownership scoping on /v2/partials/customers/{id}

Non-managers loading another rep's account by URL now get 403. Detail
template also accepts pane_target / push_url_base for embedding inside
the workspace (existing behavior preserved by defaulting to current
values).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Mobile collapse + back button polish

**Files:**
- Modify: `app/templates/htmx/partials/crm/customers_workspace.html`
- Modify: `app/templates/htmx/partials/crm/vendors_workspace.html`
- Modify: `app/templates/htmx/partials/crm/_rail.html`

The workspace templates already include conditional `hidden lg:flex` / `hidden lg:block` classes. This task verifies and tightens the responsive behavior.

- [ ] **Step 1: Manual responsive smoke test (since automated viewport tests live in Playwright)**

Open the spec at multiple widths via curl:

```bash
docker compose up -d
curl -s http://localhost:8000/v2/partials/crm/customers/workspace?account_id=1 | grep -E "lg:flex|lg:block|lg:hidden" | head -10
```

Expected: rail uses `lg:flex`, pane uses `lg:block`, mobile back button uses `lg:hidden`. If any of those classes appear inverted, fix the templates.

- [ ] **Step 2: Visual check at the actual breakpoint**

Open the deployed staging URL on a phone-sized window (or DevTools responsive mode at 375px and 1280px). Confirm:
- 1280px: rail visible left, pane visible right; back button hidden.
- 375px without `account_id`: only rail visible.
- 375px with `account_id`: only pane visible; back button visible at top of pane.

If the responsive E2E spec is added in Task 18 it can replace this manual step; for now, verify by hand and capture the result in the commit message.

- [ ] **Step 3: Commit (only if any fixes were needed)**

If no fixes: skip to Task 16. If fixes: commit them.

---

### Task 16: Keyboard navigation Alpine component

**Files:**
- Modify: `app/static/htmx_app.js`
- Test: manual + (later) Playwright

- [ ] **Step 1: Add the Alpine component**

Open `app/static/htmx_app.js`. Find the section where `Alpine.data(...)` registrations happen. Add:

```js
Alpine.data("crmRailKeyboard", () => ({
  onKey(ev) {
    const tag = (ev.target?.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

    const list = this.$el.querySelector('[data-rail-list="1"]');
    if (!list) return;

    const rows = Array.from(list.querySelectorAll("[data-rail-row]"));
    if (rows.length === 0) return;
    const currentIdx = rows.findIndex((r) => r.getAttribute("aria-current") === "true");

    const move = (delta) => {
      const next = Math.max(0, Math.min(rows.length - 1, (currentIdx === -1 ? 0 : currentIdx) + delta));
      rows.forEach((r) => r.removeAttribute("aria-current"));
      rows[next].setAttribute("aria-current", "true");
      rows[next].scrollIntoView({ block: "nearest" });
    };

    if (ev.key === "ArrowDown" || ev.key === "j") {
      ev.preventDefault();
      move(1);
    } else if (ev.key === "ArrowUp" || ev.key === "k") {
      ev.preventDefault();
      move(-1);
    } else if (ev.key === "Enter") {
      const cur = rows.find((r) => r.getAttribute("aria-current") === "true");
      if (cur) {
        ev.preventDefault();
        cur.click();
      }
    } else if (ev.key === "/" && tag !== "INPUT") {
      ev.preventDefault();
      const search = this.$el.querySelector('[data-rail-search="1"]');
      if (search) search.focus();
    } else if (ev.key === "Escape") {
      const back = document.querySelector('#crm-pane [hx-push-url][class*="lg:hidden"]');
      if (back) back.click();
    }
  },
}));
```

- [ ] **Step 2: Build the bundle**

```bash
npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 3: Manual test**

Open the workspace, focus the rail (e.g. click an empty area), press ↓ several times — watch the active row change. Press Enter — pane loads. Press `/` — search focuses.

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_app.js app/static/dist/
git commit -m "feat(crm): keyboard navigation on the rail (↑/↓/j/k/Enter//, Esc)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 17: Wire CRM shell to workspace routes (URL swap)

**Files:**
- Modify: `app/routers/htmx_views.py` (`companies_list_partial`, `vendors_list_partial`)
- Modify: `app/templates/htmx/partials/crm/shell.html` (no behavior change, but verify the `hx-get` URL)

Now that the workspace routes exist and pass tests, swap the legacy URLs to render the workspace. The shell already calls `/v2/partials/customers` and `/v2/partials/vendors` — make those return the workspace.

- [ ] **Step 1: Replace `companies_list_partial` body**

Open `app/routers/htmx_views.py` at `companies_list_partial`. Replace its entire body with a delegate to the workspace:

```python
async def companies_list_partial(
    request: Request,
    account_id: int | None = Query(None),
    search: str = "",
    staleness: str = "all",
    sort: str = "most_overdue",
    my_only: bool = False,
    hx_target: str = Query("#main-content", alias="hx_target"),
    push_url_base: str = Query("/v2/customers", alias="push_url_base"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delegates to the CRM customer workspace partial."""
    from .crm.views import customers_workspace
    return await customers_workspace(
        request=request, account_id=account_id, search=search,
        staleness=staleness, sort=sort, my_only=my_only, user=user, db=db,
    )
```

`hx_target`/`push_url_base` are accepted for backward compat but ignored by the workspace (which uses fixed `#crm-pane`).

Same for `vendors_list_partial` — replace body with delegation to `vendors_workspace`.

- [ ] **Step 2: Run the full CRM test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_views.py tests/test_crm_workspace.py tests/test_crm_interactions.py tests/test_crm_helpers.py tests/services/ tests/test_routers.py -v --override-ini="addopts=" 2>&1 | tail -25
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add app/routers/htmx_views.py
git commit -m "$(cat <<'EOF'
refactor(crm): delegate /v2/partials/customers and /v2/partials/vendors to workspace

The CRM shell already routes here; this swaps the response from the
legacy flat list templates to the new workspace partials. Old list
templates now unused — deleted in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 18: Delete obsolete list templates + add env vars

**Files:**
- Delete: `app/templates/htmx/partials/customers/list.html`
- Delete: `app/templates/htmx/partials/vendors/list.html`
- Modify: `.env.example`

- [ ] **Step 1: Confirm no lingering references**

```bash
grep -rE "customers/list\.html|vendors/list\.html" app/ --include="*.py" --include="*.html" 2>/dev/null
```

Expected: no matches. If any appear, fix the references first.

- [ ] **Step 2: Delete the files**

```bash
git rm app/templates/htmx/partials/customers/list.html app/templates/htmx/partials/vendors/list.html
```

- [ ] **Step 3: Add env vars**

Open `.env.example`. Append (group with other integration vars):

```
# 8x8 Work — click-to-dial + call-event webhook
EIGHTXEIGHT_API_BASE_URL=
EIGHTXEIGHT_API_KEY=
EIGHTXEIGHT_WEBHOOK_SECRET=
```

- [ ] **Step 4: Run full test suite to confirm nothing broken**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --override-ini="addopts=" -q 2>&1 | tail -10
```

Expected: pass count unchanged from baseline (Task 1) plus the new tests added in Tasks 2-14.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/customers/list.html app/templates/htmx/partials/vendors/list.html .env.example
git commit -m "chore(crm): delete legacy list templates + add 8x8 env vars

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: Playwright E2E spec

**Files:**
- Create: `tests/e2e/crm-split-screen.spec.ts`

- [ ] **Step 1: Write the spec**

```ts
// tests/e2e/crm-split-screen.spec.ts
import { test, expect } from '@playwright/test';

test.describe('CRM split-screen workspace', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/v2/crm');
  });

  test('rail renders and pane shows empty-state on desktop', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await expect(page.locator('#crm-rail')).toBeVisible();
    await expect(page.locator('#crm-pane')).toContainText(/Select an account/);
  });

  test('clicking a row loads the detail into the pane', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    const firstRow = page.locator('[data-rail-row]').first();
    const name = await firstRow.locator('span.truncate').textContent();
    await firstRow.click();
    await expect(page.locator('#crm-pane')).toContainText(name!);
    await expect(firstRow).toHaveAttribute('aria-current', 'true');
  });

  test('mobile collapses to single pane and back button returns to rail', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await expect(page.locator('#crm-rail')).toBeVisible();
    await expect(page.locator('#crm-pane')).toBeHidden();
    await page.locator('[data-rail-row]').first().click();
    await expect(page.locator('#crm-rail')).toBeHidden();
    await expect(page.locator('#crm-pane')).toBeVisible();
    await page.getByRole('button', { name: /Back to accounts/ }).click();
    await expect(page.locator('#crm-rail')).toBeVisible();
  });

  test('staleness chip filters the rail', async ({ page }) => {
    await page.getByRole('button', { name: 'Overdue' }).click();
    // Either rows render with rose dots, or empty state
    await expect(page.locator('#crm-rail')).toBeVisible();
  });

  test('search narrows the rail', async ({ page }) => {
    await page.locator('[data-rail-search]').fill('xyz-nonexistent');
    await expect(page.locator('#crm-rail')).toContainText(/No accounts match/i);
  });
});
```

- [ ] **Step 2: Run the spec**

```bash
npx playwright test tests/e2e/crm-split-screen.spec.ts --project=workflows 2>&1 | tail -10
```

Expected: 5 passes (when run against staging or a local dev server).

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/crm-split-screen.spec.ts
git commit -m "test(crm): Playwright E2E for split-screen workspace

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 20: Pre-PR full pipeline + open the stacked PR

**Files:** none modified. CI / PR work only.

- [ ] **Step 1: Run the full Python test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --override-ini="addopts=" 2>&1 | tail -20
```

Expected: green or known-stable failure list (capture in PR description if any pre-existing fails remain).

- [ ] **Step 2: Run linters**

```bash
ruff check app/ tests/
ruff format --check app/ tests/
mypy app/
```

Expected: clean. Fix any issues.

- [ ] **Step 3: Run pre-commit on all touched files**

```bash
pre-commit run --files \
  $(git diff --name-only origin/fix/ci-unblock-alembic-and-audit...HEAD) 2>&1 | tail -10
```

Per `feedback_pre_commit_all_files.md`, also run on the full repo before pushing a big PR:

```bash
pre-commit run --all-files 2>&1 | tail -15
```

- [ ] **Step 4: Build the frontend bundle**

```bash
npm run build 2>&1 | tail -10
```

- [ ] **Step 5: Manual smoke on the dev server**

```bash
docker compose up -d --build
sleep 8
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v2/partials/crm/shell
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v2/partials/crm/customers/workspace
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v2/partials/crm/vendors/workspace
```

Expected: 200 / 200 / 200 (or 401 if no auth — exercise via the browser).

- [ ] **Step 6: Push the branch**

```bash
git push -u origin feat/crm-split-screen
```

- [ ] **Step 7: Open the stacked PR**

```bash
gh pr create \
  --base fix/ci-unblock-alembic-and-audit \
  --head feat/crm-split-screen \
  --title "feat(crm): split-screen workspace + 8x8/Graph integrations" \
  --body "$(cat <<'EOF'
## Summary
- Persistent left-rail accounts list (Customers + Vendors), right-pane detail that swaps on click. Replaces the legacy flat list templates at the same URLs.
- 8x8 click-to-dial: click ☎ on any contact → 8x8 rings the rep's phone → ActivityLog row created with external_id → 8x8 webhook reconciles duration/recording.
- Graph send-email: click ✉ → composer modal pre-filled with user signature → /me/sendMail → ActivityLog row.
- Mobile: collapses to single pane below 1024px with a back button.
- Customer-side ownership scoping enforced server-side on rail + detail (managers see all, others see own); vendors are open.
- Single migration: `User.eightxeight_extension VARCHAR(50) NULL`.

## Stacked dependency
**This PR is stacked off `fix/ci-unblock-alembic-and-audit`.** Merges only after that PR lands on green main and this one is rebased.

## Test plan
- [ ] Full pytest suite green
- [ ] Playwright `tests/e2e/crm-split-screen.spec.ts` green
- [ ] Manual: rail filter + sort + click on staging
- [ ] Manual: mobile collapse + back at 375px
- [ ] Manual: click-to-call disabled when extension is null; error toast on 8x8 5xx
- [ ] Manual: composer opens, sends, closes, shows toast
- [ ] Manual: 8x8 webhook with bad HMAC returns 401
- [ ] Manual: non-manager direct-URL to another rep's account returns 403

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed. Capture and report back to the user.

- [ ] **Step 8: Mark plan complete**

No more code changes. Hand off to the user for review.

---

## Self-Review

**1. Spec coverage:**
- Workspace + rail layout (spec §"Architecture", §"Left Rail") → Tasks 8, 9, 11, 12.
- Right pane reuse + pane_target parameterization (§"Right Pane") → Tasks 13, 14.
- 8x8 integration (§"8x8 Click-to-Dial") → Tasks 5, 7. Webhook + idempotency covered.
- Graph send-email (§"Graph Send-Email") → Tasks 6, 7. Inline error path covered.
- Vendor parity (§"Vendor Side Parity") → Tasks 12, 13. No my_only, no scoping, sort defaults different.
- Mobile collapse (§"Mobile Collapse") → Tasks 11, 12, 15.
- Performance tab unchanged → no task (verified in shell).
- User.eightxeight_extension migration → Task 3.
- Permissions enforcement on detail endpoint → Task 14.
- Needs Attention band → Task 9 (rendered in `_rail.html`) + Task 11 (count computed in `_build_customer_rail_context`).
- Keyboard navigation → Task 16.
- E2E coverage → Task 19.

**2. Placeholder scan:** None. The single `<HEAD_REV>` placeholder in Task 3 is intentional (the value depends on the parallel 001 branch's eventual head) and the step explicitly tells the engineer how to obtain it.

**3. Type consistency:**
- `eightxeight_service.click_to_dial`, `verify_webhook`, `handle_call_event` — signatures consistent across Tasks 5, 7.
- `graph_send_service.send` — signature consistent across Tasks 6, 7.
- `scope_companies_to_user(query, user)` — signature consistent across Tasks 4, 11.
- `staleness_tier(dt)` — signature consistent across Tasks 2, 11, 12.
- ActivityLog field usage (`event_type`, `direction`, `external_id`, `auto_logged`, `subject`, `duration_seconds`, `details`, polymorphic FKs) consistent across Tasks 5, 6, 7.
- Rail template variables (`accounts`, `kind`, `selected_id`, `overdue_count`, `staleness`, `sort`, `pane_target`, `push_url_base`, `detail_url_base`, `workspace_url_base`, `target_id`, `is_manager`, `my_only`) consistent across Tasks 8, 9, 11, 12.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-30-crm-split-screen.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration. Tasks 2/3/4 are well-isolated TDD blocks; Tasks 5/6 are independent service modules; Tasks 7/11/12 are router-heavy and benefit from per-task review; Tasks 13-19 are integration/UI.

2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
