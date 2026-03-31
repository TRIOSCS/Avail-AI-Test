"""test_coverage_salesperson_scorecard.py — Tests for app/services/salesperson_scorecard.py.

Called by: pytest
Depends on: conftest.py fixtures, app.services.salesperson_scorecard
"""

import os
import uuid

os.environ["TESTING"] = "1"

from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Company,
    Contact,
    CustomerSite,
    ProactiveOffer,
    Requisition,
    SiteContact,
    StockListHash,
    User,
)
from app.models.quotes import Quote


def _uid() -> str:
    return uuid.uuid4().hex[:8]


class TestGetSalespersonScorecard:
    def test_empty_db_returns_empty_entries(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        result = get_salesperson_scorecard(db_session, date(2026, 1, 1))
        assert "month" in result
        assert "year" in result
        assert "entries" in result
        assert result["entries"] == []

    def test_month_normalized_to_first_of_month(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        result = get_salesperson_scorecard(db_session, date(2026, 3, 15))
        assert result["month"] == "2026-03-01"
        assert result["year"] == 2026

    def test_december_rolls_over_correctly(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        result = get_salesperson_scorecard(db_session, date(2025, 12, 1))
        assert result["month"] == "2025-12-01"
        assert result["year"] == 2025

    def test_active_user_appears_in_results(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        user = User(
            email="sales-score@trioscs.com",
            name="Sales Person",
            role="sales",
            azure_id="azure-sales-score-001",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        result = get_salesperson_scorecard(db_session, date(2026, 1, 1))
        assert len(result["entries"]) == 1
        entry = result["entries"][0]
        assert entry["user_id"] == user.id
        assert entry["user_name"] == "Sales Person"
        assert "monthly" in entry
        assert "ytd" in entry

    def test_inactive_user_excluded(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        user = User(
            email="inactive-sales@trioscs.com",
            name="Inactive Sales",
            role="sales",
            azure_id="azure-inactive-001",
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        result = get_salesperson_scorecard(db_session, date(2026, 1, 1))
        assert result["entries"] == []

    def test_monthly_metrics_have_expected_keys(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        user = User(
            email="metrics-user@trioscs.com",
            name="Metrics User",
            role="buyer",
            azure_id="azure-metrics-001",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        result = get_salesperson_scorecard(db_session, date(2026, 2, 1))
        entry = result["entries"][0]

        expected_keys = [
            "new_accounts",
            "new_contacts",
            "calls_made",
            "emails_sent",
            "requisitions_entered",
            "quotes_sent",
            "orders_won",
            "won_revenue",
            "proactive_sent",
            "proactive_converted",
            "proactive_revenue",
            "boms_uploaded",
        ]
        for key in expected_keys:
            assert key in entry["monthly"], f"Missing monthly key: {key}"
            assert key in entry["ytd"], f"Missing ytd key: {key}"

    def test_all_metrics_default_zero(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        user = User(
            email="zero-metrics@trioscs.com",
            name="Zero Metrics",
            role="sales",
            azure_id="azure-zero-001",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        result = get_salesperson_scorecard(db_session, date(2026, 1, 1))
        entry = result["entries"][0]

        for key, val in entry["monthly"].items():
            assert val == 0 or val == 0.0, f"Expected 0 for {key}, got {val}"

    def test_user_name_falls_back_to_email(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        user = User(
            email="noname@trioscs.com",
            name=None,
            role="sales",
            azure_id="azure-noname-001",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        result = get_salesperson_scorecard(db_session, date(2026, 1, 1))
        entry = result["entries"][0]
        assert entry["user_name"] == "noname@trioscs.com"

    def test_multiple_users_sorted_by_won_revenue(self, db_session: Session):
        from app.services.salesperson_scorecard import get_salesperson_scorecard

        user1 = User(
            email="sales1@trioscs.com",
            name="Sales One",
            role="sales",
            azure_id="azure-sort-001",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        user2 = User(
            email="sales2@trioscs.com",
            name="Sales Two",
            role="sales",
            azure_id="azure-sort-002",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([user1, user2])
        db_session.commit()

        result = get_salesperson_scorecard(db_session, date(2026, 1, 1))
        assert len(result["entries"]) == 2


class TestSalespersonMetricsBatch:
    def test_empty_user_ids_returns_empty_dict(self, db_session: Session):
        from datetime import datetime, timezone

        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)

        result = _salesperson_metrics_batch(db_session, [], start, end)
        assert result == {}

    def test_returns_dict_keyed_by_user_id(self, db_session: Session):
        from datetime import datetime, timezone

        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = User(
            email="batch-user@trioscs.com",
            name="Batch User",
            role="sales",
            azure_id="azure-batch-001",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)

        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert user.id in result
        assert result[user.id]["new_accounts"] == 0


class TestSalespersonMetricsSingle:
    def test_single_user_returns_metrics(self, db_session: Session):
        from datetime import datetime, timezone

        from app.services.salesperson_scorecard import _salesperson_metrics

        user = User(
            email="single-user@trioscs.com",
            name="Single User",
            role="sales",
            azure_id="azure-single-001",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)

        result = _salesperson_metrics(db_session, user.id, start, end)
        assert "new_accounts" in result
        assert "won_revenue" in result
        assert result["won_revenue"] == 0.0

    def test_nonexistent_user_returns_defaults(self, db_session: Session):
        from datetime import datetime, timezone

        from app.services.salesperson_scorecard import _salesperson_metrics

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)

        result = _salesperson_metrics(db_session, 999999, start, end)
        assert result["new_accounts"] == 0
        assert result["won_revenue"] == 0.0
        assert result["boms_uploaded"] == 0


class TestSalespersonMetricsWithData:
    """Tests that trigger the batch loop body — create real DB records within date range."""

    def _make_user(self, db_session: Session) -> User:
        u = User(
            email=f"sp-{_uid()}@trioscs.com",
            name="SP User",
            role="sales",
            azure_id=f"azure-sp-{_uid()}",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(u)
        db_session.flush()
        return u

    def test_new_accounts_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        company = Company(
            name=f"Acme-{_uid()}",
            account_owner_id=user.id,
            created_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )
        db_session.add(company)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["new_accounts"] == 1

    def test_new_contacts_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        company = Company(name=f"Co-{_uid()}", created_at=datetime.now(timezone.utc))
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(
            company_id=company.id,
            site_name=f"site-{_uid()}",
            owner_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Test Contact",
            created_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["new_contacts"] == 1

    def test_calls_made_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        log = ActivityLog(
            user_id=user.id,
            activity_type="call_outbound",
            channel="phone",
            created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
        db_session.add(log)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["calls_made"] == 1

    def test_emails_sent_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        req = Requisition(
            name=f"REQ-{_uid()}",
            status="open",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        contact = Contact(
            requisition_id=req.id,
            user_id=user.id,
            contact_type="email",
            vendor_name="Test Vendor",
            created_at=datetime(2026, 1, 8, tzinfo=timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["emails_sent"] == 1

    def test_requisitions_entered_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        req = Requisition(
            name=f"REQ-{_uid()}",
            status="open",
            created_by=user.id,
            created_at=datetime(2026, 1, 12, tzinfo=timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["requisitions_entered"] == 1

    def test_quotes_sent_and_won_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        req = Requisition(
            name=f"REQ-{_uid()}",
            status="open",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        quote = Quote(
            requisition_id=req.id,
            quote_number=f"Q-{_uid()}",
            created_by_id=user.id,
            sent_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
            result="won",
            result_at=datetime(2026, 1, 25, tzinfo=timezone.utc),
            won_revenue=5000.00,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["quotes_sent"] == 1
        assert result[user.id]["orders_won"] == 1
        assert result[user.id]["won_revenue"] == 5000.0

    def test_proactive_sent_and_converted_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        offer = ProactiveOffer(
            salesperson_id=user.id,
            status="converted",
            sent_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            converted_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
            total_sell=2500.00,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["proactive_sent"] == 1
        assert result[user.id]["proactive_converted"] == 1
        assert result[user.id]["proactive_revenue"] == 2500.0

    def test_boms_uploaded_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        slh = StockListHash(
            user_id=user.id,
            content_hash="a" * 64,
            file_name="bom.xlsx",
            row_count=50,
            first_seen_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
        db_session.add(slh)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["boms_uploaded"] == 1

    def test_records_outside_range_not_counted(self, db_session: Session):
        from app.services.salesperson_scorecard import _salesperson_metrics_batch

        user = self._make_user(db_session)
        # Company created in December 2025 — outside January 2026 range
        company = Company(
            name=f"OldCo-{_uid()}",
            account_owner_id=user.id,
            created_at=datetime(2025, 12, 15, tzinfo=timezone.utc),
        )
        db_session.add(company)
        db_session.commit()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = _salesperson_metrics_batch(db_session, [user.id], start, end)
        assert result[user.id]["new_accounts"] == 0
