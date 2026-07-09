"""tests/test_knowledge_jobs_coverage2.py — Additional coverage for knowledge_jobs.py.

Targets uncovered branches:
- Vendor insight exception handler (lines 96-97)
- Company insight exception handler (lines 115-120)
- MPN insight exception handler (lines 137-142)
- Outer exception re-raise path (lines 147-150)
- Company/MPN insight success paths

Called by: pytest
Depends on: conftest.py, app/jobs/knowledge_jobs.py
"""

import os

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.crm import Company, CustomerSite
from app.models.offers import Offer
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard


@contextmanager
def _patch_insight_generators(**overrides):
    """Patch all five knowledge_service insight generators at the source module.

    Each generator defaults to an AsyncMock returning []. Pass an override (e.g.
    generate_vendor_insights=mock) to swap a specific one.
    """
    names = (
        "generate_insights",
        "generate_pipeline_insights",
        "generate_vendor_insights",
        "generate_company_insights",
        "generate_mpn_insights",
    )
    with patch.multiple(
        "app.services.knowledge_service",
        **{name: overrides.get(name, AsyncMock(return_value=[])) for name in names},
    ):
        yield


def _seed_vendor_offer(db: Session, vendor_card_id: int) -> None:
    db.add(VendorCard(id=vendor_card_id, normalized_name=f"vendor{vendor_card_id}", display_name=f"V{vendor_card_id}"))
    db.flush()
    db.add(
        Offer(
            vendor_card_id=vendor_card_id, vendor_name=f"V{vendor_card_id}", mpn="LM317T", created_at=datetime.now(UTC)
        )
    )


def _seed_company_requisition(db: Session, user: User, company_id: int, name: str) -> None:
    db.add(Company(id=company_id, name=f"Co{company_id}"))
    site = CustomerSite(company_id=company_id, site_name=f"Site{company_id}")
    db.add(site)
    db.flush()
    db.add(
        Requisition(
            name=name,
            customer_name=f"Co{company_id}",
            status="open",
            created_by=user.id,
            customer_site_id=site.id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )


def _seed_mpn_offer(db: Session, mpn: str) -> None:
    db.add(Offer(vendor_name="Some Vendor", mpn=mpn, created_at=datetime.now(UTC)))


class TestJobRefreshInsightsMissingBranches:
    """P6.3: converted the vendor/company/MPN success + exception-continues tests below
    from a rotating whole-session MagicMock (canned ``(id,)`` tuples regardless of the
    real filter/join/group-by) to real Offer/Company/CustomerSite/Requisition rows on
    ``db_session`` — the "outer exception" and "section DB error" tests at the bottom
    stay mocked (see their own docstrings) since those are genuine hard-failure
    paths."""

    async def test_vendor_insight_exception_continues(self, db_session: Session):
        """If generate_vendor_insights raises for a vendor, job continues to next."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        _seed_vendor_offer(db_session, 10)
        _seed_vendor_offer(db_session, 20)
        db_session.commit()
        mock_vendor = AsyncMock(side_effect=Exception("Vendor AI failed"))

        with patch("app.database.SessionLocal", lambda: db_session):
            with _patch_insight_generators(generate_vendor_insights=mock_vendor):
                await _job_refresh_insights()  # Should not raise

        assert mock_vendor.call_count == 2

    async def test_company_insight_exception_continues(self, db_session: Session, test_user: User):
        """If generate_company_insights raises, job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        _seed_company_requisition(db_session, test_user, 100, "KJ2-REQ-100")
        _seed_company_requisition(db_session, test_user, 200, "KJ2-REQ-200")
        db_session.commit()
        mock_company = AsyncMock(side_effect=Exception("Company AI failed"))

        with patch("app.database.SessionLocal", lambda: db_session):
            with _patch_insight_generators(generate_company_insights=mock_company):
                await _job_refresh_insights()  # Should not raise

        assert mock_company.call_count == 2

    async def test_mpn_insight_exception_continues(self, db_session: Session):
        """If generate_mpn_insights raises for an MPN, job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        _seed_mpn_offer(db_session, "LM317T")
        _seed_mpn_offer(db_session, "TL431")
        db_session.commit()
        mock_mpn = AsyncMock(side_effect=Exception("MPN AI failed"))

        with patch("app.database.SessionLocal", lambda: db_session):
            with _patch_insight_generators(generate_mpn_insights=mock_mpn):
                await _job_refresh_insights()  # Should not raise

        assert mock_mpn.call_count == 2

    async def test_company_insights_called_for_each_company(self, db_session: Session, test_user: User):
        """generate_company_insights is called once per company with recently-active
        requisitions, with the REAL company id from a real join."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        _seed_company_requisition(db_session, test_user, 30, "KJ2-REQ-30")
        _seed_company_requisition(db_session, test_user, 40, "KJ2-REQ-40")
        _seed_company_requisition(db_session, test_user, 50, "KJ2-REQ-50")
        db_session.commit()
        seen_ids = []

        async def _capture(db, company_id):
            seen_ids.append(company_id)
            return [MagicMock()]

        mock_company = AsyncMock(side_effect=_capture)

        with patch("app.database.SessionLocal", lambda: db_session):
            with _patch_insight_generators(generate_company_insights=mock_company):
                await _job_refresh_insights()

        assert mock_company.call_count == 3
        assert set(seen_ids) == {30, 40, 50}

    async def test_mpn_insights_called_for_each_mpn(self, db_session: Session):
        """generate_mpn_insights is called once per recently-quoted MPN, with the REAL
        mpn string from a real Offer row."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        _seed_mpn_offer(db_session, "ABC123")
        _seed_mpn_offer(db_session, "DEF456")
        _seed_mpn_offer(db_session, "GHI789")
        db_session.commit()
        seen_mpns = []

        async def _capture(db, mpn):
            seen_mpns.append(mpn)
            return [MagicMock()]

        mock_mpn = AsyncMock(side_effect=_capture)

        with patch("app.database.SessionLocal", lambda: db_session):
            with _patch_insight_generators(generate_mpn_insights=mock_mpn):
                await _job_refresh_insights()

        assert mock_mpn.call_count == 3
        assert set(seen_mpns) == {"ABC123", "DEF456", "GHI789"}

    async def test_outer_exception_reraises_and_rollbacks(self):
        """Outer exception causes rollback and re-raise.

        Patches the datetime class in the knowledge_jobs module to raise
        at `cutoff = datetime.now(...)`, triggering lines 147-150.

        P6.3 disposition: KEPT as a whole-session MagicMock — the outer ``try`` block
        this exercises wraps ALL section queries, so forcing it requires the
        `datetime.now()` call itself to raise (via a patched-in fake ``datetime``
        class), which has nothing to do with query filtering the mock could hide.
        """
        import app.jobs.knowledge_jobs as kjobs
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = MagicMock()
        mock_session.close = MagicMock()
        mock_session.rollback = MagicMock()

        # Create a mock datetime class where .now() raises
        mock_dt = MagicMock()
        mock_dt.now.side_effect = RuntimeError("Critical datetime failure")

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch.object(kjobs, "datetime", mock_dt):
                with pytest.raises(RuntimeError, match="Critical datetime failure"):
                    await _job_refresh_insights()

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    async def test_vendor_section_db_error_caught(self):
        """DB error in vendor section is caught by section handler.

        P6.3 disposition: KEPT as a whole-session MagicMock — needs the SECOND query
        call specifically to raise while the first and third succeed, which requires
        forcing a mid-sequence failure a real SQLite session has no clean way to
        simulate; the assertion is on the per-section catch-and-continue, not on any
        query result the mock hides.
        """
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = MagicMock()
        mock_session.close = MagicMock()
        mock_session.rollback = MagicMock()

        call_count = [0]

        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            mock_q = MagicMock()
            if call_count[0] == 1:
                # Req query - returns empty
                mock_q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
            elif call_count[0] == 2:
                # Vendor query - raises section-level error
                mock_q.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.side_effect = RuntimeError(
                    "Vendor section DB failure"
                )
            else:
                mock_q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
                mock_q.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
                mock_q.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
            return mock_q

        mock_session.query.side_effect = query_side_effect

        with patch("app.database.SessionLocal", return_value=mock_session):
            with _patch_insight_generators():
                await _job_refresh_insights()  # Should not raise

        mock_session.close.assert_called_once()
