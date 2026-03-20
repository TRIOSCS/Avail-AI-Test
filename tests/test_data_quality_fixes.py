"""Tests for 4 data-quality bug fixes: TT-036, TT-100, TT-103, TT-043.

Covers:
- TT-036: _cap_outlier default cap lowered to $500K
- TT-100: Morning brief uses target user name (salesperson_id param)
- TT-103: AI prompt uses same quotes_awaiting as stats response
- TT-043: log_call_activity populates subject field
"""

from datetime import datetime, timezone

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
)

# ═══════════════════════════════════════════════════════════════════════
#  TT-100: Morning brief uses selected user's name
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
#  TT-043: Call activities populate subject field
# ═══════════════════════════════════════════════════════════════════════


class TestCallActivitySubject:
    """log_call_activity should populate the subject field."""

    def _make_company(self, db, name="Acme"):
        c = Company(
            name=name,
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(c)
        db.flush()
        return c

    def _make_site(self, db, company_id, phone=None):
        s = CustomerSite(
            company_id=company_id,
            site_name="HQ",
            contact_phone=phone,
            created_at=datetime.now(timezone.utc),
        )
        db.add(s)
        db.flush()
        return s

    def test_outbound_call_auto_subject_with_name(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        co = self._make_company(db_session)
        self._make_site(db_session, co.id, phone="+15559990001")
        db_session.commit()

        record = log_call_activity(test_user.id, "outbound", "5559990001", 120, "ext-sub-1", "Bob Smith", db_session)
        assert record is not None
        assert record.subject == "Call to Bob Smith"

    def test_inbound_call_auto_subject_with_name(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        co = self._make_company(db_session)
        self._make_site(db_session, co.id, phone="+15559990002")
        db_session.commit()

        record = log_call_activity(test_user.id, "inbound", "5559990002", 60, "ext-sub-2", "Jane Doe", db_session)
        assert record is not None
        assert record.subject == "Call from Jane Doe"

    def test_call_subject_falls_back_to_phone(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(test_user.id, "outbound", "5559990003", 30, "ext-sub-3", None, db_session)
        assert record is not None
        assert record.subject == "Call to 5559990003"

    def test_call_subject_falls_back_to_unknown(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(test_user.id, "inbound", "", None, "ext-sub-4", None, db_session)
        assert record is not None
        assert record.subject == "Call from unknown"

    def test_explicit_subject_overrides_auto(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(
            test_user.id,
            "outbound",
            "5559990005",
            45,
            "ext-sub-5",
            "Bob",
            db_session,
            subject="Follow-up re: PO-1234",
        )
        assert record is not None
        assert record.subject == "Follow-up re: PO-1234"

    def test_subject_persisted_in_db(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(test_user.id, "outbound", "5559990006", 10, "ext-sub-6", "Charlie", db_session)
        db_session.commit()

        fetched = db_session.query(ActivityLog).filter(ActivityLog.external_id == "ext-sub-6").first()
        assert fetched is not None
        assert fetched.subject == "Call to Charlie"
