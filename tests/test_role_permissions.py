"""test_role_permissions.py — Role-based access control tests.

Tests that buyer cannot access sales-gated endpoints and that sales
CAN access buyer-gated endpoints.

Called by: pytest
Depends on: conftest fixtures, app.dependencies
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User


@pytest.fixture()
def buyer_user(db_session: Session) -> User:
    user = User(
        email="buyer-rbac@trioscs.com",
        name="RBAC Buyer",
        role="buyer",
        azure_id="test-azure-rbac-buyer",
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

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


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

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


def _mock_request(user_id: int) -> MagicMock:
    """Create a mock Request with session containing user_id."""
    request = MagicMock()
    request.session = {"user_id": user_id}
    request.headers = {}
    return request


class TestRequireSalesDependency:
    """Unit tests calling require_sales directly."""

    def test_require_sales_allows_sales(self, db_session: Session, sales_user: User):
        from app.dependencies import require_sales

        request = _mock_request(sales_user.id)
        result = require_sales(request=request, db=db_session)
        assert result.id == sales_user.id
        assert result.role == "sales"

    def test_require_sales_allows_admin(self, db_session: Session, admin_user: User):
        from app.dependencies import require_sales

        request = _mock_request(admin_user.id)
        result = require_sales(request=request, db=db_session)
        assert result.id == admin_user.id
        assert result.role == "admin"

    def test_require_sales_allows_trader(self, db_session: Session, trader_user: User):
        from app.dependencies import require_sales

        request = _mock_request(trader_user.id)
        result = require_sales(request=request, db=db_session)
        assert result.id == trader_user.id
        assert result.role == "trader"

    def test_require_sales_allows_manager(self, db_session: Session, manager_user: User):
        from app.dependencies import require_sales

        request = _mock_request(manager_user.id)
        result = require_sales(request=request, db=db_session)
        assert result.id == manager_user.id
        assert result.role == "manager"

    def test_require_sales_blocks_buyer(self, db_session: Session, buyer_user: User):
        from app.dependencies import require_sales

        request = _mock_request(buyer_user.id)
        with pytest.raises(HTTPException) as exc_info:
            require_sales(request=request, db=db_session)
        assert exc_info.value.status_code == 403
        assert "Sales role required" in str(exc_info.value.detail)


class TestRequireBuyerIncludesSales:
    """Verify require_buyer now allows sales role."""

    def test_require_buyer_allows_sales(self, db_session: Session, sales_user: User):
        from app.dependencies import require_buyer

        request = _mock_request(sales_user.id)
        result = require_buyer(request=request, db=db_session)
        assert result.id == sales_user.id
        assert result.role == "sales"
