"""
test_services_ownership.py — Tests for ownership_service.

Tests account claiming, ownership sweep, at-risk detection,
and open pool logic. Uses in-memory SQLite.

Called by: pytest
Depends on: app/services/ownership_service.py, conftest.py
"""

from datetime import datetime, timedelta, timezone

from app.models import Company, User
from app.services.ownership_service import (
    check_and_claim_open_account,
    get_accounts_at_risk,
    get_my_accounts,
    get_open_pool_accounts,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_company(db, name="Test Co", owner_id=None, last_activity_at=None):
    co = Company(
        name=name, is_active=True,
        account_owner_id=owner_id,
        last_activity_at=last_activity_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()
    return co


def _make_sales_user(db, email="sales1@trioscs.com"):
    u = User(
        email=email, name="Sales User", role="sales",
        azure_id=f"azure-{email}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


# ── Auto-claim ──────────────────────────────────────────────────────


class TestCheckAndClaimOpenAccount:
    def test_claims_unowned_company(self, db_session):
        sales = _make_sales_user(db_session)
        co = _make_company(db_session)
        db_session.commit()

        result = check_and_claim_open_account(co.id, sales.id, db_session)
        assert result is True
        db_session.refresh(co)
        assert co.account_owner_id == sales.id

    def test_does_not_claim_owned_company(self, db_session):
        sales1 = _make_sales_user(db_session, "sales1@t.com")
        sales2 = _make_sales_user(db_session, "sales2@t.com")
        co = _make_company(db_session, owner_id=sales1.id)
        db_session.commit()

        result = check_and_claim_open_account(co.id, sales2.id, db_session)
        assert result is False
        db_session.refresh(co)
        assert co.account_owner_id == sales1.id

    def test_non_sales_cannot_claim(self, db_session, test_user):
        """test_user is a buyer — should not auto-claim."""
        co = _make_company(db_session)
        db_session.commit()

        result = check_and_claim_open_account(co.id, test_user.id, db_session)
        assert result is False


# ── At-risk accounts ────────────────────────────────────────────────


class TestGetAccountsAtRisk:
    def test_no_at_risk_accounts(self, db_session):
        result = get_accounts_at_risk(db_session)
        assert result == []

    def test_old_account_at_risk(self, db_session):
        sales = _make_sales_user(db_session)
        co = _make_company(
            db_session, owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.commit()

        result = get_accounts_at_risk(db_session)
        # Should show as at-risk (within 7-day warning window of 30-day limit)
        names = [r["company_name"] for r in result]
        assert co.name in names


# ── Open pool ───────────────────────────────────────────────────────


class TestGetOpenPoolAccounts:
    def test_unowned_companies_in_pool(self, db_session):
        _make_company(db_session, name="Unowned Corp")
        db_session.commit()

        result = get_open_pool_accounts(db_session)
        names = [r["company_name"] for r in result]
        assert "Unowned Corp" in names

    def test_owned_companies_not_in_pool(self, db_session):
        sales = _make_sales_user(db_session)
        _make_company(db_session, name="Owned Corp", owner_id=sales.id)
        db_session.commit()

        result = get_open_pool_accounts(db_session)
        names = [r["company_name"] for r in result]
        assert "Owned Corp" not in names


# ── My accounts ─────────────────────────────────────────────────────


class TestGetMyAccounts:
    def test_returns_owned_accounts(self, db_session):
        sales = _make_sales_user(db_session)
        _make_company(
            db_session, name="My Account", owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc),
        )
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        names = [r["company_name"] for r in result]
        assert "My Account" in names

    def test_excludes_other_users(self, db_session):
        sales1 = _make_sales_user(db_session, "s1@t.com")
        sales2 = _make_sales_user(db_session, "s2@t.com")
        _make_company(db_session, name="S1 Account", owner_id=sales1.id)
        _make_company(db_session, name="S2 Account", owner_id=sales2.id)
        db_session.commit()

        result = get_my_accounts(sales1.id, db_session)
        names = [r["company_name"] for r in result]
        assert "S1 Account" in names
        assert "S2 Account" not in names

    def test_health_status_green(self, db_session):
        sales = _make_sales_user(db_session)
        _make_company(
            db_session, name="Fresh Co", owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        assert result[0]["status"] in ("green", "no_activity")
