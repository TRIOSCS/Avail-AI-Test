# User Roles & Pre-Testing Hardening Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix role permissions so buyer and sales have correct access, add session idle timeout, and track last login — all before human testers arrive this week.

**Architecture:** Add a `require_sales` dependency that gates business-workflow actions (create reqs, quotes, buy plans) to sales/trader/manager/admin — blocking buyer. Update `require_buyer` to include sales. Add `last_login` column to User model. Add idle-timeout check in `require_user`. Hide Accounts + Contacts sidebar items from buyer role in frontend.

**Tech Stack:** FastAPI dependencies, SQLAlchemy, Alembic, Jinja2 templates, vanilla JS

---

### Task 1: Add `require_sales` Dependency + Update `require_buyer`

**Files:**
- Modify: `app/dependencies.py:83-88` (update require_buyer), add require_sales after it

**Step 1: Write the failing test**

Create `tests/test_role_permissions.py`:

```python
"""
test_role_permissions.py — Role-based access control tests

Tests that buyer cannot access sales-gated endpoints (create reqs,
quotes, buy plans) and that sales CAN access buyer-gated endpoints
(RFQs, sourcing).

Called by: pytest
Depends on: conftest fixtures, app.dependencies
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User


@pytest.fixture()
def buyer_user(db_session: Session) -> User:
    """A buyer-role user."""
    user = User(
        email="buyer@trioscs.com",
        name="Test Buyer",
        role="buyer",
        azure_id="test-azure-buyer",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def buyer_only_client(db_session: Session, buyer_user: User) -> TestClient:
    """TestClient authenticated as buyer — does NOT override require_sales."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: buyer_user
    app.dependency_overrides[require_buyer] = lambda: buyer_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def sales_full_client(db_session: Session, sales_user: User) -> TestClient:
    """TestClient authenticated as sales — overrides require_user and require_buyer."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: sales_user
    app.dependency_overrides[require_buyer] = lambda: sales_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestRequireSalesDependency:
    """Unit tests for require_sales dependency function."""

    def test_require_sales_allows_sales(self, db_session, sales_user):
        from app.dependencies import require_sales

        # Mock request with session containing user_id
        from unittest.mock import MagicMock

        request = MagicMock()
        request.session = {"user_id": sales_user.id}

        result = require_sales(request, db_session)
        assert result.id == sales_user.id

    def test_require_sales_allows_admin(self, db_session, admin_user):
        from app.dependencies import require_sales

        from unittest.mock import MagicMock

        request = MagicMock()
        request.session = {"user_id": admin_user.id}

        result = require_sales(request, db_session)
        assert result.id == admin_user.id

    def test_require_sales_allows_trader(self, db_session, trader_user):
        from app.dependencies import require_sales

        from unittest.mock import MagicMock

        request = MagicMock()
        request.session = {"user_id": trader_user.id}

        result = require_sales(request, db_session)
        assert result.id == trader_user.id

    def test_require_sales_allows_manager(self, db_session, manager_user):
        from app.dependencies import require_sales

        from unittest.mock import MagicMock

        request = MagicMock()
        request.session = {"user_id": manager_user.id}

        result = require_sales(request, db_session)
        assert result.id == manager_user.id

    def test_require_sales_blocks_buyer(self, db_session, buyer_user):
        from fastapi import HTTPException

        from app.dependencies import require_sales

        from unittest.mock import MagicMock

        request = MagicMock()
        request.session = {"user_id": buyer_user.id}

        with pytest.raises(HTTPException) as exc_info:
            require_sales(request, db_session)
        assert exc_info.value.status_code == 403


class TestRequireBuyerIncludesSales:
    """Verify require_buyer now allows sales role."""

    def test_require_buyer_allows_sales(self, db_session, sales_user):
        from app.dependencies import require_buyer

        from unittest.mock import MagicMock

        request = MagicMock()
        request.session = {"user_id": sales_user.id}

        result = require_buyer(request, db_session)
        assert result.id == sales_user.id
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py -v`
Expected: FAIL — `require_sales` does not exist yet

**Step 3: Write the implementation**

In `app/dependencies.py`, update `require_buyer` (line 83-88) to include `"sales"`:

```python
def require_buyer(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: requires buyer role for sourcing/RFQ actions."""
    user = require_user(request, db)
    if user.role not in ("buyer", "sales", "trader", "manager", "admin"):
        raise HTTPException(403, "Buyer role required for this action")
    return user
```

Add `require_sales` right after `require_buyer`:

```python
def require_sales(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: requires sales role for business workflow actions.

    Gates: create/edit requisitions, quotes, buy plans.
    Allows: sales, trader, manager, admin. Blocks: buyer.
    """
    user = require_user(request, db)
    if user.role not in ("sales", "trader", "manager", "admin"):
        raise HTTPException(403, "Sales role required for this action")
    return user
```

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/dependencies.py tests/test_role_permissions.py
git commit -m "feat: add require_sales dependency, include sales in require_buyer"
```

---

### Task 2: Guard Requisition Endpoints with `require_sales`

**Files:**
- Modify: `app/routers/requisitions/core.py:22,418,503` — import + swap `require_user` → `require_sales` on create/update
- Modify: `app/routers/requisitions/requirements.py:23,153,311,447,463` — import + swap on add/upload/delete/update requirements

**Step 1: Add tests to `tests/test_role_permissions.py`**

```python
class TestBuyerCannotCreateRequisitions:
    """Buyer role blocked from creating/editing requisitions."""

    def test_buyer_cannot_create_requisition(self, buyer_only_client):
        resp = buyer_only_client.post(
            "/api/requisitions",
            json={"name": "Test Req", "customer_name": "Acme"},
        )
        assert resp.status_code == 403

    def test_buyer_cannot_update_requisition(self, buyer_only_client, test_requisition):
        resp = buyer_only_client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"name": "Updated Name"},
        )
        assert resp.status_code == 403

    def test_buyer_cannot_add_requirements(self, buyer_only_client, test_requisition):
        resp = buyer_only_client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "LM317T", "target_qty": 100},
        )
        assert resp.status_code == 403

    def test_buyer_cannot_delete_requirement(self, buyer_only_client, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = buyer_only_client.delete(f"/api/requirements/{req_item.id}")
        assert resp.status_code == 403

    def test_buyer_cannot_update_requirement(self, buyer_only_client, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = buyer_only_client.put(
            f"/api/requirements/{req_item.id}",
            json={"target_qty": 2000},
        )
        assert resp.status_code == 403
```

**Step 2: Run tests — expect FAIL (buyer currently allowed)**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestBuyerCannotCreateRequisitions -v`

**Step 3: Implement the changes**

In `app/routers/requisitions/core.py`:
- Line 22: Add `require_sales` to import: `from ...dependencies import get_req_for_user, require_sales, require_user`
- Line 418: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `create_requisition`
- Line 503: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `update_requisition`

In `app/routers/requisitions/requirements.py`:
- Line 23: Add `require_sales` to import: `from ...dependencies import get_req_for_user, require_buyer, require_sales, require_user`
- Line 153: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `add_requirements`
- Line 311: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `upload_requirements`
- Line 447: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `delete_requirement`
- Line 463: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `update_requirement`

**Step 4: Run tests — expect PASS**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py -v`

**Step 5: Run full requisition tests to check for regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_rfq.py tests/test_requisition_flow.py -v`

Note: Existing tests use the `client` fixture which overrides `require_buyer` but NOT `require_sales`. You'll need to also override `require_sales` in any test fixture where a buyer-role user needs to create reqs for test setup. Update `conftest.py`'s `client` fixture to also override `require_sales`:

```python
# In conftest.py client fixture, add:
from app.dependencies import require_buyer, require_sales, require_user

app.dependency_overrides[require_sales] = _override_user  # test_user is a buyer but tests need full access
```

**Step 6: Commit**

```bash
git add app/routers/requisitions/core.py app/routers/requisitions/requirements.py tests/test_role_permissions.py tests/conftest.py
git commit -m "feat: guard requisition create/edit endpoints with require_sales"
```

---

### Task 3: Guard Quote Endpoints with `require_sales`

**Files:**
- Modify: `app/routers/crm/quotes.py:15,85,206,242` — import + swap on create/update/delete

**Step 1: Add tests to `tests/test_role_permissions.py`**

```python
class TestBuyerCannotManageQuotes:
    """Buyer role blocked from creating/editing/deleting quotes."""

    def test_buyer_cannot_create_quote(self, buyer_only_client, test_requisition, test_customer_site):
        # Link requisition to customer site first
        test_requisition.customer_site_id = test_customer_site.id
        resp = buyer_only_client.post(
            f"/api/requisitions/{test_requisition.id}/quote",
            json={"offer_ids": [], "line_items": []},
        )
        assert resp.status_code == 403

    def test_buyer_cannot_update_quote(self, buyer_only_client, test_quote):
        resp = buyer_only_client.put(
            f"/api/quotes/{test_quote.id}",
            json={"status": "draft"},
        )
        assert resp.status_code == 403

    def test_buyer_cannot_delete_quote(self, buyer_only_client, test_quote):
        test_quote.status = "draft"
        resp = buyer_only_client.delete(f"/api/quotes/{test_quote.id}")
        assert resp.status_code == 403
```

**Step 2: Run tests — expect FAIL**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestBuyerCannotManageQuotes -v`

**Step 3: Implement**

In `app/routers/crm/quotes.py`:
- Line 15: Change `from ...dependencies import require_user` → `from ...dependencies import require_sales, require_user`
- Line 85: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `create_quote`
- Line 206: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `update_quote`
- Line 242: Change `user: User = Depends(require_user)` → `user: User = Depends(require_sales)` on `delete_quote`

Also gate these quote workflow endpoints:
- Line 260: `preview_quote` → `require_sales`
- Line 280: `send_quote` → `require_sales`
- Line 345: `record_quote_result` → `require_sales`
- Line 396: `revise_quote` → `require_sales`
- Line 424: `reopen_quote` → `require_sales`

Leave `get_quote` (line 37) and `list_quotes` (line 60) and `pricing_history` (line 466) as `require_user` — buyer can view quotes but not create/edit.

**Step 4: Run tests — expect PASS**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py -v`

**Step 5: Run existing quote tests for regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quotes.py -v`

**Step 6: Commit**

```bash
git add app/routers/crm/quotes.py tests/test_role_permissions.py
git commit -m "feat: guard quote create/edit/delete endpoints with require_sales"
```

---

### Task 4: Guard Buy Plan Endpoints with `require_sales`

**Files:**
- Modify: `app/routers/crm/buy_plans.py:10,159,196,245` — import + swap on create/submit/full-create
- Modify: `app/routers/crm/buy_plans_v3.py:32,310,339` — import + swap on build/v3 create endpoints

**Step 1: Add tests to `tests/test_role_permissions.py`**

```python
class TestBuyerCannotManageBuyPlans:
    """Buyer role blocked from creating buy plans."""

    def test_buyer_cannot_create_buy_plan_draft(self, buyer_only_client, test_quote):
        resp = buyer_only_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan/draft",
            json={"offer_ids": [1]},
        )
        assert resp.status_code == 403

    def test_buyer_cannot_create_buy_plan_v3(self, buyer_only_client, test_quote):
        resp = buyer_only_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan-v3/build",
            json={},
        )
        assert resp.status_code == 403
```

**Step 2: Run tests — expect FAIL**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestBuyerCannotManageBuyPlans -v`

**Step 3: Implement**

In `app/routers/crm/buy_plans.py`:
- Line 10: Change `from ...dependencies import require_user` → `from ...dependencies import require_sales, require_user`
- Line 159: `create_buy_plan_draft` → `require_sales`
- Line 196: `submit_draft_buy_plan` → `require_sales`
- Line 245: `create_and_send_buy_plan` → `require_sales`

Leave approve/reject/po/complete/cancel endpoints as `require_user` — those are manager/admin workflows.

In `app/routers/crm/buy_plans_v3.py`:
- Line 32: Add `require_sales` to import
- Line 310: `build_buy_plan_v3` → `require_sales`
- Line 339: (if exists, the next create endpoint) → `require_sales`

Leave view/approve/verify endpoints as `require_user`.

**Step 4: Run tests — expect PASS**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py -v`

**Step 5: Run existing buy plan tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buy_plan_router.py tests/test_buy_plan_v3_router.py -v`

**Step 6: Commit**

```bash
git add app/routers/crm/buy_plans.py app/routers/crm/buy_plans_v3.py tests/test_role_permissions.py
git commit -m "feat: guard buy plan create/submit endpoints with require_sales"
```

---

### Task 5: Guard Company & Contact Endpoints from Buyer

**Files:**
- Modify: `app/routers/crm/companies.py:14` — import require_sales, swap on all endpoints
- Modify: `app/routers/crm/sites.py:11` — import require_sales, swap on all endpoints

**Step 1: Add tests to `tests/test_role_permissions.py`**

```python
class TestBuyerCannotAccessCustomerCRM:
    """Buyer role blocked from customer accounts and contacts sections."""

    def test_buyer_cannot_list_companies(self, buyer_only_client):
        resp = buyer_only_client.get("/api/companies")
        assert resp.status_code == 403

    def test_buyer_cannot_create_company(self, buyer_only_client):
        resp = buyer_only_client.post(
            "/api/companies",
            json={"name": "New Co", "website": "https://newco.com"},
        )
        assert resp.status_code == 403

    def test_buyer_cannot_list_sites(self, buyer_only_client, test_company):
        resp = buyer_only_client.get(f"/api/companies/{test_company.id}/sites")
        assert resp.status_code == 403

    def test_buyer_cannot_create_site(self, buyer_only_client, test_company):
        resp = buyer_only_client.post(
            f"/api/companies/{test_company.id}/sites",
            json={"site_name": "HQ", "contact_name": "Bob", "contact_email": "bob@co.com"},
        )
        assert resp.status_code == 403
```

**Step 2: Run tests — expect FAIL**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestBuyerCannotAccessCustomerCRM -v`

**Step 3: Implement**

In `app/routers/crm/companies.py`:
- Line 14: Change `from ...dependencies import require_user` → `from ...dependencies import require_sales, require_user`
- Change ALL `require_user` to `require_sales` on every endpoint (lines 50, 172, 203, 256, 372, 510, 528, 547)

In `app/routers/crm/sites.py`:
- Line 11: Change `from ...dependencies import require_user` → `from ...dependencies import require_sales, require_user`
  (keep `is_admin` import)
- Change ALL `require_user` to `require_sales` on every endpoint (lines 25, 64, 93, 195, 240, 271, 293, 316, 335, 364)

**Step 4: Run tests — expect PASS**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py -v`

**Step 5: Run existing company/site tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm_companies.py tests/test_crm_sites.py -v`

Note: These tests may use the `client` fixture (buyer role). Since we added `require_sales` override to the `client` fixture in Task 2, they should still pass. If any tests use their own fixtures, update those too.

**Step 6: Commit**

```bash
git add app/routers/crm/companies.py app/routers/crm/sites.py tests/test_role_permissions.py
git commit -m "feat: block buyer role from customer accounts and sites"
```

---

### Task 6: Hide Accounts + Contacts from Buyer in Frontend

**Files:**
- Modify: `app/static/app.js:946-950` — also hide navContacts for buyer role

**Step 1: Current code (line 946-950):**

```javascript
if (role === 'sales') {
    if (navVendors) navVendors.style.display = 'none';
} else if (role === 'buyer') {
    if (navCustomers) navCustomers.style.display = 'none';
}
```

**Step 2: Updated code:**

```javascript
if (role === 'sales') {
    if (navVendors) navVendors.style.display = 'none';
} else if (role === 'buyer') {
    if (navCustomers) navCustomers.style.display = 'none';
    if (navContacts) navContacts.style.display = 'none';
}
```

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "feat: hide Contacts sidebar item from buyer role"
```

---

### Task 7: Add `last_login` Column + Migration

**Files:**
- Modify: `app/models/auth.py:40` — add `last_login` column before `created_at`
- Create: `alembic/versions/051_add_last_login_to_users.py`
- Modify: `app/routers/auth.py:148` — set `last_login` on callback
- Modify: `app/routers/auth.py:202` — include `last_login` in auth status response

**Step 1: Add test**

Add to `tests/test_role_permissions.py`:

```python
class TestLastLogin:
    """Verify last_login is tracked on OAuth callback."""

    def test_user_model_has_last_login(self, db_session, test_user):
        """last_login column exists and defaults to None."""
        assert hasattr(test_user, "last_login")
        assert test_user.last_login is None

    def test_auth_status_includes_last_login(self, client):
        resp = client.get("/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        # last_login should be present in the response
        assert "last_login" in data
```

**Step 2: Run tests — expect FAIL**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestLastLogin -v`

**Step 3: Implement model change**

In `app/models/auth.py`, add after line 39 (after `working_hours_end`):

```python
    last_login = Column(DateTime)
```

**Step 4: Generate migration**

```bash
cd /root/availai && docker compose exec app alembic revision --autogenerate -m "add last_login to users"
```

If running locally without Docker:
```bash
cd /root/availai && PYTHONPATH=/root/availai alembic revision --autogenerate -m "add last_login to users"
```

Review the generated migration — should be a single `add_column('users', Column('last_login', DateTime))` with corresponding `drop_column` in downgrade.

**Step 5: Set last_login on OAuth callback**

In `app/routers/auth.py`, after line 148 (`db.commit()`), add:

```python
    user.last_login = datetime.now(timezone.utc)
    db.commit()
```

Actually, better: set it before the existing commit on line 148. Add after line 144 (`user.m365_connected = True`):

```python
    user.last_login = datetime.now(timezone.utc)
```

**Step 6: Expose in auth status response**

In `app/routers/auth.py`, in the `auth_status` function, add to the returned dict (around line 213):

```python
            "last_login": user.last_login.isoformat() if user.last_login else None,
```

Also add to each user in `users_status` list (around line 200):

```python
                "last_login": u.last_login.isoformat() if u.last_login else None,
```

**Step 7: Run tests — expect PASS**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestLastLogin tests/test_routers_auth.py -v`

**Step 8: Commit**

```bash
git add app/models/auth.py app/routers/auth.py alembic/versions/051_*
git commit -m "feat: track last_login timestamp on user model"
```

---

### Task 8: Add Session Idle Timeout (24h)

**Files:**
- Modify: `app/dependencies.py:32-41` — add idle check in `get_user`, stamp `last_active`

**Step 1: Add test**

Add to `tests/test_role_permissions.py`:

```python
class TestSessionIdleTimeout:
    """Sessions expire after 24h of inactivity."""

    def test_session_stamps_last_active(self):
        """Calling get_user should set session['last_active']."""
        from unittest.mock import MagicMock

        from app.dependencies import get_user

        session = {"user_id": 999}
        request = MagicMock()
        request.session = session

        # get_user won't find user 999, but it should still stamp the session
        # (stamping happens before DB lookup for efficiency)
        from tests.conftest import engine
        from sqlalchemy.orm import Session as SASession

        with SASession(engine) as db:
            get_user(request, db)

        # last_active should have been stamped
        assert "last_active" in session

    def test_stale_session_cleared(self):
        """A session idle for >24h should be cleared."""
        from unittest.mock import MagicMock

        from app.dependencies import get_user

        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        session = {"user_id": 999, "last_active": stale_ts}
        request = MagicMock()
        request.session = session

        from tests.conftest import engine
        from sqlalchemy.orm import Session as SASession

        with SASession(engine) as db:
            result = get_user(request, db)

        assert result is None
        assert "user_id" not in session  # session was cleared
```

Add the import at the top of the file:
```python
from datetime import datetime, timedelta, timezone
```

**Step 2: Run tests — expect FAIL**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestSessionIdleTimeout -v`

**Step 3: Implement**

Update `get_user` in `app/dependencies.py`:

```python
def get_user(request: Request, db: Session) -> User | None:
    """Return current user from session, or None if not logged in.

    Enforces 24h idle timeout — clears session if last_active is stale.
    """
    uid = request.session.get("user_id")
    if not uid:
        return None

    # Check idle timeout (24h)
    last_active = request.session.get("last_active")
    if last_active:
        try:
            ts = datetime.fromisoformat(last_active)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ts > timedelta(hours=24):
                request.session.clear()
                return None
        except (ValueError, TypeError):
            pass  # Malformed timestamp — just continue

    # Stamp last_active for next check
    request.session["last_active"] = datetime.now(timezone.utc).isoformat()

    try:
        return db.get(User, uid)
    except Exception:
        request.session.clear()
        return None
```

**Step 4: Run tests — expect PASS**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_role_permissions.py::TestSessionIdleTimeout -v`

**Step 5: Run full auth test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py -v`

**Step 6: Commit**

```bash
git add app/dependencies.py tests/test_role_permissions.py
git commit -m "feat: add 24h session idle timeout"
```

---

### Task 9: Update `conftest.py` + Run Full Suite

**Files:**
- Modify: `tests/conftest.py:239-258` — add `require_sales` override to `client` fixture

**Step 1: Update conftest**

The `client` fixture uses `test_user` (buyer role). Since many existing tests use this fixture to create requisitions, quotes, etc., we must override `require_sales` too:

```python
@pytest.fixture()
def client(db_session: Session, test_user: User) -> TestClient:
    """FastAPI TestClient with auth overridden to return test_user.

    Overrides get_db to use the test session and require_user/require_buyer/
    require_sales to skip M365 auth entirely.
    """
    from app.database import get_db
    from app.dependencies import require_buyer, require_sales, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    def _override_buyer():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_buyer
    app.dependency_overrides[require_sales] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
```

Also update any other test files that define their own client fixtures. Search for files that override `require_user` or `require_buyer` but don't override `require_sales`:

```bash
grep -rn "require_buyer\|require_user" tests/ --include="*.py" | grep "dependency_overrides"
```

For each fixture found, add the `require_sales` override.

**Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: ALL PASS (no regressions)

**Step 3: Coverage check**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: No coverage regression — aim for same or higher %

**Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "chore: add require_sales override to test fixtures"
```

---

### Task 10: Deploy & Verify

**Step 1: Push and rebuild**

```bash
git push origin main
```

**Step 2: Rebuild on server**

```bash
docker compose up -d --build
```

**Step 3: Run migration**

The entrypoint runs `alembic upgrade head` automatically, but verify:

```bash
docker compose logs app | tail -20
```

Look for: `Running upgrade ... -> 051, add last_login to users`

**Step 4: Smoke test**

- Log in as admin → verify all sections visible
- Check admin Users tab → verify last_login column shows
- Log in as a buyer-role user → verify Accounts and Contacts are hidden, Vendors visible
- Try creating a requisition as buyer → should get 403
- Log in as sales → verify can create reqs, quotes, buy plans
- Verify sales can't see Vendors
- Verify sales only sees own requisitions

**Step 5: Final commit (if any smoke-test fixes needed)**
