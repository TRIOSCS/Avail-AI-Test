"""test_quick_coverage_wins.py — Coverage tests for small modules below 85%.

Targets: crm_service, events, crm/clone, crm/views, documents, dependencies.
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from tests.conftest import engine

_ = engine  # ensure tables created


# ─────────────────────────────────────────────────────────────────────────────
# crm_service.next_quote_number
# ─────────────────────────────────────────────────────────────────────────────


class TestNextQuoteNumber:
    def test_first_quote_of_year(self, db_session: Session):
        from app.services.crm_service import next_quote_number

        result = next_quote_number(db_session)
        year = datetime.now(timezone.utc).year
        assert result == f"Q-{year}-0001"

    def test_increments_from_last(self, db_session: Session):
        from app.models import Requisition, User
        from app.models.quotes import Quote
        from app.services.crm_service import next_quote_number

        year = datetime.now(timezone.utc).year
        user = User(email="q@test.com", name="Q", role="buyer", azure_id="az-q")
        db_session.add(user)
        db_session.flush()
        req = Requisition(name="REQ-Q", customer_name="Acme", status="active", created_by=user.id)
        db_session.add(req)
        db_session.flush()
        quote = Quote(
            requisition_id=req.id,
            quote_number=f"Q-{year}-0005",
            status="draft",
            created_by_id=user.id,
        )
        db_session.add(quote)
        db_session.commit()

        result = next_quote_number(db_session)
        assert result == f"Q-{year}-0006"

    def test_handles_malformed_suffix(self, db_session: Session):
        from app.models import Requisition, User
        from app.models.quotes import Quote
        from app.services.crm_service import next_quote_number

        year = datetime.now(timezone.utc).year
        user = User(email="qm@test.com", name="QM", role="buyer", azure_id="az-qm")
        db_session.add(user)
        db_session.flush()
        req = Requisition(name="REQ-QM", customer_name="Acme", status="active", created_by=user.id)
        db_session.add(req)
        db_session.flush()
        quote = Quote(
            requisition_id=req.id,
            quote_number=f"Q-{year}-INVALID",
            status="draft",
            created_by_id=user.id,
        )
        db_session.add(quote)
        db_session.commit()

        result = next_quote_number(db_session)
        assert result == f"Q-{year}-0001"


# ─────────────────────────────────────────────────────────────────────────────
# events.py SSE endpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestEventStream:
    def test_event_stream_returns_200(self, client):
        """SSE endpoint should return 200 with appropriate content type."""
        with patch("app.services.sse_broker.broker.listen", return_value=_empty_async_gen()):
            resp = client.get("/api/events/stream")
        assert resp.status_code == 200

    def test_event_stream_content_type(self, client):
        with patch("app.services.sse_broker.broker.listen", return_value=_empty_async_gen()):
            resp = client.get("/api/events/stream")
        assert "text/event-stream" in resp.headers.get("content-type", "")


async def _empty_async_gen():
    """Empty async generator for mocking SSE broker."""
    return
    yield  # makes it an async generator


# ─────────────────────────────────────────────────────────────────────────────
# crm/clone.py — clone_requisition
# ─────────────────────────────────────────────────────────────────────────────


class TestCloneRequisition:
    def test_clone_not_found(self, client):
        resp = client.post("/api/requisitions/99999/clone")
        assert resp.status_code == 404

    def test_clone_simple_requisition(self, client, db_session, test_requisition):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "clone" in data["name"]
        assert data["id"] != test_requisition.id

    def test_clone_creates_requirements(self, client, db_session, test_requisition):
        from app.models import Requirement

        original_reqs = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).count()

        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200

        new_id = resp.json()["id"]
        cloned_reqs = db_session.query(Requirement).filter_by(requisition_id=new_id).count()
        assert cloned_reqs == original_reqs

    def test_clone_with_offers(self, client, db_session, test_user, test_requisition, test_vendor_card):
        from app.models import Offer, Requirement

        req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()

        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_card_id=test_vendor_card.id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.45,
            status="active",
            entered_by_id=test_user.id,
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_clone_with_substitutes(self, client, db_session, test_user, test_requisition):
        from app.models import Requirement

        req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        req_item.substitutes = ["LM317AT", "LM317BT"]
        db_session.commit()

        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# crm/views.py — CRM performance endpoints
# ─────────────────────────────────────────────────────────────────────────────


class TestCrmViews:
    def test_performance_metrics_json(self, client):
        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "names" in data
        assert "scores" in data
        assert "behaviors" in data
        assert "outcomes" in data

    def test_performance_metrics_with_users(self, client, db_session, test_user):
        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["names"], list)

    def test_compute_user_score_buyer(self, db_session, test_user):
        from datetime import date

        from app.routers.crm.views import _compute_user_score

        result = _compute_user_score(db_session, test_user, date.today().replace(day=1))
        assert "total_score" in result
        assert "behavior_total" in result
        assert "outcome_total" in result

    def test_compute_user_score_sales(self, db_session, sales_user):
        from datetime import date

        from app.routers.crm.views import _compute_user_score

        result = _compute_user_score(db_session, sales_user, date.today().replace(day=1))
        assert "total_score" in result

    def test_compute_user_score_exception_returns_zeros(self, db_session, test_user):
        from datetime import date

        from app.routers.crm.views import _compute_user_score

        with patch(
            "app.services.avail_score_service.compute_buyer_avail_score",
            side_effect=Exception("DB error"),
        ):
            result = _compute_user_score(db_session, test_user, date.today().replace(day=1))
        assert result["total_score"] == 0

    def test_score_from_snap(self):
        from app.routers.crm.views import _score_from_snap

        snap = MagicMock()
        snap.behavior_total = 12.345
        snap.outcome_total = 8.111
        snap.total_score = 20.456
        result = _score_from_snap(snap)
        assert result["behavior_total"] == 12.3
        assert result["outcome_total"] == 8.1
        assert result["total_score"] == 20.5

    def test_score_from_data(self):
        from app.routers.crm.views import _score_from_data

        data = {"behavior_total": 5.555, "outcome_total": 3.333, "total_score": 8.888}
        result = _score_from_data(data)
        assert result["behavior_total"] == 5.6
        assert result["outcome_total"] == 3.3


# ─────────────────────────────────────────────────────────────────────────────
# documents.py — PDF generation endpoints
# ─────────────────────────────────────────────────────────────────────────────


class TestDocuments:
    def test_rfq_pdf_not_found(self, client):
        resp = client.get("/api/requisitions/99999/pdf")
        assert resp.status_code == 404

    def test_rfq_pdf_success(self, client, test_requisition):
        with patch(
            "app.services.document_service.generate_rfq_summary_pdf",
            return_value=b"%PDF-1.4 fake pdf content",
        ):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"

    def test_rfq_pdf_generation_error(self, client, test_requisition):
        with patch(
            "app.services.document_service.generate_rfq_summary_pdf",
            side_effect=Exception("WeasyPrint error"),
        ):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/pdf")
        assert resp.status_code == 500

    def test_rfq_pdf_value_error(self, client, test_requisition):
        with patch(
            "app.services.document_service.generate_rfq_summary_pdf",
            side_effect=ValueError("No requirements found"),
        ):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/pdf")
        assert resp.status_code == 404

    def test_quote_pdf_not_found(self, client):
        resp = client.get("/api/quotes/99999/pdf")
        assert resp.status_code == 404

    def test_quote_pdf_success(self, client, db_session, test_user, test_requisition):
        from app.models.quotes import Quote

        quote = Quote(
            requisition_id=test_requisition.id,
            quote_number="Q-2026-0001",
            status="draft",
            created_by_id=test_user.id,
        )
        db_session.add(quote)
        db_session.commit()

        with patch(
            "app.services.document_service.generate_quote_report_pdf",
            return_value=b"%PDF-1.4 fake quote pdf",
        ):
            resp = client.get(f"/api/quotes/{quote.id}/pdf")
        assert resp.status_code == 200
        assert "application/pdf" in resp.headers["content-type"]

    def test_quote_pdf_generation_error(self, client, db_session, test_user, test_requisition):
        from app.models.quotes import Quote

        quote = Quote(
            requisition_id=test_requisition.id,
            quote_number="Q-2026-0099",
            status="draft",
            created_by_id=test_user.id,
        )
        db_session.add(quote)
        db_session.commit()

        with patch(
            "app.services.document_service.generate_quote_report_pdf",
            side_effect=Exception("WeasyPrint error"),
        ):
            resp = client.get(f"/api/quotes/{quote.id}/pdf")
        assert resp.status_code == 500


# ─────────────────────────────────────────────────────────────────────────────
# dependencies.py — auth helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestDependencies:
    def test_is_admin_true(self, test_user):
        from app.dependencies import is_admin

        test_user.role = "admin"
        assert is_admin(test_user) is True

    def test_is_admin_false(self, test_user):
        from app.dependencies import is_admin

        test_user.role = "buyer"
        assert is_admin(test_user) is False

    def test_get_req_for_user_not_found(self, db_session, test_user):
        from fastapi import HTTPException

        from app.dependencies import get_req_for_user

        with pytest.raises(HTTPException) as exc:
            get_req_for_user(db_session, test_user, 99999)
        assert exc.value.status_code == 404

    def test_get_req_for_user_sales_filters_by_owner(self, db_session, sales_user, test_user):
        from fastapi import HTTPException

        from app.dependencies import get_req_for_user
        from app.models import Requisition

        req = Requisition(
            name="Private REQ",
            customer_name="Secret Co",
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            get_req_for_user(db_session, sales_user, req.id)
        assert exc.value.status_code == 404

    def test_get_quote_for_user_not_found(self, db_session, test_user):
        from fastapi import HTTPException

        from app.dependencies import get_quote_for_user

        with pytest.raises(HTTPException) as exc:
            get_quote_for_user(db_session, test_user, 99999)
        assert exc.value.status_code == 404

    def test_require_buyer_wrong_role(self, db_session):
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from app.dependencies import require_buyer
        from app.models import User

        viewer_user = User(
            email="viewer@test.com",
            name="Viewer",
            role="viewer",
            azure_id="az-viewer",
        )
        db_session.add(viewer_user)
        db_session.commit()

        request = MagicMock()
        request.session = {"user_id": viewer_user.id}

        with pytest.raises(HTTPException) as exc:
            require_buyer(request, db_session)
        assert exc.value.status_code == 403

    def test_get_user_no_session(self, db_session):
        from unittest.mock import MagicMock

        from app.dependencies import get_user

        request = MagicMock()
        request.session = {}
        result = get_user(request, db_session)
        assert result is None

    def test_get_user_exception_clears_session(self, db_session):
        from unittest.mock import MagicMock

        from app.dependencies import get_user

        request = MagicMock()
        request.session = {"user_id": 99999}

        result = get_user(request, db_session)
        assert result is None
