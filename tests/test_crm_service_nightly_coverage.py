"""test_crm_service_nightly_coverage.py — Tests for app/services/crm_service.py.

Covers: next_quote_number (first number, sequential increment, ValueError on bad suffix).

Called by: pytest
Depends on: app/services/crm_service.py, conftest.py
"""

import os
from datetime import datetime, timezone

from freezegun import freeze_time

os.environ["TESTING"] = "1"

from app.models import Quote
from app.services.crm_service import next_quote_number


class TestNextQuoteNumber:
    def test_first_quote_in_year(self, db_session):
        """No existing quotes → Q-YYYY-0001."""
        result = next_quote_number(db_session)
        year = datetime.now(timezone.utc).year
        assert result == f"Q-{year}-0001"

    def test_increments_from_last(self, db_session, test_user, test_requisition):
        """Existing quote → next number is last + 1."""
        year = datetime.now(timezone.utc).year
        q = Quote(
            requisition_id=test_requisition.id,
            quote_number=f"Q-{year}-0005",
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        result = next_quote_number(db_session)
        assert result == f"Q-{year}-0006"

    def test_zero_pads_to_4_digits(self, db_session, test_user, test_requisition):
        """Zero-pads sequence to 4 digits."""
        year = datetime.now(timezone.utc).year
        q = Quote(
            requisition_id=test_requisition.id,
            quote_number=f"Q-{year}-0099",
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        result = next_quote_number(db_session)
        assert result == f"Q-{year}-0100"

    def test_bad_suffix_falls_back_to_1(self, db_session, test_user, test_requisition):
        """When last quote_number suffix is non-numeric, falls back to seq=1."""
        year = datetime.now(timezone.utc).year
        q = Quote(
            requisition_id=test_requisition.id,
            quote_number=f"Q-{year}-XXXX",
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        result = next_quote_number(db_session)
        assert result == f"Q-{year}-0001"

    def test_ignores_other_year_quotes(self, db_session, test_user, test_requisition):
        """Quotes from a different year don't affect this year's numbering."""
        with freeze_time("2023-06-15"):
            old = Quote(
                requisition_id=test_requisition.id,
                quote_number="Q-2023-0099",
                created_by_id=test_user.id,
            )
            db_session.add(old)
            db_session.commit()

        result = next_quote_number(db_session)
        year = datetime.now(timezone.utc).year
        assert result == f"Q-{year}-0001"
