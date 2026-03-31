"""test_coverage_buyer_leaderboard.py — Tests for app/services/buyer_leaderboard.py.

Called by: pytest
Depends on: conftest.py fixtures, app.services.buyer_leaderboard, app.models
"""

import os

os.environ["TESTING"] = "1"

from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models import BuyerLeaderboardSnapshot, User


class TestComputeBuyerLeaderboard:
    def test_empty_db_returns_empty_entries(self, db_session: Session):
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        result = compute_buyer_leaderboard(db_session, date(2026, 1, 1))
        assert result["month"] == "2026-01-01"
        assert result["entries"] == 0

    def test_with_buyer_user(self, db_session: Session):
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        buyer = User(
            email="buyer@trioscs.com",
            name="Test Buyer",
            role="buyer",
            azure_id="azure-leaderboard-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(buyer)
        db_session.commit()

        result = compute_buyer_leaderboard(db_session, date(2026, 1, 1))
        assert result["entries"] == 1

    def test_december_month_end_year_rollover(self, db_session: Session):
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        # December should roll over to next year
        result = compute_buyer_leaderboard(db_session, date(2025, 12, 1))
        assert result["month"] == "2025-12-01"
        assert result["entries"] == 0

    def test_non_buyer_role_excluded(self, db_session: Session):
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        admin = User(
            email="admin@trioscs.com",
            name="Admin User",
            role="admin",
            azure_id="azure-admin-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        db_session.commit()

        result = compute_buyer_leaderboard(db_session, date(2026, 1, 1))
        # Admin not in buyer/trader roles — should not appear
        assert result["entries"] == 0

    def test_upserts_snapshot_on_second_call(self, db_session: Session):
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        buyer = User(
            email="buyer2@trioscs.com",
            name="Buyer Two",
            role="buyer",
            azure_id="azure-buyer-002",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(buyer)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 2, 1))
        compute_buyer_leaderboard(db_session, date(2026, 2, 1))  # Second call upserts

        snapshots = (
            db_session.query(BuyerLeaderboardSnapshot).filter(BuyerLeaderboardSnapshot.month == date(2026, 2, 1)).all()
        )
        assert len(snapshots) == 1  # Only one snapshot per buyer per month


class TestGetBuyerLeaderboard:
    def test_empty_returns_empty_list(self, db_session: Session):
        from app.services.buyer_leaderboard import get_buyer_leaderboard

        result = get_buyer_leaderboard(db_session, date(2026, 1, 1))
        assert result == []

    def test_returns_data_after_compute(self, db_session: Session):
        from app.services.buyer_leaderboard import (
            compute_buyer_leaderboard,
            get_buyer_leaderboard,
        )

        buyer = User(
            email="lbbuyer@trioscs.com",
            name="LB Buyer",
            role="buyer",
            azure_id="azure-lb-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(buyer)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 3, 1))
        result = get_buyer_leaderboard(db_session, date(2026, 3, 1))

        assert len(result) == 1
        entry = result[0]
        assert entry["user_name"] == "LB Buyer"
        assert "rank" in entry
        assert "total_points" in entry
        assert "ytd_total_points" in entry

    def test_ytd_fields_present(self, db_session: Session):
        from app.services.buyer_leaderboard import (
            compute_buyer_leaderboard,
            get_buyer_leaderboard,
        )

        buyer = User(
            email="ytdbuyer@trioscs.com",
            name="YTD Buyer",
            role="buyer",
            azure_id="azure-ytd-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(buyer)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 4, 1))
        result = get_buyer_leaderboard(db_session, date(2026, 4, 1))

        assert len(result) == 1
        entry = result[0]
        expected_ytd_keys = [
            "ytd_offers_logged",
            "ytd_offers_po_confirmed",
            "ytd_total_points",
        ]
        for key in expected_ytd_keys:
            assert key in entry, f"Missing key: {key}"


class TestGetBuyerLeaderboardMonths:
    def test_empty_db_returns_empty(self, db_session: Session):
        from app.services.buyer_leaderboard import get_buyer_leaderboard_months

        result = get_buyer_leaderboard_months(db_session)
        assert result == []

    def test_returns_months_after_compute(self, db_session: Session):
        from app.services.buyer_leaderboard import (
            compute_buyer_leaderboard,
            get_buyer_leaderboard_months,
        )

        buyer = User(
            email="monthbuyer@trioscs.com",
            name="Month Buyer",
            role="buyer",
            azure_id="azure-month-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(buyer)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 1, 1))
        compute_buyer_leaderboard(db_session, date(2026, 2, 1))

        months = get_buyer_leaderboard_months(db_session)
        assert len(months) >= 1
        for month_str in months:
            assert isinstance(month_str, str)
            assert "-" in month_str  # ISO format like "2026-01-01"


class TestComputeStockListHash:
    def test_basic_hash_is_sha256(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        rows = [{"mpn": "LM317T"}, {"mpn": "BC547"}]
        result = compute_stock_list_hash(rows)
        assert isinstance(result, str)
        assert len(result) == 64  # SHA-256 hex digest

    def test_order_independent(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        rows1 = [{"mpn": "LM317T"}, {"mpn": "BC547"}]
        rows2 = [{"mpn": "BC547"}, {"mpn": "LM317T"}]
        assert compute_stock_list_hash(rows1) == compute_stock_list_hash(rows2)

    def test_case_insensitive(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        rows1 = [{"mpn": "lm317t"}]
        rows2 = [{"mpn": "LM317T"}]
        assert compute_stock_list_hash(rows1) == compute_stock_list_hash(rows2)

    def test_part_number_field_fallback(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        rows = [{"part_number": "LM317T"}, {"part_number": "BC547"}]
        result = compute_stock_list_hash(rows)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_empty_rows_returns_hash(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        result = compute_stock_list_hash([])
        assert isinstance(result, str)
        assert len(result) == 64

    def test_deduplicates_mpns(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        rows_with_dup = [{"mpn": "LM317T"}, {"mpn": "LM317T"}, {"mpn": "BC547"}]
        rows_unique = [{"mpn": "LM317T"}, {"mpn": "BC547"}]
        assert compute_stock_list_hash(rows_with_dup) == compute_stock_list_hash(rows_unique)

    def test_empty_mpn_skipped(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        rows_with_empty = [{"mpn": ""}, {"mpn": "LM317T"}]
        rows_without = [{"mpn": "LM317T"}]
        assert compute_stock_list_hash(rows_with_empty) == compute_stock_list_hash(rows_without)

    def test_none_mpn_skipped(self):
        from app.services.buyer_leaderboard import compute_stock_list_hash

        rows = [{"mpn": None}, {"mpn": "BC547"}]
        rows_clean = [{"mpn": "BC547"}]
        assert compute_stock_list_hash(rows) == compute_stock_list_hash(rows_clean)


class TestCheckAndRecordStockList:
    def test_new_stock_list_not_duplicate(self, db_session: Session, test_user):
        from app.services.buyer_leaderboard import check_and_record_stock_list

        result = check_and_record_stock_list(
            db_session,
            user_id=test_user.id,
            content_hash="abc123def456" + "a" * 52,
            vendor_card_id=None,
            file_name="stock_list.xlsx",
            row_count=100,
        )
        assert result["is_duplicate"] is False
        assert result["upload_count"] == 1
        assert "first_seen_at" in result

    def test_duplicate_stock_list_detected(self, db_session: Session, test_user):
        from app.services.buyer_leaderboard import check_and_record_stock_list

        content_hash = "dup" + "x" * 61  # 64 chars
        # First upload
        check_and_record_stock_list(
            db_session,
            user_id=test_user.id,
            content_hash=content_hash,
            vendor_card_id=None,
            file_name="first.xlsx",
            row_count=50,
        )

        # Second upload — same hash
        result = check_and_record_stock_list(
            db_session,
            user_id=test_user.id,
            content_hash=content_hash,
            vendor_card_id=None,
            file_name="second.xlsx",
            row_count=50,
        )
        assert result["is_duplicate"] is True
        assert result["upload_count"] == 2

    def test_different_users_same_hash_not_duplicate(self, db_session: Session, test_user):
        from app.services.buyer_leaderboard import check_and_record_stock_list

        user2 = User(
            email="user2@trioscs.com",
            name="User Two",
            role="buyer",
            azure_id="azure-user2-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user2)
        db_session.commit()

        content_hash = "shared" + "y" * 58  # 64 chars

        result1 = check_and_record_stock_list(
            db_session,
            user_id=test_user.id,
            content_hash=content_hash,
            vendor_card_id=None,
            file_name="file.xlsx",
            row_count=20,
        )
        result2 = check_and_record_stock_list(
            db_session,
            user_id=user2.id,
            content_hash=content_hash,
            vendor_card_id=None,
            file_name="file.xlsx",
            row_count=20,
        )

        assert result1["is_duplicate"] is False
        assert result2["is_duplicate"] is False
